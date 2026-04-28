"""
LangGraph node implementations for the Learning Manager agent.

Each node receives an AgentState and returns a partial state update dict.
All exceptions are caught and surfaced as user-friendly messages in state.
"""

import json
import pytz
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast, TypedDict

import logging
from src.core import sm2 as _sm2_mod

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

from src.agent.weak_areas_helpers import null_if_skip, to_key, breakdown, load_topics, load_config

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

# WEAK AREAS CONSTANTS
_DSA_ALL = ["edge_case", "time_complexity", "implementation"]
_SYSDESIGN_ALL = ["scalability", "data_pipeline", "trade_offs", "estimation",
                  "component_selection", "latency_vs_throughput"]
_BEHAVIORAL_ALL = ["delivery", "quantification", "structure"]





# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    trigger: str               # fresh flow routing signal
    chat_id: int
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
    pending_message_id: int | None      # message_id of the one button message currently awaiting user interaction
    has_unlogged_sessions: bool | None  # True when done_parser / log_weak_areas queued a topic for rating
    weak_areas_first_answer: str | None # Q1 answer carried from log_weak_areas to log_weak_areas_q2
    weak_areas_topic_type: str | None    # topic_type carried from log_weak_areas to log_weak_areas_q2


# ---------------------------------------------------------------------------
# Node: router
# ---------------------------------------------------------------------------

def router(state: AgentState) -> AgentState:
    trigger = state.get("trigger", "")
    if not trigger:
        return {"messages": ["⚠️ No trigger set — cannot route."]}
    # Clear stale messages from any previous flow so routing guards
    # (e.g. route_from_study_topic) don't misread checkpoint state.
    return {"messages": []}


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
    if not state.get("has_unlogged_sessions"):
        return "output"
    return "log_session" if state.get("current_topic_id") is not None else "select_done_topic"


def route_from_select_done_topic(state: AgentState) -> str:
    """Route to output on error (messages set or no current_topic_id), else log_session."""
    if state.get("messages") or state.get("current_topic_id") is None:
        return "output"
    return "log_session"


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


def route_from_log_weak_areas(state: AgentState) -> str:
    """Conceptual topics complete in Q1 and route to output; all others need Q2."""
    if state.get("weak_areas_topic_type") == "conceptual":
        return "output"
    return "log_weak_areas_q2"


# ---------------------------------------------------------------------------
# Node: daily_planning
# ---------------------------------------------------------------------------

