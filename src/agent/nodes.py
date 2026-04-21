"""
LangGraph node implementations for the Learning Manager agent.

Each node receives an AgentState and returns a partial state update dict.
All exceptions are caught and surfaced as user-friendly messages in state.
"""

import pytz
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast, TypedDict

import logging
from src.core import sm2 as _sm2_mod

import yaml

from langgraph.types import interrupt

from src.agent.formatting import (
    format_time,
    local_datetime_str,
)
from src.agent.daily_planning_helpers import (
    append_calendar_lines,
    build_evening_preview_state,
    pack_mock_slots,
)
from src.agent.planning_helpers import (
    build_in_progress_study_slots,
    build_missing_study_events,
    get_prebooked_topics,
    rebook_study_events,
)
from src.core import gap_finder as _gap_finder
from src.core import sm2 as _sm2
from src.integrations import claude_api as _claude
from src.integrations import gcal as _gcal
from src.integrations import telegram_client as _telegram
from src.repositories import session_repository, topic_repository
from src.services import topic_service

_CONFIG_PATH = Path(__file__).parents[2] / "config.yaml"
_TOPICS_PATH = Path(__file__).parents[2] / "topics.yaml"
logger = logging.getLogger(__name__)

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _load_topics() -> dict:
    with open(_TOPICS_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    trigger: str               # fresh flow routing signal
    chat_id: int
    message_id: int | None     # Telegram message_id (legacy; button removal now happens in-node)
    duration_min: int | None
    proposed_topic: str | None          # single-slot flow (on_demand)
    proposed_slot: dict | None          # single-slot flow (on_demand)
    proposed_slots: list[dict] | None   # multi-slot flow (daily_planning)
    has_study_plan: bool                # False → skip confirm, go straight to output
    preview_only: bool                  # True → evening briefing, route to output not confirm
    quality_score: int | None
    messages: list[str]        # outbound Telegram messages
    payload: str | None        # raw resume value from interrupt()
    current_topic_id: int | None
    current_topic_name: str | None
    study_topic_category: str | None    # selected category in /pick flow
    pending_subtopic_message_id: int | None  # message_id of last sent subtopic list
    pending_picker_message_id: int | None    # message_id of last sent duration picker


# ---------------------------------------------------------------------------
# Node: router
# ---------------------------------------------------------------------------

def router(state: AgentState) -> AgentState:
    trigger = state.get("trigger", "")
    if not trigger:
        return {"messages": ["⚠️ No trigger set — cannot route."]}
    return {}


def route_from_router(state: AgentState) -> str:
    trigger = state.get("trigger", "")
    mapping = {
        "daily":    "daily_planning",
        "evening":  "daily_planning",
        "weekend":  "weekend_brief",
        "study":    "send_duration_picker",
        "done":     "done_parser",
        "pick":     "study_topic",
        "activate": "activate_topic",
    }
    return mapping.get(trigger, "output")


def route_from_daily_planning(state: AgentState) -> str:
    if state.get("preview_only") or not state.get("has_study_plan"):
        return "output"
    payload = (state.get("payload") or "").lower().strip()
    if payload == "yes, book them":
        return "book_events"
    return "output"


def route_from_done_parser(state: AgentState) -> str:
    if state.get("quality_score") is not None:
        return "log_session"
    return "output"


def route_from_log_weak_areas(state: AgentState) -> str:
    proposed_slots = state.get("proposed_slots") or []
    logged_names = session_repository.get_logged_topic_names_for_today()
    unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]
    return "log_session" if unlogged else "output"


def route_from_on_demand(state: AgentState) -> str:
    return "generate_brief" if state.get("proposed_topic") else "output"


def route_from_generate_brief(state: AgentState) -> str:
    """Route to book_events only when a slot exists and user confirmed booking.

    If no free slot was found (has_study_plan=False), the brief was already
    sent directly — go straight to output with no booking step.
    If a slot exists but user skipped, book_events handles the skip message.
    """
    if state.get("has_study_plan") and state.get("proposed_slot"):
        return "book_events"
    return "output"


