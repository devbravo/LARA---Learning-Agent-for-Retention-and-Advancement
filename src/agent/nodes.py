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

from langgraph.errors import GraphInterrupt
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
    pending_subtopic_message_id: int | None   # message_id of last sent subtopic list
    pending_picker_message_id: int | None     # message_id of last sent duration picker
    pending_booking_message_id: int | None    # message_id of last sent booking confirm prompt
    pending_rating_message_id: int | None     # message_id of last sent rating buttons
    pending_weak_areas_message_id: int | None # message_id of last sent weak-areas prompt
    pending_category_message_id: int | None   # message_id of last sent category selector
    pending_topic_selection_message_id: int | None  # message_id of last sent topic selector
    has_unlogged_sessions: bool | None        # True when done_parser / log_weak_areas queued a topic for rating


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
    return "await_daily_confirmation"


def route_from_await_daily_confirmation(state: AgentState) -> str:
    payload = (state.get("payload") or "").lower().strip()
    return "book_events" if payload == "yes, book them" else "output"


def route_from_done_parser(state: AgentState) -> str:
    # Route to log_session when a topic was queued for rating
    return "log_session" if state.get("has_unlogged_sessions") else "output"


def route_from_log_weak_areas(state: AgentState) -> str:
    # log_weak_areas sets has_unlogged_sessions when more topics remain
    return "log_session" if state.get("has_unlogged_sessions") else "output"


def route_from_on_demand(state: AgentState) -> str:
    return "generate_brief" if state.get("proposed_topic") else "output"


def route_from_generate_brief(state: AgentState) -> str:
    """Route to await_brief_confirmation when a slot is available; else output."""
    if state.get("has_study_plan") and state.get("proposed_slot"):
        return "await_brief_confirmation"
    return "output"


def route_from_await_brief_confirmation(state: AgentState) -> str:
    payload = (state.get("payload") or "").lower().strip()
    return "book_events" if payload == "yes, book them" else "output"


def route_from_activate_topic(state: AgentState) -> str:
    # graduate_topic when a topic selection message was queued (no error)
    return "output" if state.get("messages") else "graduate_topic"


def route_from_study_topic(state: AgentState) -> str:
    return "output" if state.get("messages") else "study_topic_category"


def route_from_study_topic_category(state: AgentState) -> str:
    return "output" if state.get("messages") else "study_topic_confirm"


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
            in_progress_topics = topic_repository.get_in_progress_topic_names()
            evening_state = build_evening_preview_state(
                target_date, events, timed_events, due_topics, config, topics_config,
                in_progress_topics=in_progress_topics,
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
            "messages": [],  # clear any stale messages
        }

        if not has_study_plan:
            base_state["messages"] = [message]
            return cast(AgentState, base_state)

        # Send buttons and return — interrupt happens in await_daily_confirmation
        msg_id = _telegram.send_buttons(message, ["Yes, book them", "Skip"])
        base_state["pending_booking_message_id"] = msg_id
        return cast(AgentState, base_state)

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return cast(AgentState, {"messages": [f"⚠️ Daily briefing failed: {e}"]})


# ---------------------------------------------------------------------------
# Node: await_daily_confirmation  (interrupt lives here, not in daily_planning)
# ---------------------------------------------------------------------------

def await_daily_confirmation(state: AgentState) -> AgentState:
    """Wait for the user to confirm or skip the daily booking proposal.

    interrupt() is the very first statement so that on LangGraph resume the
    node re-runs with no side-effects before it — eliminating duplicate sends.
    """
    try:
        chat_id = state.get("chat_id")
        msg_id = state.get("pending_booking_message_id")

        booking_payload = interrupt("waiting for booking confirmation")

        # Remove buttons after resume (idempotent — silently fails if already gone)
        if msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, msg_id)
            except Exception:
                pass

        booking_payload_lower = (booking_payload or "").lower().strip()
        return {
            "payload": booking_payload,
            "pending_booking_message_id": None,
            "messages": [] if booking_payload_lower == "yes, book them"
                        else ["Okay, no study blocks booked. See you tomorrow! 👋"],
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Booking confirmation failed: {e}"]}


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
        # Return msg_id — interrupt lives in on_demand so there's no duplicate send on resume
        return {"pending_picker_message_id": msg_id}

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Duration picker failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: on_demand
# ---------------------------------------------------------------------------

def on_demand(state: AgentState) -> AgentState:
    """interrupt() is the very first statement so re-runs on resume are side-effect-free."""
    try:
        chat_id = state.get("chat_id")
        picker_msg_id = state.get("pending_picker_message_id")

        # Interrupt at start — no side effects above this line
        duration_payload = interrupt("waiting for duration selection")

        # Remove picker buttons after resume (idempotent)
        if picker_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, picker_msg_id)
            except Exception:
                pass

        try:
            duration_min = int((duration_payload or "30 min").replace(" min", "").strip())
        except (ValueError, AttributeError):
            duration_min = 30

        due_topics = _sm2.get_due_topics()
        topic = due_topics[0] if due_topics else None
        if topic is None:
            return {
                "messages": ["🎉 Nothing due for review right now — enjoy your break!"],
                "duration_min": duration_min,
                "pending_picker_message_id": None,
            }

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
            "duration_min": duration_min,
            "pending_picker_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ On-demand session failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: generate_brief
