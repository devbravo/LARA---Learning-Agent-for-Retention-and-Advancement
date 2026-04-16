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
import sqlite3
from src.core import sm2 as _sm2_mod

import yaml

from src.core import gap_finder as _gap_finder
from src.core import sm2 as _sm2
from src.integrations import claude_api as _claude
from src.integrations import gcal as _gcal
from src.integrations import telegram_client as _telegram
from src.core.db import get_connection

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
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_time(t) -> str:
    """Format a datetime.time or HH:MM string to 'HH:MM'."""
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M")
    return str(t)[:5]


def _fmt_event_time(dt_dict: dict) -> str:
    """Parse a GCal start/end dict to a readable 'HH:MM' string."""
    if "dateTime" in dt_dict:
        dt = datetime.fromisoformat(dt_dict["dateTime"]).replace(tzinfo=None)
        return dt.strftime("%H:%M")
    return "all-day"


def _event_duration_min(event: dict) -> int:
    """Compute duration in minutes from a GCal event dict."""
    try:
        s = datetime.fromisoformat(event["start"]["dateTime"]).replace(tzinfo=None)
        e = datetime.fromisoformat(event["end"]["dateTime"]).replace(tzinfo=None)
        return int((e - s).total_seconds() / 60)
    except Exception:
        return 0


def _topic_due_label(topic: dict) -> str:
    """Return 'due' or 'due tomorrow' based on next_review."""
    try:
        nr = date.fromisoformat(topic["next_review"])
        delta = (nr - date.today()).days
        if delta <= 0:
            return "due"
        if delta == 1:
            return "due tomorrow"
        return f"due in {delta}d"
    except Exception:
        return "due"


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

def _rebook_study_events(
        in_progress_rows: list, timed_events: list, target_date, config: dict
) -> None:
    tz = pytz.timezone(config["timezone"])
    offset = datetime.now(tz).strftime("%z")
    offset_str = f"{offset[:3]}:{offset[3:]}"

    start_hour = 8  # start at 08:00

    for row in in_progress_rows:
        topic_name = row["name"]
        already_booked = any(
            _is_topic_in_summary(topic_name, ev.get("summary", ""))
            for ev in timed_events
        )
        if not already_booked:
            try:
                start = f"{target_date.isoformat()}T{start_hour:02d}:00:00{offset_str}"
                end = f"{target_date.isoformat()}T{start_hour + 1:02d}:00:00{offset_str}"
                _gcal.write_study_event(
                    topic=topic_name,
                    start=start,
                    end=end,
                )
            except Exception as e:
                logger.warning("Failed to rebook [Study] for %s: %s", topic_name, e)

        start_hour += 1  # always advance, whether booked or already on calendar


def _get_topic_config(topic_name: str, config: dict) -> dict:
    for t in config.get("topics", []):
        if t["name"] == topic_name:
            return t
    return {}


def _is_topic_in_summary(topic_name: str, summary: str) -> bool:
    norm_topic = topic_name.lower().replace(" and ", " & ")
    norm_summary = summary.lower().replace(" and ", " & ")
    return norm_topic in norm_summary or norm_summary in norm_topic