def route_from_activate_topic(state: AgentState) -> str:
    return "graduate_topic" if state.get("payload") else "output"


def route_from_study_topic(state: AgentState) -> str:
    return "study_topic_category" if state.get("study_topic_category") else "output"


def route_from_study_topic_category(state: AgentState) -> str:
    return "study_topic_confirm" if state.get("proposed_topic") else "output"


# ---------------------------------------------------------------------------
# Node: daily_planning
# ---------------------------------------------------------------------------

def daily_planning(state: AgentState) -> AgentState:
    try:
        trigger = state.get("trigger", "daily")
        is_evening = trigger == "evening"

        today = date.today()
        target_date = today + timedelta(days=1) if is_evening else today
        config = _load_config()

        events = _gcal.get_events(target_date)
        due_topics = _sm2.get_due_topics(target_date=target_date)
        timed_events = [e for e in events if "dateTime" in e.get("start", {})]

        if is_evening:
            topics_config = _load_topics()
            evening_state = build_evening_preview_state(
                target_date, events, timed_events, due_topics, config, topics_config
            )
            return cast(AgentState, evening_state)

        # --- Morning briefing ---
        _TZ = pytz.timezone(config["timezone"])
        after_time = datetime.now(_TZ).time()

        in_progress_topics = topic_repository.get_in_progress_topic_names()

        study_busy_events = build_missing_study_events(
            in_progress_topics, timed_events, target_date, config
        )

        free_windows = _gap_finder.find_free_windows(
            events + study_busy_events, target_date, config, after_time
        )
        prebooked = get_prebooked_topics(timed_events, due_topics)

        day_str = f"{target_date.strftime('%A %B')} {target_date.day}"
        lines = [f"☀️ Good morning Diego — {day_str}", ""]
        append_calendar_lines(lines, timed_events, "📅 Your day: No meetings today")

        available_topics = [t for t in due_topics if t["name"] not in prebooked]
        topics_config = _load_topics()
        min_window_minutes = config.get("min_window_minutes", 25)
        proposed_topic, proposed_slot, proposed_slots = pack_mock_slots(
            target_date,
            free_windows,
            available_topics,
            topics_config,
            min_window_minutes,
            lines,
        )

        in_progress_study_slots = build_in_progress_study_slots(in_progress_topics, timed_events, target_date)

        if in_progress_study_slots:
            lines.append("⏳ In Progress:")
            for slot in in_progress_study_slots:
                lines.append(
                    f"• {slot['start']}–{slot['end']} [STUDY] {slot['topic']} ({slot['duration_min']}min)"
                )
            lines.append("")

        rebook_study_events(in_progress_topics, timed_events, target_date, config)

        assigned_names = {slot["topic"] for slot in proposed_slots}
        backlog_topics = [t for t in available_topics if t["name"] not in assigned_names]

        if backlog_topics:
            lines.append("📌 Also due but no window today:")
            for topic in backlog_topics[:3]:
                lines.append(f" {topic['name']}")
            if len(backlog_topics) > 3:
                lines.append(f" +{len(backlog_topics) - 3} more")
            lines.append("")

        has_study_plan = bool(proposed_slots)
        message = "\n".join(lines + (["Confirm these mock interview blocks?"] if has_study_plan else ["No mock interview windows available today — calendar fully booked."]))

        base_state: dict = {
            "proposed_topic": proposed_topic,
            "proposed_slot": proposed_slot,
            "proposed_slots": proposed_slots if proposed_slots else None,
            "has_study_plan": has_study_plan,
            "preview_only": False,
        }

        if not has_study_plan:
            base_state["messages"] = [message]
            return cast(AgentState, base_state)

        # Interactive path: send buttons and interrupt for user confirmation
        chat_id = state.get("chat_id")
        msg_id = _telegram.send_buttons(message, ["Yes, book them", "Skip"])
        booking_payload = interrupt("waiting for booking confirmation")

        # Remove buttons after resume
        if msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, msg_id)
            except Exception:
                pass

        booking_payload_lower = (booking_payload or "").lower().strip()
        if booking_payload_lower != "yes, book them":
            base_state["messages"] = ["Okay, no study blocks booked. See you tomorrow! 👋"]

        base_state["payload"] = booking_payload
        return cast(AgentState, base_state)

    except Exception as e:
        return cast(AgentState, {"messages": [f"⚠️ Daily briefing failed: {e}"]})


