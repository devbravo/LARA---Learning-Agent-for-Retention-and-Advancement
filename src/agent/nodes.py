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
    trigger: str               # "daily" | "evening" | "on_demand" | "done" | "confirm" | "rate" | "weak_areas"
    chat_id: int
    message_id: int | None              # Telegram message_id of the confirm keyboard
    duration_min: int | None
    proposed_topic: str | None          # single-slot flow (on_demand)
    proposed_slot: dict | None          # single-slot flow (on_demand)
    proposed_slots: list[dict] | None   # multi-slot flow (daily_planning)
    has_study_plan: bool                # False → skip confirm, go straight to output
    preview_only: bool                  # True → evening briefing, route to output not confirm
    quality_score: int | None
    messages: list[str]        # outbound Telegram messages
    awaiting_weak_areas: bool
    current_topic_id: int | None
    current_topic_name: str | None
    study_topic_category: str | None    # selected category in /study_topic flow, e.g. "DSA"
    pending_subtopic_message_id: int | None  # message_id of the last sent subtopic list (for cleanup)

# ---------------------------------------------------------------------------
# Node: router
# ---------------------------------------------------------------------------

def router(state: AgentState) -> AgentState:
    """Validate the incoming trigger before graph routing.

    Args:
        state: Current partial agent state.

    Returns:
        Empty update when trigger is present, otherwise an error message payload.
    """
    trigger = state.get("trigger", "")
    if not trigger:
        return {"messages": ["⚠️ No trigger set — cannot route."]}
    return {}


def route_from_router(state: AgentState) -> str:
    """Map a trigger string to the next graph node name.

    Args:
        state: Current partial agent state.

    Returns:
        Graph node key used by conditional edges.
    """
    trigger = state.get("trigger", "")
    mapping = {
        "daily":                "daily_planning",
        "evening":              "daily_planning",
        "on_demand":            "on_demand",
        "done":                 "done_parser",
        "confirm":              "output",
        "rate":                 "log_session",
        "weak_areas":           "log_weak_areas",
        "study_topic":          "study_topic",
        "study_topic_category": "study_topic_category",
        "study_topic_confirm":  "study_topic_confirm",
    }
    return mapping.get(trigger, "output")


def route_from_daily_planning(state: AgentState) -> str:
    """Pick the next node after ``daily_planning``.

    Args:
        state: State returned by ``daily_planning``.

    Returns:
        ``output`` for preview/no-plan flows, otherwise ``confirm``.
    """
    if state.get("preview_only"):
        return "output"
    return "confirm" if state.get("has_study_plan") else "output"


# ---------------------------------------------------------------------------
# Node: calendar_reader
# ---------------------------------------------------------------------------

