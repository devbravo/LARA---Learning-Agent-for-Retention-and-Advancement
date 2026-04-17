"""
LangGraph node implementations for the Learning Manager agent.

Each node receives an AgentState and returns a partial state update dict.
All exceptions are caught and surfaced as user-friendly messages in state.
"""

import pytz
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import logging
from src.core import sm2 as _sm2_mod

import yaml

from src.agent.formatting import (
    event_duration_min,
    format_event_time,
    format_time,
    local_datetime_str,
    topic_due_label,
)
from src.agent.planning_helpers import (
    build_in_progress_study_slots,
    build_missing_study_events,
    get_prebooked_topics,
    get_topic_config,
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

# ---------------------------------------------------------------------------
# Node: router
# ---------------------------------------------------------------------------

def router(state: AgentState) -> AgentState:
    """
    Entry point. Validates trigger is set.
    Routing is handled by conditional edges — this node just passes through.
    """
    trigger = state.get("trigger", "")
    if not trigger:
        return {"messages": ["⚠️ No trigger set — cannot route."]}
    return {}


def route_from_router(state: AgentState) -> str:
    """Conditional edge: maps trigger → next node name."""
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
    """Conditional edge: skip confirm for evening briefings or when there's nothing to book."""
    if state.get("preview_only"):
        return "output"
    return "confirm" if state.get("has_study_plan") else "output"


# ---------------------------------------------------------------------------
# Node: calendar_reader
# ---------------------------------------------------------------------------

def calendar_reader(state: AgentState) -> AgentState:
    """Read-only GCal fetch for today. Returns structured event list in messages."""
    try:
        events = _gcal.get_events(date.today())
        return {"messages": state.get("messages", []) + [f"📅 Fetched {len(events)} events"]}
    except Exception as e:
        return {"messages": state.get("messages", []) + [f"⚠️ Calendar read failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: sm2_engine
# ---------------------------------------------------------------------------

def sm2_engine(state: AgentState) -> AgentState:
    """Returns due topics ranked by tier + easiness factor."""
    try:
        topics = _sm2.get_due_topics()
        return {"messages": state.get("messages", []) + [f"🧠 {len(topics)} topics due"]}
    except Exception as e:
        return {"messages": state.get("messages", []) + [f"⚠️ SM-2 fetch failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: gap_finder
# ---------------------------------------------------------------------------

def gap_finder(state: AgentState) -> AgentState:
    """Computes free windows respecting protected blocks."""
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
    """
    Assembles the morning plan (trigger="daily") or evening preview (trigger="evening")
    from calendar + SM-2 + gap finder.

    Evening flow: reads tomorrow's calendar and SM-2 due topics, sets preview_only=True,
    and routes straight to output without confirmation buttons or calendar writes.
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

        # --- Evening briefing: read-only preview for tomorrow ---
        if is_evening:
            day_str = target_date.strftime("%A %B %-d")
            lines = [f"🌙 Tomorrow's plan — {day_str}", ""]

            # Tomorrow's calendar events (skip all-day)
            if timed_events:
                lines.append("📅 Your day:")
                for ev in timed_events:
                    t = format_event_time(ev["start"])
                    dur = event_duration_min(ev)
                    dur_str = f"{dur}min" if dur else ""
                    summary = ev.get("summary", "(No title)")
                    lines.append(f"• {t} {summary}{' (' + dur_str + ')' if dur_str else ''}")
            else:
                lines.append("📅 Your day: No meetings tomorrow")
            lines.append("")

            # Study windows for tomorrow (no after_time filter — all windows are in the future)
            free_windows = _gap_finder.find_free_windows(events, target_date, config)
            prebooked = get_prebooked_topics(timed_events, due_topics)
            available_topics = [t for t in due_topics if t["name"] not in prebooked]
            topics_config = _load_topics()

            if free_windows:
                lines.append("🧠 Study windows:")
                for i, win in enumerate(free_windows):
                    topic = available_topics[i] if i < len(available_topics) else None
                    if topic is None:
                        break
                    topic_cfg = get_topic_config(topic["name"], topics_config)
                    default_duration = topic_cfg.get("default_duration_minutes", 60)
                    duration = min(default_duration, win["duration_min"])
                    t_start = format_time(win["start"])
                    start_dt = datetime.combine(target_date, win["start"])
                    end_dt = start_dt + timedelta(minutes=duration)
                    t_end = format_time(end_dt.time())
                    lines.append(f"• {t_start}–{t_end} → {topic['name']} ({duration}min)")
            else:
                lines.append("🧠 Study windows: None found for tomorrow")
            lines.append("")

            # SM-2 picks for tomorrow — all due topics with EF values
            if due_topics:
                lines.append("📌 SM-2 picks tomorrow:")
                for i, topic in enumerate(due_topics, 1):
                    label = topic_due_label(topic)
                    ef = topic["easiness_factor"]
                    lines.append(f"• {i}. {topic['name']} — {label} (EF: {ef})")
                lines.append("")

            lines.append("No confirmation needed — this is your preview for tomorrow.")

            return {
                "preview_only": True,
                "has_study_plan": False,
                "messages": ["\n".join(lines)],
            }

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

        # --- Build message ---
        day_str = target_date.strftime("%A %B %-d")
        lines = [f"☀️ Good morning Diego — {day_str}", ""]

        # Today's calendar events (skip all-day)
        if timed_events:
            lines.append("📅 Your day:")
            for ev in timed_events:
                t = format_event_time(ev["start"])
                dur = event_duration_min(ev)
                dur_str = f"{dur}min" if dur else ""
                summary = ev.get("summary", "(No title)")
                lines.append(f"• {t} {summary}{' (' + dur_str + ')' if dur_str else ''}")
        else:
            lines.append("📅 Your day: No meetings today")
        lines.append("")

        # Study windows → assign topics, build proposed_slots list

        proposed_topic = None
        proposed_slot = None
        proposed_slots: list[dict] = []

        available_topics = [t for t in due_topics if t["name"] not in prebooked]
        topics_config = _load_topics()

        MAX_SLOTS = 6
        min_window_minutes = config.get("min_window_minutes", 25)

        if free_windows:
            lines.append("🎯 Today's mock interview(s):")

            if free_windows:
                remaining_topics = list(available_topics)
                for win in free_windows:
                    if not remaining_topics or len(proposed_slots) >= MAX_SLOTS:
                        break
                    cursor = datetime.combine(target_date, win["start"])
                    win_end = datetime.combine(target_date, win["end"])
                    while remaining_topics and len(proposed_slots) < MAX_SLOTS:
                        remaining_min = int((win_end - cursor).total_seconds() // 60)
                        if remaining_min < min_window_minutes:
                            break  # not enough time left in this window for anything useful
                        topic = remaining_topics[0]
                        topic_cfg = get_topic_config(topic["name"], topics_config)
                        default_duration = topic_cfg.get("default_duration_minutes", 60)
                        duration = min(default_duration, remaining_min)
                        end_dt = cursor + timedelta(minutes=duration)
                        t_start = format_time(cursor.time())
                        t_end = format_time(end_dt.time())
                        lines.append(f"• {t_start}–{t_end} [Mock] {topic['name']} ({duration}min)")
                        if topic.get("weak_areas"):
                            lines.append(f" ⚠️ Focus on: {topic['weak_areas']}")
                        lines.append("")  # blank line after each slot

                        slot = {
                            "topic": topic["name"],
                            "start": t_start,
                            "end": t_end,
                            "duration_min": duration,
                        }
                        proposed_slots.append(slot)

                        # Keep single-slot fields pointing at the first block (backwards compatible)
                        if proposed_topic is None:
                            proposed_topic = topic["name"]
                            proposed_slot = slot

                        cursor = end_dt
                        remaining_topics.pop(0)

        else:
            lines.append("🎯 Mock interview blocks: None found today")
            lines.append("")

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
        return {
            "proposed_topic": proposed_topic,
            "proposed_slot": proposed_slot,
            "proposed_slots": proposed_slots if proposed_slots else None,
            "has_study_plan": has_study_plan,
            "preview_only": False,
            "messages": [message],
        }

    except Exception as e:
        return {"messages": [f"⚠️ Daily briefing failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: on_demand
# ---------------------------------------------------------------------------

def on_demand(state: AgentState) -> AgentState:
    """
    Handles 'I have X min' flow.
    Validates the requested duration fits a free window, sets proposed_topic/slot.
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
    """
    Find the first unlogged slot from proposed_slots and send rating buttons.
    Sets current_topic_id and current_topic_name in state.
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
    """Calls Claude API to generate a study brief. Only node that calls an LLM."""
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
    """
    Sends the assembled plan to Telegram.
    Daily briefing flow: sends inline buttons (Yes, book them / Skip).
    On-demand flow: sends the study brief as a plain message (no booking needed).
    Does NOT advance state — next trigger arrives via webhook.
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
    """Logs session row with quality_score and prompts for weak areas."""
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
    """Saves weak areas (or marks resolved on Skip), then prompts for next unlogged slot."""
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
    """
    Sends final message via Telegram.
    If a confirmed slot exists, books it on Google Calendar.
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
    """Entry point: query inactive tier 1 (or tier 2 fallback) topics, send category picker."""
    try:
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
        return {}

    except Exception as e:
        logger.error("study_topic failed: %s", e, exc_info=True)
        try:
            _telegram.send_message(f"⚠️ Failed to load topics: {e}")
        except Exception:
            pass
        return {}


# ---------------------------------------------------------------------------
# Node: study_topic_category
# ---------------------------------------------------------------------------

def study_topic_category(state: AgentState) -> AgentState:
    """Received category selection: filter subtopics for that category, send subtopic buttons."""
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
        _telegram.send_inline_buttons("Which topic?", buttons)
        return {}

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
    """Received subtopic selection: set status=in_progress in DB, confirm to user."""
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
        return {}

    except Exception as e:
        logger.error("study_topic_confirm failed: %s", e, exc_info=True)
        try:
            _telegram.send_message(f"⚠️ Failed to set topic in progress: {e}")
        except Exception:
            pass
        return {}