def daily_planning(state: AgentState) -> AgentState:
    try:
        trigger = state.get("trigger", "daily")
        is_evening = trigger == "evening"

        today = date.today()
        target_date = today + timedelta(days=1) if is_evening else today
        config = load_config()

        events = _gcal.get_events(target_date)
        due_topics = _sm2.get_due_topics(target_date=target_date)
        timed_events = [e for e in events if "dateTime" in e.get("start", {})]

        if is_evening:
            topics_config = load_topics()
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
        topics_config = load_topics()
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

        base_state: AgentState = {
            "proposed_topic": proposed_topic,
            "proposed_slot": proposed_slot,
            "proposed_slots": proposed_slots if proposed_slots else None,
            "has_study_plan": has_study_plan,
            "preview_only": False,
            "messages": [],  # clear any stale messages
        }

        if not has_study_plan:
            base_state["messages"] = [message]
            return base_state

        # Send buttons and return — interrupt happens in await_daily_confirmation
        msg_id = _telegram.send_buttons(message, ["Yes, book them", "Skip"])
        base_state["pending_message_id"] = msg_id
        return base_state

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Daily briefing failed: {e}"]}


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
        msg_id = state.get("pending_message_id")

        booking_payload = interrupt("waiting for booking confirmation")

        # Remove buttons after resume (idempotent — silently fails if already gone)
        if msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        booking_payload_lower = (booking_payload or "").lower().strip()
        return {
            "payload": booking_payload,
            "pending_message_id": None,
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

def weekend_brief(_state: AgentState) -> AgentState:
    try:
        today = date.today()
        day_str = f"{today.strftime('%A %B')} {today.day}"
        lines = [f"☀️ Good morning Diego — {day_str}", ""]

        due_topics = _sm2.get_due_topics(target_date=today)
        in_progress = topic_repository.get_in_progress_topic_names()

        if due_topics:
            lines.append(f"🎯 You have {len(due_topics)} topic(s) due for review today:")
            for topic in due_topics:
                overdue_days = (today - date.fromisoformat(topic["next_review"])).days
                overdue_str = f" ⚠️ overdue {overdue_days}d" if overdue_days > 0 else ""
                lines.append(f"• {topic['name']}{overdue_str}")
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

        # Clean up stale picker from a previous abandoned /study flow
        old_id = state.get("pending_message_id")
        if old_id is not None and chat_id:
            try:
                _telegram.remove_buttons(chat_id, old_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        msg_id = _telegram.send_buttons("How long do you have?", ["30 min", "45 min", "60 min"])
        # Return msg_id — interrupt lives in on_demand so there's no duplicate send on resume
        return {"pending_message_id": msg_id}

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
        picker_msg_id = state.get("pending_message_id")

        # Interrupt at start — no side effects above this line
        duration_payload = interrupt("waiting for duration selection")

        # Remove picker buttons after resume (idempotent)
        if picker_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, picker_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

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
                "pending_message_id": None,
            }

        # Find a free window of the requested duration
        config = load_config()
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
            "pending_message_id": None,
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
            return {"pending_message_id": msg_id, "messages": []}
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
        msg_id = state.get("pending_message_id")

        booking_payload = interrupt("waiting for booking confirmation")

        if msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        return {
            "payload": booking_payload,
            "pending_message_id": None,
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
    today = date.today()
    config = load_config()
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
        unlogged = topic_repository.get_active_unlogged_topics_today()

        # Scope to today's plan when proposed_slots are available; fall back to
        # topics actually due today so /done on a weekend (no morning plan) doesn't
        # present every active topic.
        proposed_slots = state.get("proposed_slots") or []
        planned_names = {s["topic"] for s in proposed_slots}
        if planned_names:
            unlogged = [t for t in unlogged if t["name"] in planned_names]
        else:
            due_names = {t["name"] for t in _sm2.get_due_topics()}
            unlogged = [t for t in unlogged if t["name"] in due_names]

        if not unlogged:
            return {"messages": ["No active sessions to log right now."], "has_unlogged_sessions": False}

        if len(unlogged) >= 2:
            logger.info("done_parser: %d unlogged topics — sending picker", len(unlogged))
            # One button per row so long topic names don't get truncated
            picker_msg_id = _telegram.send_inline_buttons(
                "Which topic did you just finish?",
                [(t["name"], t["name"]) for t in unlogged],
            )
            return {
                "current_topic_id": None,
                "current_topic_name": None,
                "quality_score": None,
                "pending_message_id": picker_msg_id,
                "has_unlogged_sessions": True,
            }

        # Exactly one unlogged topic — skip picker
        topic = unlogged[0]
        topic_id = topic["id"]
        topic_name = topic["name"]

        proposed_slots = state.get("proposed_slots") or []
        duration_min = next(
            (s["duration_min"] for s in proposed_slots if s["topic"] == topic_name), 0
        )

        logger.info("done_parser: sending rating buttons for %s", topic_name)
        rating_msg_id = _telegram.send_buttons(f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"])

        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
            "duration_min": duration_min,
            "quality_score": None,
            "pending_message_id": rating_msg_id,
            "has_unlogged_sessions": True,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("done_parser failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Done flow failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: select_done_topic
# ---------------------------------------------------------------------------

def select_done_topic(state: AgentState) -> AgentState:
    """interrupt() is first — on resume, receives selected topic name from picker button."""
    try:
        chat_id = state.get("chat_id")
        picker_msg_id = state.get("pending_message_id")

        # Interrupt at start — no side effects above this line
        selected_name = interrupt("waiting for topic selection")

        # Remove picker buttons after resume (idempotent)
        if picker_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, picker_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        topic_name = (selected_name or "").strip()
        topic_id = topic_repository.get_topic_id_by_name(topic_name)

        if topic_id is None:
            return {"messages": [f"⚠️ Topic '{topic_name}' not found in database."], "has_unlogged_sessions": False}

        proposed_slots = state.get("proposed_slots") or []
        duration_min = next(
            (s["duration_min"] for s in proposed_slots if s["topic"] == topic_name), 0
        )

        rating_msg_id = _telegram.send_buttons(f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"])

        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
            "duration_min": duration_min,
            "quality_score": None,
            "pending_message_id": rating_msg_id,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("select_done_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Topic selection failed: {e}"], "has_unlogged_sessions": False}


# ---------------------------------------------------------------------------
# Node: log_session
# ---------------------------------------------------------------------------

def log_session(state: AgentState) -> AgentState:
    """interrupt() is first — on resume, no side-effects run before it."""
    try:
        chat_id = state.get("chat_id")
        rating_msg_id = state.get("pending_message_id")
        topic_id = state.get("current_topic_id")
        topic_name = state.get("current_topic_name") or "topic"

        # Interrupt at start — no side effects above this line
        rating_payload = interrupt("waiting for rating")

        # Remove rating buttons after resume (idempotent)
        if rating_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, rating_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

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
            student_quality=quality,
        )
        teacher_quality = session_repository.get_today_teacher_quality(topic_id)
        sm2_quality = teacher_quality if teacher_quality is not None else quality
        _sm2_mod.update_topic_after_session(topic_id=topic_id, quality=sm2_quality)

        # Send type-specific first structured feedback question
        topic_type = topic_repository.get_topic_type_by_id(topic_id) or "conceptual"
        if topic_type == "dsa":
            first_msg_id = _telegram.send_inline_buttons(
                "What broke down?",
                [("Edge case", "Edge case"), ("Time complexity", "Time complexity"),
                 ("Implementation", "Implementation"), ("All of the above", "All of the above"),
                 ("Nothing", "Nothing")],
            )
        elif topic_type == "system_design":
            first_msg_id = _telegram.send_buttons(
                "Describe the scenario briefly, or tap Skip.",
                ["Skip"],
            )
        elif topic_type == "conceptual":
            first_msg_id = _telegram.send_buttons(
                "What couldn't you answer? or tap Skip",
                ["Skip"],
            )
        else:  # behavioral
            first_msg_id = _telegram.send_buttons(
                "Which story did you practice? or tap Skip.",
                ["Skip"],
            )

        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
            "quality_score": quality,
            "payload": None,
            "pending_message_id": first_msg_id,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        return {"messages": [f"⚠️ Failed to log session: {e}"]}


# ---------------------------------------------------------------------------
# Node: log_weak_areas
# ---------------------------------------------------------------------------

def log_weak_areas(state: AgentState) -> AgentState:
    """Holds interrupt 1. On resume removes Q1 buttons and sends Q2 prompt."""
    try:
        chat_id = state.get("chat_id")
        first_msg_id = state.get("pending_message_id")
        topic_id = state.get("current_topic_id")

        # ── INTERRUPT 1 at top — no side effects before this ────────────────
        first_answer = interrupt("waiting for first feedback")

        if first_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, first_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        first_text = (first_answer or "").strip()

        topic_type = topic_repository.get_topic_type_by_id(topic_id) if topic_id else None
        topic_type = topic_type or "conceptual"

        if topic_type == "dsa":
            second_msg_id = _telegram.send_buttons(
                "Which problems did you solve? (e.g Two Sum, Valid Parentheses)",
                ["Skip"],
            )
        elif topic_type == "system_design":
            second_msg_id = _telegram.send_inline_buttons(
                "What felt weak?",
                [("Scalability", "Scalability"), ("Data pipeline", "Data pipeline"),
                 ("Trade-offs", "Trade-offs"), ("Estimation", "Estimation"),
                 ("Component selection", "Component selection"),
                 ("Latency vs throughput", "Latency vs throughput"),
                 ("All of the above", "All of the above"), ("Nothing", "Nothing")],
            )
        elif topic_type == "conceptual":
            weak_json_str = json.dumps({"unclear": null_if_skip(first_text)})
            session_id = session_repository.get_today_session_id(topic_id)
            if session_id is not None:
                session_repository.update_session_weak_areas(session_id, weak_json_str)
                session_repository.update_session_student_weak_areas(session_id, weak_json_str)
            topic_repository.update_topic_weak_areas(topic_id, weak_json_str)

            topic_name = state.get("current_topic_name") or "topic"
            all_unlogged = topic_repository.get_active_unlogged_topics_today()
            proposed_slots = state.get("proposed_slots") or []
            planned_names = {s["topic"] for s in proposed_slots}
            if planned_names:
                remaining = [t for t in all_unlogged if t["name"] in planned_names]
            else:
                due_names = {t["name"] for t in _sm2.get_due_topics()}
                remaining = [t for t in all_unlogged if t["name"] in due_names]

            if not remaining:
                completion_msg = f"✅ {topic_name} logged. All done for today! 💪"
            else:
                bullet_list = "\n".join(f"• {t['name']}" for t in remaining)
                completion_msg = f"✅ {topic_name} logged. Still unlogged:\n{bullet_list}\n\nPress /done when you're ready."

            return {
                "messages": [completion_msg],
                "weak_areas_first_answer": first_text,
                "weak_areas_topic_type": topic_type,
                "pending_message_id": None,
                "has_unlogged_sessions": False,
            }

        else:  # behavioral
            second_msg_id = _telegram.send_inline_buttons(
                "What felt weak?",
                [("Delivery", "Delivery"), ("Quantification", "Quantification"),
                 ("Structure", "Structure"), ("All of the above", "All of the above"),
                 ("Nothing", "Nothing")],
            )

        return {
            "weak_areas_first_answer": first_text,
            "weak_areas_topic_type": topic_type,
            "pending_message_id": second_msg_id,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("log_weak_areas failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to log weak areas: {e}"], "payload": None}


# ---------------------------------------------------------------------------
# Node: log_weak_areas_q2
# ---------------------------------------------------------------------------

def log_weak_areas_q2(state: AgentState) -> AgentState:
    """Holds interrupt 2. Builds structured JSON and computes remaining topics."""
    try:
        chat_id = state.get("chat_id")
        second_msg_id = state.get("pending_message_id")
        topic_id = state.get("current_topic_id")
        first_text = state.get("weak_areas_first_answer") or ""

        # ── INTERRUPT 2 at top — no side effects before this ────────────────
        second_answer = interrupt("waiting for second feedback")

        if second_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, second_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        second_text = (second_answer or "").strip()

        if not topic_id:
            return {
                "pending_message_id": None,
                "weak_areas_first_answer": None,
                "weak_areas_topic_type": None,
                "has_unlogged_sessions": False,
                "current_topic_id": None,
                "current_topic_name": None,
            }

        session_id = session_repository.get_today_session_id(topic_id)

        topic_type = state.get("weak_areas_topic_type") or "conceptual"

        if topic_type == "dsa":
            breakdown_text = first_text
            problems_text = second_text
            weak_json = {
                "problems": null_if_skip(problems_text),
                "breakdown": breakdown(breakdown_text, _DSA_ALL),
            }
        elif topic_type == "system_design":
            scenario_text = first_text
            breakdown_text = second_text
            weak_json = {
                "scenario": null_if_skip(scenario_text),
                "breakdown": breakdown(breakdown_text, _SYSDESIGN_ALL),
            }
        elif topic_type == "conceptual":
            unclear_text = first_text
            weak_json = {
                "unclear": null_if_skip(unclear_text),
            }
        else:  # behavioral
            story_text = first_text
            breakdown_text = second_text
            weak_json = {
                "story": null_if_skip(story_text),
                "breakdown": breakdown(breakdown_text, _BEHAVIORAL_ALL),
            }

        weak_json_str = json.dumps(weak_json)

        if session_id is not None:
            session_repository.update_session_weak_areas(session_id, weak_json_str)
            session_repository.update_session_student_weak_areas(session_id, weak_json_str)
        topic_repository.update_topic_weak_areas(topic_id, weak_json_str)

        topic_name = state.get("current_topic_name") or "topic"
        all_unlogged = topic_repository.get_active_unlogged_topics_today()

        proposed_slots = state.get("proposed_slots") or []
        planned_names = {s["topic"] for s in proposed_slots}
        if planned_names:
            remaining = [t for t in all_unlogged if t["name"] in planned_names]
        else:
            due_names = {t["name"] for t in _sm2.get_due_topics()}
            remaining = [t for t in all_unlogged if t["name"] in due_names]

        if not remaining:
            msg = f"✅ {topic_name} logged. All done for today! 💪"
        else:
            bullet_list = "\n".join(f"• {t['name']}" for t in remaining)
            msg = f"✅ {topic_name} logged. Still unlogged:\n{bullet_list}\n\nPress /done when you're ready."

        return {
            "messages": [msg],
            "payload": None,
            "pending_message_id": None,
            "weak_areas_first_answer": None,
            "weak_areas_topic_type": None,
            "has_unlogged_sessions": False,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("log_weak_areas_q2 failed: %s", e, exc_info=True)
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

        # Clean up any leftover button message from a previous abandoned /pick flow
        old_msg_id = state.get("pending_message_id")
        if old_msg_id is not None and chat_id:
            try:
                _telegram.remove_buttons(chat_id, old_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        rows = topic_repository.get_inactive_topics_tier1_or2()
        tier1 = [r for r in rows if r["tier"] == 1]
        available = tier1 if tier1 else [r for r in rows if r["tier"] == 2]

        if not available:
            return {"messages": ["No inactive topics available to start studying."], "pending_message_id": None}

        categories = sorted(set(
            r["name"].split(" - ")[0] if " - " in r["name"] else "Other"
            for r in available
        ))

        buttons = [(c, f"category:{c}") for c in categories]
        cat_msg_id = _telegram.send_inline_buttons("Which category?", buttons)

        # Send buttons and return — interrupt lives in study_topic_category
        return {
            "pending_message_id": cat_msg_id,
            "study_topic_category": None,  # clear any stale category
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("study_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to load topics: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: study_topic_category
# ---------------------------------------------------------------------------

def study_topic_category(state: AgentState) -> AgentState:
    """interrupt() is first — no duplicate category prompt on resume."""
    try:
        chat_id = state.get("chat_id")
        cat_msg_id = state.get("pending_message_id")

        # Interrupt at start — no side effects above this line
        category_payload = interrupt("waiting for category selection")

        # Remove category buttons after resume (idempotent)
        if cat_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, cat_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)
        # If the resume payload looks like a command (e.g. user typed /pick instead
        # of tapping a button), re-send the category picker and prompt the user to
        # use the buttons. This avoids treating a command string as a category
        # name (which produced messages like "No topics found in category '/pick'").
        if not category_payload or (isinstance(category_payload, str) and category_payload.startswith("/")):
            # Recompute available categories and re-send the picker
            rows = topic_repository.get_inactive_topics_tier1_or2()
            tier1 = [r for r in rows if r["tier"] == 1]
            available = tier1 if tier1 else [r for r in rows if r["tier"] == 2]

            categories = sorted(set(
                r["name"].split(" - ")[0] if " - " in r["name"] else "Other"
                for r in available
            ))
            buttons = [(c, f"category:{c}") for c in categories]
            try:
                new_cat_msg_id = _telegram.send_inline_buttons("Which category?", buttons)
            except RuntimeError as e:
                if "timed out" in str(e).lower():
                    logger.warning("send_inline_buttons timed out: %s", e)
                    return {"messages": ["⚠️ Timed out while sending the category list. Please retry /pick."], "pending_message_id": None}
                raise

            return {"messages": ["Please choose a category using the buttons."], "pending_message_id": new_cat_msg_id}

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
            return {"messages": [f"No topics found in category '{category}'."], "pending_message_id": None}

        buttons = [(r["name"], f"subtopic_id:{r['id']}") for r in subtopic_rows]
        try:
            subtopic_msg_id = _telegram.send_inline_buttons("Which topic?", buttons)
        except RuntimeError as e:
            if "timed out" in str(e).lower():
                logger.warning("send_inline_buttons timed out: %s", e)
                return {"messages": ["⚠️ Timed out while sending the topic list. Please retry /pick."], "pending_message_id": None}
            raise

        # Send subtopic buttons and return — interrupt lives in study_topic_confirm
        return {
            "study_topic_category": category,
            "pending_message_id": subtopic_msg_id,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("study_topic_category failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to load subtopics: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: study_topic_confirm
# ---------------------------------------------------------------------------

def study_topic_confirm(state: AgentState) -> AgentState:
    """interrupt() is first — no duplicate subtopic list on resume."""
    try:
        chat_id = state.get("chat_id")
        subtopic_msg_id = state.get("pending_message_id")

        # Interrupt at start — no side effects above this line
        subtopic_payload = interrupt("waiting for subtopic selection")

        # Remove subtopic buttons after resume (idempotent)
        if subtopic_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, subtopic_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        try:
            topic_id = int((subtopic_payload or "")[len("subtopic_id:"):])
        except (ValueError, TypeError):
            return {"messages": ["⚠️ Invalid topic selection."], "pending_message_id": None}

        resolved_name = topic_service.get_topic_name_by_id(topic_id)
        if resolved_name is None:
            return {"messages": ["⚠️ Topic not found."], "pending_message_id": None}

        updated = topic_repository.set_topic_in_progress(resolved_name)
        if not updated:
            return {"messages": [f"⚠️ Topic '{resolved_name}' not found or already in progress."], "pending_message_id": None}

        return {
            "messages": [
                f"✅ {resolved_name} added to In Progress. "
                "It will be booked on your calendar tomorrow morning."
            ],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("study_topic_confirm failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to set topic in progress: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: activate_topic
# ---------------------------------------------------------------------------

def activate_topic(state: AgentState) -> AgentState:
    try:
        topics = topic_service.get_in_progress_topics()

        if not topics:
            return {"messages": ["No topics currently in progress."]}

        buttons = [(t["name"], f"studied:{t['id']}") for t in topics]
        topic_msg_id = _telegram.send_inline_buttons("Which topic are you ready to be tested on?", buttons)

        # Send buttons and return — interrupt lives in graduate_topic
        # Clear stale messages so route_from_activate_topic doesn't mis-route to output
        return {"pending_message_id": topic_msg_id, "messages": []}

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("activate_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to load in-progress topics: {e}"]}


# ---------------------------------------------------------------------------
# Node: graduate_topic
# ---------------------------------------------------------------------------

def graduate_topic(state: AgentState) -> AgentState:
    """interrupt() is first — no duplicate topic list on resume."""
    try:
        chat_id = state.get("chat_id")
        topic_msg_id = state.get("pending_message_id")

        # Interrupt at start — no side effects above this line
        studied_payload = interrupt("waiting for topic selection")

        # Remove buttons after resume (idempotent)
        if topic_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, topic_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        if not isinstance(studied_payload, str) or not studied_payload.startswith("studied:"):
            return {"messages": ["⚠️ Invalid topic selection."], "pending_message_id": None}

        try:
            topic_id = int(studied_payload[len("studied:"):])
        except ValueError:
            return {"messages": ["⚠️ Invalid topic id."], "pending_message_id": None}

        topic_name = topic_service.graduate_topic(topic_id)
        return {
            "messages": [
                f"✅ {topic_name} graduated to active. "
                "First SM-2 review scheduled for tomorrow."
            ],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("graduate_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to graduate topic: {e}"], "pending_message_id": None}