def calendar_reader(state: AgentState) -> AgentState:
    """Fetch today's calendar events and report count.

    Args:
        state: Current partial agent state.

    Returns:
        State update with a status message.
    """
    try:
        events = _gcal.get_events(date.today())
        return {"messages": state.get("messages", []) + [f"📅 Fetched {len(events)} events"]}
    except Exception as e:
        return {"messages": state.get("messages", []) + [f"⚠️ Calendar read failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: sm2_engine
# ---------------------------------------------------------------------------

def sm2_engine(state: AgentState) -> AgentState:
    """Fetch due topics from SM-2 and report count.

    Args:
        state: Current partial agent state.

    Returns:
        State update with a status message.
    """
    try:
        topics = _sm2.get_due_topics()
        return {"messages": state.get("messages", []) + [f"🧠 {len(topics)} topics due"]}
    except Exception as e:
        return {"messages": state.get("messages", []) + [f"⚠️ SM-2 fetch failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: gap_finder
# ---------------------------------------------------------------------------

def gap_finder(state: AgentState) -> AgentState:
    """Compute today's free windows and report count.

    Args:
        state: Current partial agent state.

    Returns:
        State update with a status message.
    """
    try:
        config = _load_config()
        events = _gcal.get_events(date.today())
        windows = _gap_finder.find_free_windows(events, date.today(), config)
        return {"messages": state.get("messages", []) + [f"🕐 {len(windows)} free windows found"]}
    except Exception as e:
        return {"messages": state.get("messages", []) + [f"⚠️ Gap finder failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: daily_planning
# ---------------------------------------------------------------------------

def daily_planning(state: AgentState) -> AgentState:
    """Build either the morning plan or evening preview payload.

    Args:
        state: Current partial agent state containing the trigger.

    Returns:
        Partial ``AgentState`` containing generated messages and planning fields.
    """
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

        # --- Morning briefing: full interactive plan for today ---
        _TZ = pytz.timezone(config["timezone"])
        after_time = datetime.now(_TZ).time()

        # Fetch in_progress topics BEFORE computing free windows so their
        # [Study] blocks are treated as busy by gap_finder and don't overlap [Mock] slots.
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

        # Auto-rebook [Study] events for in_progress topics
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
        if has_study_plan:
            lines.append("Confirm these mock interview blocks?")
        else:
            lines.append("No mock interview windows available today — calendar fully booked.")

        message = "\n".join(lines)
        morning_payload: object = {
            "proposed_topic": proposed_topic,
            "proposed_slot": proposed_slot,
            "proposed_slots": proposed_slots if proposed_slots else None,
            "has_study_plan": has_study_plan,
            "preview_only": False,
            "messages": [message],
        }
        morning_state = cast(AgentState, morning_payload)
        return morning_state

    except Exception as e:
        error_payload: object = {"messages": [f"⚠️ Daily briefing failed: {e}"]}
        error_state = cast(AgentState, error_payload)
        return error_state


# ---------------------------------------------------------------------------
# Node: on_demand
# ---------------------------------------------------------------------------

def on_demand(state: AgentState) -> AgentState:
    """Prepare the on-demand study flow state.

    Args:
        state: Current partial agent state.

    Returns:
        Partial state with selected topic and a status/brief-generation message.
    """
    try:
        due_topics = _sm2.get_due_topics()
        topic = due_topics[0] if due_topics else None
        if topic is None:
            return {"messages": ["🎉 Nothing due for review right now — enjoy your break!"]}

        duration_min = state.get("duration_min") or 30
        return {
            "proposed_topic": topic["name"],
            "proposed_slot": None,
            "proposed_slots": None,
            "messages": [f"📚 Generating a {duration_min} min brief for {topic['name']}…"],
        }

    except Exception as e:
        return {"messages": [f"⚠️ On-demand session failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: done_parser
# ---------------------------------------------------------------------------

def done_parser(state: AgentState) -> AgentState:
    """Start the done-flow by prompting a rating for the next unlogged topic.

    Args:
        state: Current partial agent state.

    Returns:
        State update with ``current_topic_id`` and ``current_topic_name`` when
        a loggable topic is found, otherwise an empty update.
    """
    logger.info("done_parser: entered")

    try:
        proposed_slots = state.get("proposed_slots") or []
        if not proposed_slots:
            _telegram.send_message("No study sessions were planned today.")
            return {}

        # Find topics already logged today
        logged_names = session_repository.get_logged_topic_names_for_today()

        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]
        if not unlogged:
            _telegram.send_message("All sessions already logged for today.")
            return {}

        slot = unlogged[0]
        topic_name = slot["topic"]

        topic_id = topic_repository.get_topic_id_by_name(topic_name)
        if topic_id is None:
            _telegram.send_message(f"⚠️ Topic '{topic_name}' not found in database.")
            return {}

        # Detect mid-flow: buttons already sent for this topic but not yet tapped.
        # Avoid sending duplicate buttons — just remind the user.
        pending_name = state.get("current_topic_name")
        if pending_name == topic_name and not state.get("awaiting_weak_areas"):
            logger.info("done_parser: rating already pending for %s — sending reminder", topic_name)
            _telegram.send_message(
                f"⏳ Still waiting for your rating on <b>{topic_name}</b> — tap a button above."
            )
            return {}

        logger.info("done_parser: sending rating buttons for %s", topic_name)
        _telegram.send_buttons(f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"])
        return {
            "current_topic_id": topic_id,
            "current_topic_name": topic_name,
        }
    except Exception as e:
        logger.error("done_parser failed: %s", e, exc_info=True)
        try:
            _telegram.send_message(f"⚠️ Done flow failed: {e}")
        except Exception:
            pass
        return {}


# ---------------------------------------------------------------------------
# Node: generate_brief
# ---------------------------------------------------------------------------

def generate_brief(state: AgentState) -> AgentState:
    """Generate a study brief through Claude for the selected topic.

    Args:
        state: Current partial agent state.

    Returns:
        State update containing the generated brief or a fallback message.
    """
    try:
        topic = state.get("proposed_topic") or "General Study"
        duration_min = state.get("duration_min") or 30

        # Build context from weak_areas if available
        context = "General review"
        weak_areas = topic_repository.get_topic_weak_areas_by_name(topic)
        if weak_areas:
            context = f"Focus on weak areas: {weak_areas}"

        brief = _claude.generate_brief(
            topic=topic,
            duration_min=duration_min,
            context=context,
        )
        return {"messages": state.get("messages", []) + [brief]}

    except Exception as e:
        return {
            "messages": state.get("messages", []) + [
                f"⚠️ Could not generate brief: {e}\n"
                "Proceeding with general study plan."
            ]
        }


# ---------------------------------------------------------------------------
# Node: confirm
# ---------------------------------------------------------------------------

def confirm(state: AgentState) -> AgentState:
    """Send the assembled plan/brief to Telegram and wait for user action.

    Args:
        state: Current partial agent state.

    Returns:
        Always an empty state update; follow-up happens on next webhook trigger.
    """
    messages = state.get("messages") or []
    fallback_text = messages[-1] if messages else ""
    try:
        text = fallback_text or "Ready to study?"

        if state.get("proposed_slots"):
            # Daily briefing flow, needs confirmation before booking
            _telegram.send_buttons(text, ["Yes, book them", "Skip"])
        else:
            # Study picker flow, no booking needed, just send the brief
            _telegram.send_message(text)
    except Exception as e:
        try:
            _telegram.send_message(f"⚠️ Button send failed: {e}\n\n{fallback_text}")
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Node: log_session
# ---------------------------------------------------------------------------

def log_session(state: AgentState) -> AgentState:
    """Persist today's session rating and prompt for weak areas.

    Args:
        state: Current partial agent state.

    Returns:
        State update toggling ``awaiting_weak_areas`` on success, otherwise an
        error message payload.
    """
    try:
        topic_id = state.get("current_topic_id")
        topic_name = state.get("current_topic_name") or "topic"
        quality = state.get("quality_score") or 3

        if not topic_id:
            _telegram.send_message("⚠️ Cannot log session: missing topic.")
            return {}

        # Find duration from proposed_slots for this topic
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

        _telegram.send_buttons(
            "Any weak areas to note? Reply with text or tap Skip.",
            ["Skip"]
        )
        return {"awaiting_weak_areas": True}

    except Exception as e:
        return {"messages": [f"⚠️ Failed to log session: {e}"]}


# ---------------------------------------------------------------------------
# Node: log_weak_areas
# ---------------------------------------------------------------------------

def log_weak_areas(state: AgentState) -> AgentState:
    """Persist weak-area notes and continue or close the done-flow loop.

    Args:
        state: Current partial agent state.

    Returns:
        State update clearing ``awaiting_weak_areas`` and optionally pointing to
        the next topic to rate.
    """
    try:
        messages = state.get("messages") or []
        text = messages[0].strip() if messages else ""
        topic_id = state.get("current_topic_id")

        if not topic_id:
            _telegram.send_message("⚠️ Cannot log weak areas: missing topic.")
            return {"awaiting_weak_areas": False}

        session_id = session_repository.get_today_session_id(topic_id)

        if text:
            if session_id is not None:
                session_repository.update_session_weak_areas(session_id, text)
            topic_repository.update_topic_weak_areas(topic_id, text)
        else:
            # Skip — mark existing weak areas as resolved
            topic_repository.update_topic_weak_areas(topic_id, None)

        # Check for next unlogged slot
        proposed_slots = state.get("proposed_slots") or []
        logged_names = session_repository.get_logged_topic_names_for_today()

        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]

        if unlogged:
            next_topic_name = unlogged[0]["topic"]
            next_topic_id = topic_repository.get_topic_id_by_name(next_topic_name)

            if next_topic_id is not None:
                _telegram.send_buttons(
                    f"How did {next_topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"]
                )
                return {
                    "awaiting_weak_areas": False,
                    "current_topic_id": next_topic_id,
                    "current_topic_name": next_topic_name,
                }

        _telegram.send_message("All sessions logged for today. Great work! 💪")
        return {"awaiting_weak_areas": False}

    except Exception as e:
        logger.error("log_weak_areas failed: %s", e, exc_info=True)
        return {"messages": [f"⚠️ Failed to log weak areas: {e}"], "awaiting_weak_areas": False}


# ---------------------------------------------------------------------------
# Node: output
# ---------------------------------------------------------------------------

def output(state: AgentState) -> AgentState:
    """Send final outbound messages and book confirmed calendar events.

    Args:
        state: Current partial agent state.

    Returns:
        Always an empty update after side effects complete.
    """
    trigger = state.get("trigger", "")

    if trigger in ("rate", "weak_areas", "done"):
        messages = state.get("messages") or []
        if messages and messages[-1].startswith("⚠"):
            try:
                _telegram.send_message(messages[-1])
            except Exception as e:
                print(f"[output] Telegram send failed: {e}")
        return {}

    # --- Send final message for non-confirm triggers ---
    if trigger != "confirm":
        messages = state.get("messages") or []
        if messages:
            try:
                _telegram.send_message(messages[-1])
            except Exception as e:
                print(f"[output] Telegram send failed: {e}")
        return {}

    # --- Book calendar events on confirmation ---
    if trigger == "confirm":
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
                    print(f"[output] Calendar write failed for {slot.get('topic')}: {e}")
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
                print(f"[output] Calendar write failed: {e}")

        chat_id = state.get("chat_id")
        message_id = state.get("message_id")
        if chat_id and message_id:
            try:
                _telegram.remove_buttons(chat_id, message_id)
            except Exception as e:
                print(f"[output] remove_buttons failed: {e}")

        if booked:
            summary = "\n".join(f"  • {t}" for t in booked)
            try:
                _telegram.send_message(f"✅ Booked {len(booked)} mock session(s):\n{summary}")
            except Exception as e:
                print(f"[output] Confirmation send failed: {e}")

    return {}


# ---------------------------------------------------------------------------
# Node: study_topic
# ---------------------------------------------------------------------------

def study_topic(state: AgentState) -> AgentState:
    """Start the topic-picking flow by sending category buttons.

    Cleans up any previously pending subtopic list before presenting
    fresh category buttons, so abandoned /pick flows don't accumulate.

    Args:
        state: Current partial agent state.

    Returns:
        Empty state update; interaction continues through callback triggers.
    """
    try:
        # Clean up any leftover subtopic list from a previous abandoned /pick
        old_msg_id = state.get("pending_subtopic_message_id")
        if old_msg_id is not None:
            chat_id = state.get("chat_id")
            try:
                _telegram.remove_buttons(chat_id, old_msg_id)
            except Exception:
                pass  # already removed or expired — ignore
        rows = topic_repository.get_inactive_topics_tier1_or2()

        tier1 = [r for r in rows if r["tier"] == 1]
        available = tier1 if tier1 else [r for r in rows if r["tier"] == 2]

        if not available:
            _telegram.send_message("No inactive topics available to start studying.")
            return {}

        categories = sorted(set(
            r["name"].split(" - ")[0] if " - " in r["name"] else "Other"
            for r in available
        ))

        buttons = [(c, f"category:{c}") for c in categories]
        _telegram.send_inline_buttons("Which category?", buttons)
        return {"pending_subtopic_message_id": None}

    except Exception as e:
        logger.error("study_topic failed: %s", e, exc_info=True)
        try:
            _telegram.send_message(f"⚠️ Failed to load topics: {e}")
        except Exception:
            pass
        return {"pending_subtopic_message_id": None}


# ---------------------------------------------------------------------------
# Node: study_topic_category
# ---------------------------------------------------------------------------

def study_topic_category(state: AgentState) -> AgentState:
    """Handle category selection and send matching topic buttons.

    Args:
        state: Current partial agent state containing ``study_topic_category``.

    Returns:
        Empty state update; interaction continues through callback triggers.
    """
    try:
        category = state.get("study_topic_category")
        if not category:
            _telegram.send_message("⚠️ No category selected.")
            return {}

        rows = topic_repository.get_inactive_topics_tier1_or2()

        tier1 = [r for r in rows if r["tier"] == 1]
        available = tier1 if tier1 else [r for r in rows if r["tier"] == 2]

        if category == "Other":
            subtopic_rows = [r for r in available if " - " not in r["name"]]
        else:
            subtopic_rows = [r for r in available if r["name"].startswith(f"{category} - ")]

        if not subtopic_rows:
            _telegram.send_message(f"No topics found in category '{category}'.")
            return {}

        buttons = [(r["name"], f"subtopic_id:{r['id']}") for r in subtopic_rows]
        try:
            sent_msg_id = _telegram.send_inline_buttons("Which topic?", buttons)
        except RuntimeError as e:
            if "timed out" in str(e).lower():
                # Telegram likely delivered the message despite the timeout — log and continue
                logger.warning("send_inline_buttons timed out but message was likely delivered: %s", e)
                sent_msg_id = None
            else:
                raise
        return {"pending_subtopic_message_id": sent_msg_id}

    except Exception as e:
        logger.error("study_topic_category failed: %s", e, exc_info=True)
        try:
            _telegram.send_message(f"⚠️ Failed to load subtopics: {e}")
        except Exception:
            pass
        return {}


# ---------------------------------------------------------------------------
# Node: study_topic_confirm
# ---------------------------------------------------------------------------

def study_topic_confirm(state: AgentState) -> AgentState:
    """Mark the selected topic as ``in_progress`` and notify the user.

    Args:
        state: Current partial agent state containing ``proposed_topic``.

    Returns:
        Empty state update after DB write + Telegram side effects.
    """
    try:
        topic_name = state.get("proposed_topic")
        if not topic_name:
            _telegram.send_message("⚠️ No topic selected.")
            return {}

        updated = topic_repository.set_topic_in_progress(topic_name)
        if not updated:
            _telegram.send_message(
                f"⚠️ Topic '{topic_name}' not found or already in progress."
            )
            return {}

        _telegram.send_message(
            f"✅ {topic_name} added to In Progress. "
            "It will be booked on your calendar tomorrow morning."
        )
        return {"pending_subtopic_message_id": None}

    except Exception as e:
        logger.error("study_topic_confirm failed: %s", e, exc_info=True)
        try:
            _telegram.send_message(f"⚠️ Failed to set topic in progress: {e}")
        except Exception:
            pass
        return {}