# ---------------------------------------------------------------------------
# Node: weekend_brief
# ---------------------------------------------------------------------------

def weekend_brief(state: AgentState) -> AgentState:
    try:
        today = date.today()
        day_str = f"{today.strftime('%A %B')} {today.day}"
        lines = [f"☀️ Good morning Diego — {day_str}", ""]

        due_topics = _sm2.get_due_topics(target_date=today)
        in_progress = topic_repository.get_in_progress_topic_names()

        if due_topics:
            lines.append(f"🎯 You have {len(due_topics)} topic(s) due for review today:")
            for topic in due_topics:
                weak = topic.get("weak_areas")
                focus = f" — focus: {weak}" if weak else ""
                overdue_days = (today - date.fromisoformat(topic["next_review"])).days
                overdue_str = f" ⚠️ overdue {overdue_days}d" if overdue_days > 0 else ""
                lines.append(f"• {topic['name']}{overdue_str}{focus}")
            lines.append("")
            lines.append("What time block will you have today to tackle these?")

        elif in_progress:
            lines.append("📚 Nothing due for SM-2 review today.")
            lines.append("")
            lines.append("You have in-progress topics:")
            for name in in_progress:
                lines.append(f"• {name}")
            lines.append("")
            lines.append("Want to book a study block for one of these? Tell me when.")

        else:
            lines.append("🎉 You're all caught up — nothing due today.")
            lines.append("")
            lines.append("Take a rest, or do /study if you want to practice anyway.")

        return {
            "messages": ["\n".join(lines)],
            "has_study_plan": False,
            "preview_only": True,
            "proposed_slots": None,
            "proposed_slot": None,
            "proposed_topic": None,
        }

    except Exception as e:
        logger.error("weekend_brief failed: %s", e)
        return {"messages": [f"⚠️ Weekend brief failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: send_duration_picker
# ---------------------------------------------------------------------------

def send_duration_picker(state: AgentState) -> AgentState:
    try:
        chat_id = state.get("chat_id")

        # Clean up stale picker
        old_id = state.get("pending_picker_message_id")
        if old_id is not None and chat_id:
            try:
                _telegram.remove_buttons(chat_id, old_id)
            except Exception:
                pass

        msg_id = _telegram.send_buttons("How long do you have?", ["30 min", "45 min", "60 min"])

        duration_payload = interrupt("waiting for duration selection")

        # Remove buttons after resume
        if msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, msg_id)
            except Exception:
                pass

        try:
            duration_min = int((duration_payload or "30 min").replace(" min", "").strip())
        except (ValueError, AttributeError):
            duration_min = 30

        return {
            "pending_picker_message_id": msg_id,
            "duration_min": duration_min,
        }

    except Exception as e:
        return {"messages": [f"⚠️ Duration picker failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: on_demand
# ---------------------------------------------------------------------------

def on_demand(state: AgentState) -> AgentState:
    try:
        due_topics = _sm2.get_due_topics()
        topic = due_topics[0] if due_topics else None
        if topic is None:
            return {"messages": ["🎉 Nothing due for review right now — enjoy your break!"]}

        duration_min = state.get("duration_min") or 30

        # Find a free window of the requested duration
        config = _load_config()
        today = date.today()
        events = _gcal.get_events(today)
        _TZ = pytz.timezone(config["timezone"])
        after_time = datetime.now(_TZ).time()
        free_windows = _gap_finder.find_free_windows(events, today, config, after_time)

        proposed_slot = None
        for window in free_windows:
            w_start = window["start"]
            w_end = window["end"]
            window_duration = (w_end.hour * 60 + w_end.minute) - (w_start.hour * 60 + w_start.minute)
            if window_duration >= duration_min:
                slot_end_min = w_start.hour * 60 + w_start.minute + duration_min
                slot_end_h, slot_end_m = divmod(slot_end_min, 60)
                proposed_slot = {
                    "topic": topic["name"],
                    "start": f"{w_start.hour:02d}:{w_start.minute:02d}",
                    "end": f"{slot_end_h:02d}:{slot_end_m:02d}",
                    "duration_min": duration_min,
                }
                break

        status = f"📚 Generating a {duration_min} min brief for {topic['name']}…"
        _telegram.send_message(status)
        return {
            "proposed_topic": topic["name"],
            "proposed_slot": proposed_slot,
            "has_study_plan": proposed_slot is not None,
        }

    except Exception as e:
        return {"messages": [f"⚠️ On-demand session failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: generate_brief
# ---------------------------------------------------------------------------

def generate_brief(state: AgentState) -> AgentState:
    try:
        topic = state.get("proposed_topic") or "General Study"
        duration_min = state.get("duration_min") or 30
        chat_id = state.get("chat_id")

        context = "General review"
        weak_areas = topic_repository.get_topic_weak_areas_by_name(topic)
        if weak_areas:
            context = f"Focus on weak areas: {weak_areas}"

        brief = _claude.generate_brief(
            topic=topic,
            duration_min=duration_min,
            context=context,
        )

        if state.get("has_study_plan") and state.get("proposed_slot"):
            # Send brief with booking buttons and wait for confirmation.
            # Do NOT add to messages — output runs after book_events and
            # must not re-send the brief.
            msg_id = _telegram.send_buttons(brief, ["Yes, book them", "Skip"])
            booking_payload = interrupt("waiting for booking confirmation")

            if msg_id and chat_id:
                try:
                    _telegram.remove_buttons(chat_id, msg_id)
                except Exception:
                    pass

            return {"payload": booking_payload}
        else:
            # No free slot found — brief is the final output; let output send it.
            return {"messages": [brief]}

    except Exception as e:
        return {
            "messages": [f"⚠️ Could not generate brief: {e}\nProceeding with general study plan."],
        }


# ---------------------------------------------------------------------------
# Node: book_events
# ---------------------------------------------------------------------------

def book_events(state: AgentState) -> AgentState:
    payload = (state.get("payload") or "").lower().strip()
    if payload == "skip":
        try:
            _telegram.send_message("Okay, no study blocks booked. See you tomorrow! 👋")
        except Exception as e:
            logger.warning("[book_events] Failed to send skip message: %s", e)
        return {}

    today = date.today()
    config = _load_config()
    tz = pytz.timezone(config["timezone"])

    booked: list[str] = []
    slots = state.get("proposed_slots")

    if slots:
        for slot in slots:
            try:
                t_start = format_time(slot["start"])
                t_end = format_time(slot["end"])
                start_hour, start_minute = map(int, t_start.split(":"))
                end_hour, end_minute = map(int, t_end.split(":"))
                _gcal.write_event(
                    topic=slot["topic"],
                    start=local_datetime_str(today, start_hour, start_minute, tz),
                    end=local_datetime_str(today, end_hour, end_minute, tz),
                )
                booked.append(slot["topic"])
            except Exception as e:
                logger.warning("[book_events] Calendar write failed for %s: %s", slot.get("topic"), e, exc_info=True)
    else:
        try:
            topic = state.get("proposed_topic")
            slot = state.get("proposed_slot")
            if topic and slot:
                t_start = format_time(slot["start"])
                t_end = format_time(slot["end"])
                start_hour, start_minute = map(int, t_start.split(":"))
                end_hour, end_minute = map(int, t_end.split(":"))
                _gcal.write_event(
                    topic=topic,
                    start=local_datetime_str(today, start_hour, start_minute, tz),
                    end=local_datetime_str(today, end_hour, end_minute, tz),
                )
                booked.append(topic)
        except Exception as e:
            logger.warning("[book_events] Calendar write failed: %s", e, exc_info=True)

    if booked:
        summary = "\n".join(f"  • {t}" for t in booked)
        try:
            _telegram.send_message(f"✅ Booked {len(booked)} mock session(s):\n{summary}")
        except Exception as e:
            logger.warning("[book_events] Confirmation send failed: %s", e, exc_info=True)
    else:
        try:
            _telegram.send_message(
                "⚠️ Could not book any sessions — Google Calendar may be unavailable. "
                "Please try confirming again."
            )
        except Exception as e:
            logger.warning("[book_events] Failed to send booking-failure notice: %s", e, exc_info=True)

    return {}


# ---------------------------------------------------------------------------
# Node: done_parser
# ---------------------------------------------------------------------------

def done_parser(state: AgentState) -> AgentState:
    logger.info("done_parser: entered")

    try:
        proposed_slots = state.get("proposed_slots") or []
        if not proposed_slots:
            return {"messages": ["No study sessions were planned today."]}

        logged_names = session_repository.get_logged_topic_names_for_today()
        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]

        if not unlogged:
            return {"messages": ["All sessions already logged for today."]}

        slot = unlogged[0]
        topic_name = slot["topic"]
        topic_id = topic_repository.get_topic_id_by_name(topic_name)

        if topic_id is None:
            return {"messages": [f"⚠️ Topic '{topic_name}' not found in database."]}

        logger.info("done_parser: sending rating buttons for %s", topic_name)
        chat_id = state.get("chat_id")
        rating_msg_id = _telegram.send_buttons(f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"])

        rating_payload = interrupt("waiting for rating")

        if rating_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, rating_msg_id)
            except Exception:
                pass

        score_map = {"😕 hard": 2, "😐 ok": 3, "😊 easy": 5}
        quality = score_map.get((rating_payload or "").lower().strip(), 3)

        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
            "quality_score": quality,
        }

    except Exception as e:
        logger.error("done_parser failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Done flow failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: log_session
# ---------------------------------------------------------------------------

def log_session(state: AgentState) -> AgentState:
    try:
        quality = state.get("quality_score")
        topic_id = state.get("current_topic_id")
        topic_name = state.get("current_topic_name") or "topic"

        if quality is None or topic_id is None:
            # Loop case: find next unlogged topic and get rating via interrupt
            proposed_slots = state.get("proposed_slots") or []
            logged_names = session_repository.get_logged_topic_names_for_today()
            unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]

            if not unlogged:
                return {"messages": ["All sessions logged for today. Great work! 💪"]}

            slot = unlogged[0]
            topic_name = slot["topic"]
            topic_id = topic_repository.get_topic_id_by_name(topic_name)

            if topic_id is None:
                return {"messages": [f"⚠️ Topic '{topic_name}' not found in database."]}

            chat_id = state.get("chat_id")
            loop_rating_msg_id = _telegram.send_buttons(f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"])
            rating_payload = interrupt("waiting for rating (loop)")

            if loop_rating_msg_id and chat_id:
                try:
                    _telegram.remove_buttons(chat_id, loop_rating_msg_id)
                except Exception:
                    pass

            score_map = {"😕 hard": 2, "😐 ok": 3, "😊 easy": 5}
            quality = score_map.get((rating_payload or "").lower().strip(), 3)

        # Find duration from proposed_slots
        proposed_slots = state.get("proposed_slots") or []
        duration_min = 0
        for slot in proposed_slots:
            if slot["topic"] == topic_name:
                duration_min = slot["duration_min"]
                break

        chat_id = state.get("chat_id")
        session_repository.upsert_today_session(
            topic_id=topic_id,
            duration_min=duration_min,
            quality_score=quality,
        )
        _sm2_mod.update_topic_after_session(topic_id=topic_id, quality=quality)

        weak_areas_msg_id = _telegram.send_buttons(
            "Any weak areas to note? Reply with text or tap Skip.",
            ["Skip"]
        )
        weak_areas_payload = interrupt("waiting for weak areas")

        if weak_areas_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, weak_areas_msg_id)
            except Exception:
                pass

        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
            "quality_score": None,  # clear so next loop iteration finds next topic
            "payload": weak_areas_payload,
        }

    except Exception as e:
        return {"messages": [f"⚠️ Failed to log session: {e}"]}


# ---------------------------------------------------------------------------
# Node: log_weak_areas
# ---------------------------------------------------------------------------

def log_weak_areas(state: AgentState) -> AgentState:
    try:
        text = (state.get("payload") or "").strip()
        topic_id = state.get("current_topic_id")

        if not topic_id:
            return {}

        session_id = session_repository.get_today_session_id(topic_id)

        if text and text.lower() != "skip":
            if session_id is not None:
                session_repository.update_session_weak_areas(session_id, text)
            topic_repository.update_topic_weak_areas(topic_id, text)
        else:
            topic_repository.update_topic_weak_areas(topic_id, None)

        # Check if more slots remain — set final message if done
        proposed_slots = state.get("proposed_slots") or []
        logged_names = session_repository.get_logged_topic_names_for_today()
        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]

        if not unlogged:
            return {"messages": ["All sessions logged for today. Great work! 💪"], "payload": None}

        return {"payload": None}

    except Exception as e:
        logger.error("log_weak_areas failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to log weak areas: {e}"], "payload": None}