# ---------------------------------------------------------------------------

def generate_brief(state: AgentState) -> AgentState:
    try:
        topic = state.get("proposed_topic") or "General Study"
        duration_min = state.get("duration_min") or 30

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
            # Send brief with booking buttons — interrupt lives in await_brief_confirmation
            msg_id = _telegram.send_buttons(brief, ["Yes, book them", "Skip"])
            return {"pending_booking_message_id": msg_id, "messages": []}
        else:
            # No free slot — brief is the final output
            return {"messages": [brief]}

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {
            "messages": [f"⚠️ Could not generate brief: {e}\nProceeding with general study plan."],
        }


# ---------------------------------------------------------------------------
# Node: await_brief_confirmation  (interrupt lives here, not in generate_brief)
# ---------------------------------------------------------------------------

def await_brief_confirmation(state: AgentState) -> AgentState:
    """interrupt() is the very first statement — no duplicate sends on resume."""
    try:
        chat_id = state.get("chat_id")
        msg_id = state.get("pending_booking_message_id")

        booking_payload = interrupt("waiting for booking confirmation")

        if msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, msg_id)
            except Exception:
                pass

        return {
            "payload": booking_payload,
            "pending_booking_message_id": None,
            "messages": [],
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Booking confirmation failed: {e}"]}


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

    # Book in-progress [Study] events first (only when user confirmed, not on Skip)
    booked_study: list[str] = []
    try:
        in_progress_topics = topic_repository.get_in_progress_topic_names()
        events_today = _gcal.get_events(today)
        timed_events_today = [e for e in events_today if "dateTime" in e.get("start", {})]
        from src.agent.planning_helpers import is_topic_in_summary
        already_booked = {
            t for t in in_progress_topics
            if any(is_topic_in_summary(t, ev.get("summary", "")) for ev in timed_events_today)
        }
        rebook_study_events(in_progress_topics, timed_events_today, today, config)
        booked_study = [t for t in in_progress_topics if t not in already_booked]
    except Exception as e:
        logger.warning("[book_events] Failed to rebook study events: %s", e, exc_info=True)

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

    if booked or booked_study:
        parts = []
        if booked_study:
            study_lines = "\n".join(f"  • {t}" for t in booked_study)
            parts.append(f"📚 Booked {len(booked_study)} study session(s):\n{study_lines}")
        if booked:
            mock_lines = "\n".join(f"  • {t}" for t in booked)
            parts.append(f"🎯 Booked {len(booked)} mock session(s):\n{mock_lines}")
        try:
            _telegram.send_message("\n\n".join(parts))
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
            return {"messages": ["No study sessions were planned today."], "has_unlogged_sessions": False}

        logged_names = session_repository.get_logged_topic_names_for_today()
        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]

        if not unlogged:
            return {"messages": ["All sessions already logged for today."], "has_unlogged_sessions": False}

        slot = unlogged[0]
        topic_name = slot["topic"]
        topic_id = topic_repository.get_topic_id_by_name(topic_name)

        if topic_id is None:
            return {"messages": [f"⚠️ Topic '{topic_name}' not found in database."], "has_unlogged_sessions": False}

        logger.info("done_parser: sending rating buttons for %s", topic_name)
        # Send rating buttons and return — interrupt lives in log_session
        rating_msg_id = _telegram.send_buttons(f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"])

        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
            "quality_score": None,
            "pending_rating_message_id": rating_msg_id,
            "has_unlogged_sessions": True,
        }

    except Exception as e:
        logger.error("done_parser failed: %s", e, exc_info=True)
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Done flow failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: log_session
# ---------------------------------------------------------------------------