def _get_prebooked_topics(events: list, due_topics: list) -> set:
    prebooked = set()
    for topic in due_topics:
        for ev in events:
            raw_summary = ev.get("summary") or ""
            if not raw_summary:
                continue
            if _is_topic_in_summary(topic["name"], raw_summary):
                prebooked.add(topic["name"])
                break
    return prebooked


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
                    t = _fmt_event_time(ev["start"])
                    dur = _event_duration_min(ev)
                    dur_str = f"{dur}min" if dur else ""
                    summary = ev.get("summary", "(No title)")
                    lines.append(f"• {t} {summary}{' (' + dur_str + ')' if dur_str else ''}")
            else:
                lines.append("📅 Your day: No meetings tomorrow")
            lines.append("")

            # Study windows for tomorrow (no after_time filter — all windows are in the future)
            free_windows = _gap_finder.find_free_windows(events, target_date, config)
            prebooked = _get_prebooked_topics(timed_events, due_topics)
            available_topics = [t for t in due_topics if t["name"] not in prebooked]
            topics_config = _load_topics()

            if free_windows:
                lines.append("🧠 Study windows:")
                for i, win in enumerate(free_windows):
                    topic = available_topics[i] if i < len(available_topics) else None
                    if topic is None:
                        break
                    topic_cfg = _get_topic_config(topic["name"], topics_config)
                    default_duration = topic_cfg.get("default_duration_minutes", 60)
                    duration = min(default_duration, win["duration_min"])
                    t_start = _fmt_time(win["start"])
                    start_dt = datetime.combine(target_date, win["start"])
                    end_dt = start_dt + timedelta(minutes=duration)
                    t_end = _fmt_time(end_dt.time())
                    lines.append(f"• {t_start}–{t_end} → {topic['name']} ({duration}min)")
            else:
                lines.append("🧠 Study windows: None found for tomorrow")
            lines.append("")

            # SM-2 picks for tomorrow — all due topics with EF values
            if due_topics:
                lines.append("📌 SM-2 picks tomorrow:")
                for i, topic in enumerate(due_topics, 1):
                    label = _topic_due_label(topic)
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
        free_windows = _gap_finder.find_free_windows(events, target_date, config, after_time)
        prebooked = _get_prebooked_topics(timed_events, due_topics)

        # --- Build message ---
        day_str = target_date.strftime("%A %B %-d")
        lines = [f"☀️ Good morning Diego — {day_str}", ""]

        # Today's calendar events (skip all-day)
        if timed_events:
            lines.append("📅 Your day:")
            for ev in timed_events:
                t = _fmt_event_time(ev["start"])
                dur = _event_duration_min(ev)
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
                        topic_cfg = _get_topic_config(topic["name"], topics_config)
                        default_duration = topic_cfg.get("default_duration_minutes", 60)
                        duration = min(default_duration, remaining_min)
                        end_dt = cursor + timedelta(minutes=duration)
                        t_start = _fmt_time(cursor.time())
                        t_end = _fmt_time(end_dt.time())
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

        with get_connection() as conn:
            in_progress_rows = conn.execute(
                "SELECT name FROM topics WHERE status = 'in_progress' ORDER BY tier ASC, name ASC"
            ).fetchall()
        if in_progress_rows:
            lines.append("⏳ In Progress:")
            start_hour = 8 # TODO MAGIC NUMBER
            for row in in_progress_rows:
                t_start = f"{start_hour:02d}:00"
                t_end = f"{start_hour + 1:02d}:00"
                lines.append(f"• {t_start}–{t_end} [STUDY] {row['name']} (60min)")
                start_hour += 1
            lines.append("")

        # Auto-rebook [Study] events for in_progress topics
        _rebook_study_events(in_progress_rows, timed_events, target_date, config)

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
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT t.name FROM sessions s
                   JOIN topics t ON t.id = s.topic_id
                   WHERE date(s.studied_at) = date('now')"""
            ).fetchall()
        logged_names = {row["name"] for row in rows}

        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]
        if not unlogged:
            _telegram.send_message("All sessions already logged for today.")
            return {}

        slot = unlogged[0]
        topic_name = slot["topic"]

        with get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM topics WHERE name = ? COLLATE NOCASE", (topic_name,)
            ).fetchone()

        if row is None:
            _telegram.send_message(f"⚠️ Topic '{topic_name}' not found in database.")
            return {}

        logger.info("done_parser: sending rating buttons for %s", topic_name)
        _telegram.send_buttons(f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"])
        return {
            "current_topic_id": row["id"],
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
        with get_connection() as conn:
            row = conn.execute(
                "SELECT weak_areas FROM topics WHERE name = ? COLLATE NOCASE",
                (topic,)
            ).fetchone()
        if row and row["weak_areas"]:
            context = f"Focus on weak areas: {row['weak_areas']}"

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
    try:
        messages = state.get("messages") or []
        text = messages[-1] if messages else "Ready to study?"

        if state.get("proposed_slots"):
            # Daily briefing flow, needs confirmation before booking
            _telegram.send_buttons(text, ["Yes, book them", "Skip"])
        else:
            # Study picker flow, no booking needed, just send the brief
            _telegram.send_message(text)
    except Exception as e:
        try:
            _telegram.send_message(f"⚠️ Button send failed: {e}\n\n{messages[-1] if messages else ''}")
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

        db_path = str(Path(__file__).parents[2] / "db" / "learning.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT id FROM sessions WHERE topic_id = ? AND DATE(studied_at) = DATE('now')",
                (topic_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE sessions SET quality_score = ?, duration_min = ? WHERE id = ?",
                    (quality, duration_min, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO sessions (topic_id, duration_min, quality_score) VALUES (?, ?, ?)",
                    (topic_id, duration_min, quality),
                )
            conn.commit()
        finally:
            conn.close()

        _sm2_mod.update_topic_after_session(db_path=db_path, topic_id=topic_id, quality=quality)

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

        with get_connection() as conn:
            session_row = conn.execute(
                "SELECT id FROM sessions WHERE topic_id = ? AND DATE(studied_at) = DATE('now')",
                (topic_id,)
            ).fetchone()

        if text:
            with get_connection() as conn:
                if session_row:
                    conn.execute(
                        "UPDATE sessions SET weak_areas = ? WHERE id = ?",
                        (text, session_row["id"]),
                    )
                conn.execute(
                    "UPDATE topics SET weak_areas = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (text, topic_id),

                )
        else:
            # Skip — mark existing weak areas as resolved
            with get_connection() as conn:
                conn.execute(
                    "UPDATE topics SET weak_areas = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (topic_id,),
                )

        # Check for next unlogged slot
        proposed_slots = state.get("proposed_slots") or []
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT t.name FROM sessions s
                   JOIN topics t ON t.id = s.topic_id
                   WHERE date(s.studied_at) = date('now')"""
            ).fetchall()
        logged_names = {row["name"] for row in rows}

        unlogged = [s for s in proposed_slots if s["topic"] not in logged_names]

        if unlogged:
            next_topic_name = unlogged[0]["topic"]
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT id FROM topics WHERE name = ? COLLATE NOCASE", (next_topic_name,)
                ).fetchone()

            if row:
                _telegram.send_buttons(
                    f"How did {next_topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"]
                )
                return {
                    "awaiting_weak_areas": False,
                    "current_topic_id": row["id"],
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
        offset = datetime.now(tz).strftime("%z")
        offset_str = f"{offset[:3]}:{offset[3:]}"

        booked: list[str] = []
        slots = state.get("proposed_slots")

        if slots:
            for slot in slots:
                try:
                    t_start = _fmt_time(slot["start"])
                    t_end = _fmt_time(slot["end"])
                    _gcal.write_event(
                        topic=slot["topic"],
                        start=f"{today.isoformat()}T{t_start}:00{offset_str}",
                        end=f"{today.isoformat()}T{t_end}:00{offset_str}",
                    )
                    booked.append(slot["topic"])
                except Exception as e:
                    print(f"[output] Calendar write failed for {slot.get('topic')}: {e}")
        else:
            try:
                topic = state.get("proposed_topic")
                slot = state.get("proposed_slot")
                if topic and slot:
                    t_start = _fmt_time(slot["start"])
                    t_end = _fmt_time(slot["end"])
                    _gcal.write_event(
                        topic=topic,
                        start=f"{today.isoformat()}T{t_start}:00{offset_str}",
                        end=f"{today.isoformat()}T{t_end}:00{offset_str}",
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
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT name, tier FROM topics
                   WHERE status = 'inactive' AND tier IN (1, 2)
                   ORDER BY tier ASC, name ASC"""
            ).fetchall()

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

        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, name, tier FROM topics
                   WHERE status = 'inactive' AND tier IN (1, 2)
                   ORDER BY tier ASC, name ASC"""
            ).fetchall()

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

        with get_connection() as conn:
            cursor = conn.execute(
                """UPDATE topics SET status = 'in_progress', updated_at = CURRENT_TIMESTAMP
                   WHERE name = ? AND status = 'inactive'""",
                (topic_name,),
            )
            if cursor.rowcount == 0:
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