# ---------------------------------------------------------------------------
# Node: output
# ---------------------------------------------------------------------------

def output(state: AgentState) -> AgentState:
    messages = state.get("messages") or []
    if messages:
        try:
            _telegram.send_message(messages[-1])
        except Exception as e:
            logger.warning("[output] Telegram send failed: %s", e, exc_info=True)
    return {}


# ---------------------------------------------------------------------------
# Node: study_topic
# ---------------------------------------------------------------------------

def study_topic(state: AgentState) -> AgentState:
    try:
        chat_id = state.get("chat_id")

        # Clean up any leftover subtopic list
        old_msg_id = state.get("pending_subtopic_message_id")
        if old_msg_id is not None and chat_id:
            try:
                _telegram.remove_buttons(chat_id, old_msg_id)
            except Exception:
                pass

        rows = topic_repository.get_inactive_topics_tier1_or2()
        tier1 = [r for r in rows if r["tier"] == 1]
        available = tier1 if tier1 else [r for r in rows if r["tier"] == 2]

        if not available:
            return {"messages": ["No inactive topics available to start studying."], "pending_subtopic_message_id": None}

        categories = sorted(set(
            r["name"].split(" - ")[0] if " - " in r["name"] else "Other"
            for r in available
        ))

        buttons = [(c, f"category:{c}") for c in categories]
        cat_msg_id = _telegram.send_inline_buttons("Which category?", buttons)

        category_payload = interrupt("waiting for category selection")

        # Remove category buttons after resume
        if cat_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, cat_msg_id)
            except Exception:
                pass

        category = (category_payload or "")[len("category:"):] if (category_payload or "").startswith("category:") else category_payload

        return {
            "pending_subtopic_message_id": None,
            "study_topic_category": category,
        }

    except Exception as e:
        logger.error("study_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to load topics: {e}"], "pending_subtopic_message_id": None}