def log_session(state: AgentState) -> AgentState:
    """interrupt() is first — on resume, no side-effects run before it."""
    try:
        chat_id = state.get("chat_id")
        rating_msg_id = state.get("pending_rating_message_id")
        topic_id = state.get("current_topic_id")
        topic_name = state.get("current_topic_name") or "topic"

        # Interrupt at start — no side effects above this line
        rating_payload = interrupt("waiting for rating")

        # Remove rating buttons after resume (idempotent)
        if rating_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, rating_msg_id)
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

        session_repository.upsert_today_session(
            topic_id=topic_id,
            duration_min=duration_min,
            quality_score=quality,
        )
        _sm2_mod.update_topic_after_session(topic_id=topic_id, quality=quality)

        # Send weak areas prompt and return — interrupt lives in log_weak_areas
        weak_areas_msg_id = _telegram.send_buttons(
            "Any weak areas to note? Reply with text or tap Skip.",
            ["Skip"]
        )

        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
            "quality_score": quality,
            "payload": None,
            "pending_rating_message_id": None,
            "pending_weak_areas_message_id": weak_areas_msg_id,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Failed to log session: {e}"]}


# ---------------------------------------------------------------------------
# Node: log_weak_areas
# ---------------------------------------------------------------------------

def log_weak_areas(state: AgentState) -> AgentState:
    """interrupt() is first — on resume, no side-effects run before it."""
    try:
        chat_id = state.get("chat_id")
        weak_areas_msg_id = state.get("pending_weak_areas_message_id")
        topic_id = state.get("current_topic_id")

        # Interrupt at start — no side effects above this line
        weak_areas_payload = interrupt("waiting for weak areas")

        # Remove weak areas buttons after resume (idempotent)
        if weak_areas_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, weak_areas_msg_id)
            except Exception:
                pass

        text = (weak_areas_payload or "").strip()

        if not topic_id:
            return {"pending_weak_areas_message_id": None}

        session_id = session_repository.get_today_session_id(topic_id)

        if text and text.lower() != "skip":
            if session_id is not None:
                session_repository.update_session_weak_areas(session_id, text)
            topic_repository.update_topic_weak_areas(topic_id, text)
        else:
            topic_repository.update_topic_weak_areas(topic_id, None)

        # Check if more slots remain
        proposed_slots = state.get("proposed_slots") or []
        logged_names = session_repository.get_logged_topic_names_for_today()
        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]

        if not unlogged:
            return {
                "messages": ["All sessions logged for today. Great work! 💪"],
                "payload": None,
                "pending_weak_areas_message_id": None,
                "pending_rating_message_id": None,
                "has_unlogged_sessions": False,
            }

        # More topics: send next rating buttons — interrupt lives in log_session
        next_slot = unlogged[0]
        next_topic_name = next_slot["topic"]
        next_topic_id = topic_repository.get_topic_id_by_name(next_topic_name)

        if next_topic_id is None:
            return {
                "messages": [f"⚠️ Topic '{next_topic_name}' not found in database."],
                "payload": None,
                "pending_weak_areas_message_id": None,
                "pending_rating_message_id": None,
                "has_unlogged_sessions": False,
            }

        rating_msg_id = _telegram.send_buttons(
            f"How did {next_topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"]
        )

        return {
            "current_topic_id": next_topic_id,
            "current_topic_name": next_topic_name,
            "quality_score": None,
            "payload": None,
            "pending_weak_areas_message_id": None,
            "pending_rating_message_id": rating_msg_id,
            "has_unlogged_sessions": True,
        }

    except Exception as e:
        logger.error("log_weak_areas failed: %s", e, exc_info=True)
        if isinstance(e, GraphInterrupt):
            raise
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

        # Send buttons and return — interrupt lives in study_topic_category
        return {
            "pending_subtopic_message_id": None,
            "pending_category_message_id": cat_msg_id,
            "study_topic_category": None,  # clear any stale category
        }

    except Exception as e:
        logger.error("study_topic failed: %s", e, exc_info=True)
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Failed to load topics: {e}"], "pending_subtopic_message_id": None}


# ---------------------------------------------------------------------------
# Node: study_topic_category
# ---------------------------------------------------------------------------

