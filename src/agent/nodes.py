"""
LangGraph node implementations for the Learning Manager agent.

Each node receives an AgentState and returns a partial state update dict.
All exceptions are caught and surfaced as user-friendly messages in state.
"""

import json
import pytz
import yaml
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import logging
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from src.agent.formatting import (
    format_time,
    local_datetime_str,
)
from src.agent.plan_message import (
    append_calendar_lines,
    build_evening_preview_state,
    pack_mock_slots,
)
from src.agent.slot_builders import (
    build_in_progress_study_slots,
    build_missing_study_events,
    get_prebooked_topics,
    rebook_study_events,
)

from src.agent import messages
from src.agent.state import AgentState
from src.agent.weak_areas_parser import null_if_skip, breakdown, _DSA_ALL, _SYSDESIGN_ALL, _BEHAVIORAL_ALL

from src.core import gap_finder as _gap_finder
from src.core import sm2 as _sm2
from src.integrations import claude_api as _claude
from src.integrations import gcal as _gcal
from src.integrations import telegram_client as _telegram
from src.repositories import session_repository, topic_repository
from src.services import topic_service

_CONFIG_PATH = Path(__file__).parents[2] / "config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)
logger = logging.getLogger(__name__)



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
            in_progress_topics = topic_repository.get_in_progress_topic_names()
            evening_state = build_evening_preview_state(
                target_date, events, timed_events, due_topics, config,
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
        min_window_minutes = config.get("min_window_minutes", 25)
        proposed_topic, proposed_slot, proposed_slots = pack_mock_slots(
            target_date,
            free_windows,
            available_topics,
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
        msg_id = _telegram.send_buttons(message, messages.BOOKING_BUTTONS)
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

        msg_id = _telegram.send_buttons(*messages.duration_picker())
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
                "messages": [messages.nothing_due()],
                "duration_min": duration_min,
                "pending_message_id": None,
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

        status = messages.generating_brief(topic["name"], duration_min)
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
            msg_id = _telegram.send_buttons(brief, messages.BOOKING_BUTTONS)
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
    config = _load_config()
    tz = pytz.timezone(config["timezone"])

    # Book in-progress [Study] events first (only when user confirmed, not on Skip)
    booked_study: list[str] = []
    try:
        in_progress_topics = topic_repository.get_in_progress_topic_names()
        events_today = _gcal.get_events(today)
        timed_events_today = [e for e in events_today if "dateTime" in e.get("start", {})]
        from src.agent.slot_builders import is_topic_in_summary
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
        try:
            _telegram.send_message(messages.booked_sessions(booked_study, booked))
        except Exception as e:
            logger.warning("[book_events] Confirmation send failed: %s", e, exc_info=True)
    else:
        try:
            _telegram.send_message(messages.BOOKING_FAILED)
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
            return {"messages": [messages.no_sessions_to_log()], "has_unlogged_sessions": False}

        if len(unlogged) >= 2:
            logger.info("done_parser: %d unlogged topics — sending picker", len(unlogged))
            # One button per row so long topic names don't get truncated
            picker_msg_id = _telegram.send_inline_buttons(*messages.topic_picker(unlogged))
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
        rating_msg_id = _telegram.send_buttons(*messages.rating_prompt(topic_name))

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

        rating_msg_id = _telegram.send_buttons(*messages.rating_prompt(topic_name))

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
        _sm2.update_topic_after_session(topic_id=topic_id, quality=sm2_quality)

        # Send type-specific first structured feedback question
        topic_type = topic_repository.get_topic_type_by_id(topic_id) or "conceptual"
        if topic_type == "dsa":
            first_msg_id = _telegram.send_inline_buttons(*messages.weak_areas_q1_dsa())
        elif topic_type == "system_design":
            first_msg_id = _telegram.send_buttons(*messages.weak_areas_q1_system_design())
        elif topic_type == "conceptual":
            first_msg_id = _telegram.send_buttons(*messages.weak_areas_q1_conceptual())
        else:  # behavioral
            first_msg_id = _telegram.send_buttons(*messages.weak_areas_q1_behavioral())

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


def _build_completion_message(topic_name: str, proposed_slots: list[dict]) -> str:
    """Return the post-log completion message with any remaining unlogged topics.

    Args:
        topic_name: Name of the topic just logged.
        proposed_slots: Proposed slots from state, used to scope remaining topics.

    Returns:
        Completion message string.
    """
    all_unlogged = topic_repository.get_active_unlogged_topics_today()
    planned_names = {s["topic"] for s in proposed_slots}
    if planned_names:
        remaining = [t for t in all_unlogged if t["name"] in planned_names]
    else:
        due_names = {t["name"] for t in _sm2.get_due_topics()}
        remaining = [t for t in all_unlogged if t["name"] in due_names]

    if not remaining:
        return messages.completion_all_done(topic_name)
    return messages.completion_still_unlogged(topic_name, remaining)


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
            second_msg_id = _telegram.send_buttons(*messages.weak_areas_q2_dsa())
        elif topic_type == "system_design":
            second_msg_id = _telegram.send_inline_buttons(*messages.weak_areas_q2_system_design())
        elif topic_type == "conceptual":
            weak_json_str = json.dumps({"unclear": null_if_skip(first_text)})
            session_id = session_repository.get_today_session_id(topic_id)
            if session_id is not None:
                session_repository.update_session_weak_areas(session_id, weak_json_str)
                session_repository.update_session_student_weak_areas(session_id, weak_json_str)
            topic_repository.update_topic_weak_areas(topic_id, weak_json_str)

            topic_name = state.get("current_topic_name") or "topic"
            proposed_slots = state.get("proposed_slots") or []
            completion_msg = _build_completion_message(topic_name, proposed_slots)

            return {
                "messages": [completion_msg],
                "weak_areas_first_answer": first_text,
                "weak_areas_topic_type": topic_type,
                "pending_message_id": None,
                "has_unlogged_sessions": False,
            }

        else:  # behavioral
            second_msg_id = _telegram.send_inline_buttons(*messages.weak_areas_q2_behavioral())

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
        proposed_slots = state.get("proposed_slots") or []
        msg = _build_completion_message(topic_name, proposed_slots)

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
            return {"messages": [messages.no_inactive_topics()], "pending_message_id": None}

        categories = sorted(set(
            r["name"].split(" - ")[0] if " - " in r["name"] else "Other"
            for r in available
        ))

        buttons = [(c, f"category:{c}") for c in categories]
        cat_msg_id = _telegram.send_inline_buttons(messages.CATEGORY_PICKER_PROMPT, buttons)

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
                new_cat_msg_id = _telegram.send_inline_buttons(messages.CATEGORY_PICKER_PROMPT, buttons)
            except RuntimeError as e:
                if "timed out" in str(e).lower():
                    logger.warning("send_inline_buttons timed out: %s", e)
                    return {"messages": ["⚠️ Timed out while sending the category list. Please retry /pick."], "pending_message_id": None}
                raise

            return {"messages": [messages.CATEGORY_PICKER_FALLBACK], "pending_message_id": new_cat_msg_id}

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
            subtopic_msg_id = _telegram.send_inline_buttons(messages.TOPIC_PICKER_PROMPT, buttons)
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
                messages.topic_added_to_in_progress(resolved_name)
            ],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("study_topic_confirm failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to set topic in progress: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: discuss_parser
# ---------------------------------------------------------------------------

def _weak_area_keys(raw: str | None) -> list[str]:
    """Extract focus-area labels from a stored ``topics.weak_areas`` value.

    ``topics.weak_areas`` is written by the /done flow as a structured dict
    where top-level keys are schema labels (``"unclear"``, ``"breakdown"``,
    ``"problems"``, ``"scenario"``, ``"story"``) and the *values* hold the
    meaningful content (e.g. ``{"unclear": "CAP theorem vs PACELC"}`` or
    ``{"breakdown": "Edge case, Time complexity"}``).  Surfacing the values
    gives Diego actionable focus areas; surfacing the keys would only show
    unhelpful structural labels.

    Note: this is distinct from ``discuss_service._parse_weak_area_keys``,
    which reads ``sessions.teacher_weak_areas`` — a field where the *keys*
    are the identifiers used for repetition detection.

    Args:
        raw: Raw ``weak_areas`` value as stored in the topics table.

    Returns:
        List of non-empty string values; empty list when input is None,
        empty, or not a JSON object.
    """
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return [raw.strip()]
        return [v for v in parsed.values() if v and isinstance(v, str)]
    except Exception:
        return [raw.strip()]


def discuss_parser(state: AgentState) -> AgentState:
    logger.info("discuss_parser: entered")

    try:
        chat_id = state.get("chat_id")

        # Clean up stale button message from a previous abandoned flow
        old_msg_id = state.get("pending_message_id")
        if old_msg_id is not None and chat_id:
            try:
                _telegram.remove_buttons(chat_id, old_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        topics = topic_repository.get_in_progress_and_active_topics()

        if not topics:
            return {
                "messages": ["No topics in progress or active to discuss."],
                "pending_message_id": None,
            }

        if len(topics) >= 2:
            logger.info("discuss_parser: %d topics — sending picker", len(topics))
            buttons = [(t["name"], f"discuss_topic:{t['id']}") for t in topics]
            picker_msg_id = _telegram.send_inline_buttons("Which topic are you discussing?", buttons)
            return {
                "messages": [],
                "pending_message_id": picker_msg_id,
            }

        # Exactly one topic — handle inline, no interrupt needed
        topic = topics[0]
        topic_id = topic["id"]
        topic_name = topic["name"]

        topic_repository.set_topic_discussing(topic_id)
        session_count = session_repository.get_discuss_session_count(topic_id)
        context = topic_repository.get_topic_context(topic_id)
        topic_type = context.get("topic_type") or "conceptual"
        weak_areas = _weak_area_keys(context.get("weak_areas"))

        msg = messages.discuss_session_ready(topic_name, topic_type, weak_areas, session_count + 1)
        return {
            "messages": [msg],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("discuss_parser failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Discuss flow failed: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: start_discuss
# ---------------------------------------------------------------------------

def start_discuss(state: AgentState) -> AgentState:
    """interrupt() is first — on resume, receives the selected topic id payload."""
    try:
        chat_id = state.get("chat_id")
        picker_msg_id = state.get("pending_message_id")

        # Interrupt at start — no side effects above this line
        selected_payload = interrupt("waiting for topic selection")

        # Remove picker buttons after resume (idempotent)
        if picker_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, picker_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        # If the user typed a command instead of tapping a button, re-send the
        # picker so they can see their options — mirrors study_topic_category.
        if not selected_payload or (
            isinstance(selected_payload, str) and selected_payload.startswith("/")
        ):
            topics = topic_repository.get_in_progress_and_active_topics()
            if not topics:
                return {
                    "messages": ["No topics in progress or active to discuss."],
                    "pending_message_id": None,
                }
            buttons = [(t["name"], f"discuss_topic:{t['id']}") for t in topics]
            try:
                new_picker_id = _telegram.send_inline_buttons(
                    "Which topic are you discussing?", buttons
                )
            except RuntimeError as e:
                if "timed out" in str(e).lower():
                    logger.warning("start_discuss: send_inline_buttons timed out: %s", e)
                    return {
                        "messages": ["⚠️ Timed out sending the topic list. Type /discuss to try again."],
                        "pending_message_id": None,
                    }
                raise
            return {
                "messages": [messages.DISCUSS_PICKER_FALLBACK],
                "pending_message_id": new_picker_id,
            }

        if not selected_payload.startswith("discuss_topic:"):
            return {"messages": ["⚠️ Invalid topic selection."], "pending_message_id": None}

        try:
            topic_id = int(selected_payload[len("discuss_topic:"):])
        except ValueError:
            return {"messages": ["⚠️ Invalid topic id."], "pending_message_id": None}

        topic_name = topic_repository.get_topic_name_by_id(topic_id)
        if topic_name is None:
            return {"messages": ["⚠️ Topic not found."], "pending_message_id": None}

        topic_repository.set_topic_discussing(topic_id)
        session_count = session_repository.get_discuss_session_count(topic_id)
        context = topic_repository.get_topic_context(topic_id)
        topic_type = context.get("topic_type") or "conceptual"
        weak_areas = _weak_area_keys(context.get("weak_areas"))

        msg = messages.discuss_session_ready(topic_name, topic_type, weak_areas, session_count + 1)
        return {
            "messages": [msg],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("start_discuss failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to start discuss session: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: notify_discuss_ready
# ---------------------------------------------------------------------------

def notify_discuss_ready(state: AgentState) -> AgentState:
    """Send the discuss-readiness activation buttons.

    Reads ``current_topic_name`` from state (stored by
    ``assess_discuss_readiness`` when it invokes the graph).  The interrupt
    lives in ``await_discuss_activation`` — this node is side-effect only.

    Args:
        state: Current agent state.

    Returns:
        Partial state update with ``pending_message_id`` set and
        ``messages`` cleared.
    """
    try:
        topic_name = state.get("current_topic_name") or "topic"
        msg_id = _telegram.send_buttons(
            messages.discuss_ready_prompt(topic_name),
            messages.DISCUSS_ACTIVATION_BUTTONS,
        )
        return {"pending_message_id": msg_id, "messages": []}

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("notify_discuss_ready failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Could not send activation prompt: {e}"]}


# ---------------------------------------------------------------------------
# Node: await_discuss_activation
# ---------------------------------------------------------------------------

def await_discuss_activation(state: AgentState) -> AgentState:
    """Wait for the user to confirm or defer discuss-readiness activation.

    ``interrupt()`` is the very first statement so re-runs on resume are
    side-effect-free.

    Handles two resume values:
    - ``"Yes, activate"`` — activates the topic via
      ``topic_repository.activate_topic_from_discuss`` with
      ``next_review = today`` so SM-2 schedules the first mock immediately.
    - Anything else (``"Not yet"``) — sends a no-rush message without
      changing the topic status.

    Args:
        state: Current agent state (must contain ``current_topic_id`` and
            ``current_topic_name``).

    Returns:
        Partial state update with ``messages`` set and
        ``pending_message_id`` cleared.
    """
    try:
        chat_id = state.get("chat_id")
        msg_id = state.get("pending_message_id")
        topic_id = state.get("current_topic_id")
        topic_name = state.get("current_topic_name") or "topic"

        # Interrupt at start — no side effects above this line
        payload = interrupt("waiting for activation confirmation")

        if msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        if (payload or "").strip().lower() == "yes, activate":
            if topic_id is None:
                return {
                    "messages": ["⚠️ No topic selected for activation."],
                    "pending_message_id": None,
                }
            topic_repository.activate_topic_from_discuss(topic_id)
            return {
                "messages": [messages.discuss_activated(topic_name)],
                "pending_message_id": None,
            }

        # "Not yet" or any other payload
        return {
            "messages": [messages.discuss_not_yet()],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("await_discuss_activation failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Activation failed: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: activate_topic
# ---------------------------------------------------------------------------

def activate_topic(state: AgentState) -> AgentState:
    try:
        topics = topic_service.get_in_progress_topics()

        if not topics:
            return {"messages": [messages.no_topics_in_progress()]}

        buttons = [(t["name"], f"studied:{t['id']}") for t in topics]
        topic_msg_id = _telegram.send_inline_buttons(messages.ACTIVATE_TOPIC_PROMPT, buttons)

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
    """interrupt() is first — no duplicate topic list on resume.

    Applies two pre-activation guards before promoting the topic:

    - **Hard block** (``discussing`` status): the topic is mid-discuss; the
      user must wait for Claude's readiness assessment.
    - **Soft guard** (``in_progress``, zero discuss sessions): warns the user
      they haven't discussed this topic yet and offers ["Yes, activate",
      "Do discuss first"]; routes to ``confirm_graduate`` for the response.

    All other cases proceed with normal activation.
    """
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

        topic_name = topic_repository.get_topic_name_by_id(topic_id)
        if topic_name is None:
            return {"messages": ["⚠️ Topic not found."], "pending_message_id": None}

        # --- Pre-activation guards ---
        status = topic_repository.get_topic_status_by_id(topic_id)

        if status == "discussing":
            # Hard block: activating mid-discuss would break the readiness flow.
            return {
                "messages": [messages.activate_discussing_block(topic_name)],
                "pending_message_id": None,
            }

        if status == "in_progress":
            discuss_count = session_repository.get_discuss_session_count(topic_id)
            if discuss_count == 0:
                # Soft guard: topic has never been discussed; ask for confirmation.
                prompt, buttons = messages.activate_no_discuss_warning(topic_name)
                guard_msg_id = _telegram.send_buttons(prompt, buttons)
                return {
                    "current_topic_id": topic_id,
                    "current_topic_name": topic_name,
                    "pending_message_id": guard_msg_id,
                    "messages": [],
                }

        # Normal activation (in_progress with prior discuss sessions, or any other status).
        topic_name = topic_service.graduate_topic(topic_id)
        return {
            "messages": [messages.topic_graduated(topic_name)],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("graduate_topic failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to graduate topic: {e}"], "pending_message_id": None}


# ---------------------------------------------------------------------------
# Node: confirm_graduate
# ---------------------------------------------------------------------------

def confirm_graduate(state: AgentState) -> AgentState:
    """Handle the soft-guard response after graduate_topic sends a warning.

    ``interrupt()`` is the very first statement so re-runs on resume are
    side-effect-free.

    Handles two resume values:
    - ``"Yes, activate"`` — proceeds with normal graduation (same as
      ``/activate`` happy path; ``next_review = tomorrow``).
    - ``"Do discuss first"`` — sets the topic to ``discussing`` status and
      sends a discuss session-ready message without activating.

    Args:
        state: Current agent state (must contain ``current_topic_id`` and
            ``current_topic_name`` set by ``graduate_topic``).

    Returns:
        Partial state update with ``messages`` set and
        ``pending_message_id`` cleared.
    """
    try:
        chat_id = state.get("chat_id")
        guard_msg_id = state.get("pending_message_id")
        topic_id = state.get("current_topic_id")
        topic_name = state.get("current_topic_name") or "topic"

        # Interrupt at start — no side effects above this line
        payload = interrupt("waiting for activate confirmation")

        if guard_msg_id and chat_id:
            try:
                _telegram.remove_buttons(chat_id, guard_msg_id)
            except Exception as _e:
                logger.debug("remove_buttons silently failed: %s", _e)

        if topic_id is None:
            return {"messages": ["⚠️ No topic selected."], "pending_message_id": None}

        if (payload or "").strip().lower() == "yes, activate":
            topic_name = topic_service.graduate_topic(topic_id)
            return {
                "messages": [messages.topic_graduated(topic_name)],
                "pending_message_id": None,
            }

        # "Do discuss first" — mirror discuss_parser's single-topic path.
        topic_repository.set_topic_discussing(topic_id)
        session_count = session_repository.get_discuss_session_count(topic_id)
        context = topic_repository.get_topic_context(topic_id)
        topic_type = context.get("topic_type") or "conceptual"
        weak_areas = _weak_area_keys(context.get("weak_areas"))
        msg = messages.discuss_session_ready(topic_name, topic_type, weak_areas, session_count + 1)
        return {
            "messages": [msg],
            "pending_message_id": None,
        }

    except Exception as e:
        if isinstance(e, GraphInterrupt):
            raise
        logger.error("confirm_graduate failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Activation failed: {e}"], "pending_message_id": None}
