"""
LangGraph node implementations for the Learning Manager agent.

Each node receives an AgentState and returns a partial state update dict.
All exceptions are caught and surfaced as user-friendly messages in state.
"""

import re
import pytz
from datetime import date, datetime
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
    trigger: str               # "daily" | "on_demand" | "done" | "confirm" | "rate" | "weak_areas"
    chat_id: int
    message_id: int | None              # Telegram message_id of the confirm keyboard
    duration_min: int | None
    proposed_topic: str | None          # single-slot flow (on_demand)
    proposed_slot: dict | None          # single-slot flow (on_demand)
    proposed_slots: list[dict] | None   # multi-slot flow (daily_planning)
    has_study_plan: bool                # False → skip confirm, go straight to output
    quality_score: int | None
    messages: list[str]        # outbound Telegram messages
    awaiting_weak_areas: bool
    current_topic_id: int | None
    current_topic_name: str | None

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
        "daily": "daily_planning",
        "on_demand": "on_demand",
        "done": "done_parser",
        "confirm": "output",
        "rate": "log_session",
        "weak_areas": "log_weak_areas",
    }
    return mapping.get(trigger, "output")


def route_from_daily_planning(state: AgentState) -> str:
    """Conditional edge: skip confirm entirely when there's nothing to book."""
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
    Assembles the morning plan from calendar + SM-2 + gap finder.
    Sets proposed_topic and proposed_slot.
    """
    try:
        today = date.today()
        config = _load_config()

        events = _gcal.get_events(today)
        due_topics = _sm2.get_due_topics()
        _TZ = pytz.timezone(_load_config()["timezone"])
        after_time = datetime.now(_TZ).time()
        free_windows = _gap_finder.find_free_windows(events, today, config, after_time)
        timed_events = [e for e in events if "dateTime" in e.get("start", {})]
        prebooked = _get_prebooked_topics(timed_events, due_topics)


        # --- Build message ---
        day_str = today.strftime("%A %B %-d")
        lines = [f"☀️ Good morning Diego — {day_str}", ""]

        # Today's calendar events (skip all-day)
        if timed_events:
            lines.append("📅 Your day:")
            for ev in timed_events:
                t = _fmt_event_time(ev["start"])
                dur = _event_duration_min(ev)
                dur_str = f"{dur}min" if dur else ""
                summary = ev.get("summary", "(No title)")
                booked_marker = " ✓ already booked" if any(
                    _is_topic_in_summary(tn, summary) for tn in prebooked
                ) else ""
                lines.append(f"  {t} {summary}{' (' + dur_str + ')' if dur_str else ''}{booked_marker}")
        else:
            lines.append("📅 Your day: No meetings today")
        lines.append("")

        # Study windows → assign topics, build proposed_slots list
        from datetime import timedelta

        proposed_topic = None
        proposed_slot = None
        proposed_slots: list[dict] = []

        if free_windows:
            lines.append("🧠 Today's study plan:")
            topics_config = _load_topics()
            available_topics = [t for t in due_topics if t["name"] not in prebooked]
            for i, win in enumerate(free_windows):
                topic = available_topics[i] if i < len(available_topics) else None
                if topic is None:
                    break  # no more topics to assign
                topic_cfg = _get_topic_config(topic["name"], topics_config)
                default_duration = topic_cfg.get("default_duration_minutes", 60)
                duration = min(default_duration, win["duration_min"])
                t_start = _fmt_time(win["start"])
                start_dt = datetime.combine(today, win["start"])
                end_dt = start_dt + timedelta(minutes=duration)
                t_end = _fmt_time(end_dt.time())
                lines.append(f"  {t_start}–{t_end} → {topic['name']} ({duration}min)")

                slot = {
                    "topic": topic["name"],
                    "start": win["start"],
                    "end": end_dt.time(),
                    "duration_min": duration,
                }
                proposed_slots.append(slot)


                # Keep single-slot fields pointing at the first block (backwards compatible)
                if i == 0:
                    proposed_topic = topic["name"]
                    proposed_slot = slot
        else:
            lines.append("🧠 Study windows: None found today")
        lines.append("")

        # In-progress topics (informational only — not assigned to windows)
        from src.core.db import get_connection
        with get_connection() as conn:
            in_progress_rows = conn.execute(
                "SELECT name FROM topics WHERE status = 'in_progress' ORDER BY tier ASC, name ASC"
            ).fetchall()
        if in_progress_rows:
            lines.append("📌 In progress (via AlgoMonster):")
            for row in in_progress_rows:
                lines.append(f"  • {row['name']}")
            lines.append("")

        has_study_plan = bool(proposed_slots)
        if has_study_plan:
            lines.append("Confirm these study blocks?")
        else:
            lines.append("No study windows available today — calendar fully booked.")

        message = "\n".join(lines)
        return {
            "proposed_topic": proposed_topic,
            "proposed_slot": proposed_slot,
            "proposed_slots": proposed_slots if proposed_slots else None,
            "has_study_plan": has_study_plan,
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
        if messages:
            try:
                _telegram.send_message(messages[-1])
            except Exception as e:
                print(f"[output] Telegram send failed: {e}")

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
                _telegram.send_message(f"✅ Booked {len(booked)} study session(s):\n{summary}")
            except Exception as e:
                print(f"[output] Confirmation send failed: {e}")

    return {}