def study_topic_category(state: AgentState) -> AgentState:
    """interrupt() is first — no duplicate category prompt on resume."""
    try:
        chat_id = state.get("chat_id")
        cat_msg_id = state.get("pending_category_message_id")

        # Interrupt at start — no side effects above this line
        category_payload = interrupt("waiting for category selection")

        # Remove category buttons after resume (idempotent)
        if cat_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, cat_msg_id)
            except Exception:
                pass

        category = (category_payload or "")[len("category:"):] \
            if (category_payload or "").startswith("category:") else category_payload

        rows = topic_repository.get_inactive_topics_tier1_or2()
        tier1 = [r for r in rows if r["tier"] == 1]
        available = tier1 if tier1 else [r for r in rows if r["tier"] == 2]

        if category == "Other":
            subtopic_rows = [r for r in available if " - " not in r["name"]]
        else:
            subtopic_rows = [r for r in available if r["name"].startswith(f"{category} - ")]

        if not subtopic_rows:
            return {"messages": [f"No topics found in category '{category}'."], "pending_category_message_id": None}

        buttons = [(r["name"], f"subtopic_id:{r['id']}") for r in subtopic_rows]
        try:
            subtopic_msg_id = _telegram.send_inline_buttons("Which topic?", buttons)
        except RuntimeError as e:
            if "timed out" in str(e).lower():
                logger.warning("send_inline_buttons timed out: %s", e)
                return {"messages": ["⚠️ Timed out while sending the topic list. Please retry /pick."], "pending_category_message_id": None}
            raise

        # Send subtopic buttons and return — interrupt lives in study_topic_confirm
        return {
            "study_topic_category": category,
            "pending_subtopic_message_id": subtopic_msg_id,
            "pending_category_message_id": None,
        }

    except Exception as e:
        logger.error("study_topic_category failed: %s", e, exc_info=True)
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Failed to load subtopics: {e}"], "pending_category_message_id": None}


# ---------------------------------------------------------------------------
# Node: study_topic_confirm
# ---------------------------------------------------------------------------

def study_topic_confirm(state: AgentState) -> AgentState:
    """interrupt() is first — no duplicate subtopic list on resume."""
    try:
        chat_id = state.get("chat_id")
        subtopic_msg_id = state.get("pending_subtopic_message_id")

        # Interrupt at start — no side effects above this line
        subtopic_payload = interrupt("waiting for subtopic selection")

        # Remove subtopic buttons after resume (idempotent)
        if subtopic_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, subtopic_msg_id)
            except Exception:
                pass

        try:
            topic_id = int((subtopic_payload or "")[len("subtopic_id:"):])
        except (ValueError, TypeError):
            return {"messages": ["⚠️ Invalid topic selection."], "pending_subtopic_message_id": None}

        resolved_name = topic_service.get_topic_name_by_id(topic_id)
        if resolved_name is None:
            return {"messages": ["⚠️ Topic not found."], "pending_subtopic_message_id": None}

        updated = topic_repository.set_topic_in_progress(resolved_name)
        if not updated:
            return {"messages": [f"⚠️ Topic '{resolved_name}' not found or already in progress."], "pending_subtopic_message_id": None}

        return {
            "messages": [
                f"✅ {resolved_name} added to In Progress. "
                "It will be booked on your calendar tomorrow morning."
            ],
            "pending_subtopic_message_id": None,
        }

    except Exception as e:
        logger.error("study_topic_confirm failed: %s", e, exc_info=True)
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Failed to set topic in progress: {e}"], "pending_subtopic_message_id": None}


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

        # Send buttons and return — interrupt lives in graduate_topic
        return {"pending_topic_selection_message_id": topic_msg_id}

    except Exception as e:
        logger.error("activate_topic failed: %s", e, exc_info=True)
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Failed to load in-progress topics: {e}"]}


# ---------------------------------------------------------------------------
# Node: graduate_topic
# ---------------------------------------------------------------------------

def graduate_topic(state: AgentState) -> AgentState:
    """interrupt() is first — no duplicate topic list on resume."""
    try:
        chat_id = state.get("chat_id")
        topic_msg_id = state.get("pending_topic_selection_message_id")

        # Interrupt at start — no side effects above this line
        studied_payload = interrupt("waiting for topic selection")

        # Remove buttons after resume (idempotent)
        if topic_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, topic_msg_id)
            except Exception:
                pass

        if not isinstance(studied_payload, str) or not studied_payload.startswith("studied:"):
            return {"messages": ["⚠️ Invalid topic selection."], "pending_topic_selection_message_id": None}

        try:
            topic_id = int(studied_payload[len("studied:"):])
        except ValueError:
            return {"messages": ["⚠️ Invalid topic id."], "pending_topic_selection_message_id": None}

        topic_name = topic_service.graduate_topic(topic_id)
        return {
            "messages": [
                f"✅ {topic_name} graduated to active. "
                "First SM-2 review scheduled for tomorrow."
            ],
            "pending_topic_selection_message_id": None,
        }

    except Exception as e:
        logger.error("graduate_topic failed: %s", e, exc_info=True)
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Failed to graduate topic: {e}"], "pending_topic_selection_message_id": None}