# ---------------------------------------------------------------------------
# Node: study_topic_category
# ---------------------------------------------------------------------------

def study_topic_category(state: AgentState) -> AgentState:
    try:
        category = state.get("study_topic_category")
        if not category:
            return {"messages": ["⚠️ No category selected."]}

        chat_id = state.get("chat_id")

        rows = topic_repository.get_inactive_topics_tier1_or2()
        tier1 = [r for r in rows if r["tier"] == 1]
        available = tier1 if tier1 else [r for r in rows if r["tier"] == 2]

        if category == "Other":
            subtopic_rows = [r for r in available if " - " not in r["name"]]
        else:
            subtopic_rows = [r for r in available if r["name"].startswith(f"{category} - ")]

        if not subtopic_rows:
            return {"messages": [f"No topics found in category '{category}'."]}

        buttons = [(r["name"], f"subtopic_id:{r['id']}") for r in subtopic_rows]
        try:
            subtopic_msg_id = _telegram.send_inline_buttons("Which topic?", buttons)
        except RuntimeError as e:
            if "timed out" in str(e).lower():
                logger.warning("send_inline_buttons timed out: %s", e)
                return {"messages": ["⚠️ Timed out while sending the topic list. Please retry /pick."]}
            raise

        subtopic_payload = interrupt("waiting for subtopic selection")

        # Remove subtopic buttons after resume
        if subtopic_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, subtopic_msg_id)
            except Exception:
                pass

        try:
            topic_id = int((subtopic_payload or "")[len("subtopic_id:"):])
        except (ValueError, TypeError):
            return {"messages": ["⚠️ Invalid topic selection."]}

        resolved_name = topic_service.get_topic_name_by_id(topic_id)
        if resolved_name is None:
            return {"messages": [f"⚠️ Topic not found."]}

        return {
            "pending_subtopic_message_id": subtopic_msg_id,
            "proposed_topic": resolved_name,
        }

    except Exception as e:
        logger.error("study_topic_category failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to load subtopics: {e}"]}


# ---------------------------------------------------------------------------
# Node: study_topic_confirm
# ---------------------------------------------------------------------------

def study_topic_confirm(state: AgentState) -> AgentState:
    try:
        topic_name = state.get("proposed_topic")
        if not topic_name:
            return {"messages": ["⚠️ No topic selected."]}

        updated = topic_repository.set_topic_in_progress(topic_name)
        if not updated:
            return {"messages": [f"⚠️ Topic '{topic_name}' not found or already in progress."]}

        return {
            "messages": [
                f"✅ {topic_name} added to In Progress. "
                "It will be booked on your calendar tomorrow morning."
            ],
            "pending_subtopic_message_id": None,
        }

    except Exception as e:
        logger.error("study_topic_confirm failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to set topic in progress: {e}"]}


# ---------------------------------------------------------------------------
# Node: activate_topic
# ---------------------------------------------------------------------------

def activate_topic(state: AgentState) -> AgentState:
    try:
        topics = topic_service.get_in_progress_topics()

        if not topics:
            return {"messages": ["No topics currently in progress."]}

        chat_id = state.get("chat_id")
        buttons = [(t["name"], f"studied:{t['id']}") for t in topics]
        topic_msg_id = _telegram.send_inline_buttons("Which topic did you just study?", buttons)

        studied_payload = interrupt("waiting for topic selection")

        # Remove buttons after resume
        if topic_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, topic_msg_id)
            except Exception:
                pass

        return {"payload": studied_payload}

    except Exception as e:
        logger.error("activate_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to load in-progress topics: {e}"]}


# ---------------------------------------------------------------------------
# Node: graduate_topic
# ---------------------------------------------------------------------------

def graduate_topic(state: AgentState) -> AgentState:
    try:
        payload = state.get("payload") or ""

        if not payload.startswith("studied:"):
            return {"messages": ["⚠️ Invalid topic selection."]}

        try:
            topic_id = int(payload[len("studied:"):])
        except ValueError:
            return {"messages": ["⚠️ Invalid topic id."]}

        topic_name = topic_service.graduate_topic(topic_id)
        return {
            "messages": [
                f"✅ {topic_name} graduated to active. "
                "First SM-2 review scheduled for tomorrow."
            ]
        }

    except Exception as e:
        logger.error("graduate_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to graduate topic: {e}"]}
