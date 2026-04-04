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

import yaml

from src.core import gap_finder as _gap_finder
from src.core import sm2 as _sm2
from src.integrations import claude_api as _claude
from src.integrations import gcal as _gcal
from src.integrations import telegram_client as _telegram

_CONFIG_PATH = Path(__file__).parents[2] / "config.yaml"


def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    trigger: str               # "daily" | "study_picker" | "done" | "confirm"
    chat_id: int
    duration_min: int | None
    proposed_topic: str | None          # single-slot flow (study_picker)
    proposed_slot: dict | None          # single-slot flow (study_picker)
    proposed_slots: list[dict] | None   # multi-slot flow (daily_briefing)
    session_summary: dict | None
    quality_score: int | None
    messages: list[str]        # outbound Telegram messages
    # Internal routing flag set by done_parser
    _parse_ok: bool


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
        "daily": "daily_briefing",
        "study_picker": "study_picker",
        "done": "done_parser",
        "confirm": "output",
    }
    return mapping.get(trigger, "output")


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
# Node: daily_briefing
# ---------------------------------------------------------------------------

def _get_topic_config(topic_name: str, config: dict) -> dict:
    for t in config.get("topics", []):
        if t["name"] == topic_name:
            return t
    return {}


def daily_briefing(state: AgentState) -> AgentState:
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
        # after_time = datetime.now(_TZ).time()
        free_windows = _gap_finder.find_free_windows(events, today, config, )

        # --- Build message ---
        day_str = today.strftime("%A %B %-d")
        lines = [f"☀️ Good morning Diego — {day_str}", ""]

        # Today's calendar events (skip all-day)
        timed_events = [e for e in events if "dateTime" in e.get("start", {})]
        if timed_events:
            lines.append("📅 Your day:")
            for ev in timed_events:
                t = _fmt_event_time(ev["start"])
                dur = _event_duration_min(ev)
                dur_str = f"{dur}min" if dur else ""
                summary = ev.get("summary", "(No title)")
                lines.append(f"  {t} {summary}{' (' + dur_str + ')' if dur_str else ''}")
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
            for i, win in enumerate(free_windows):
                topic = due_topics[i] if i < len(due_topics) else None
                if topic is None:
                    break  # no more topics to assign
                topic_cfg = _get_topic_config(topic["name"], config)
                duration = topic_cfg.get("default_duration_minutes", 60)
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

                # Keep single-slot fields pointing at the first block (backwards compat)
                if i == 0:
                    proposed_topic = topic["name"]
                    proposed_slot = slot
        else:
            lines.append("🧠 Study windows: None found today")
        lines.append("")

        if proposed_slots:
            lines.append("Confirm these study blocks? [Yes, book them] [Skip]")
        else:
            lines.append("No study blocks to confirm today.")

        message = "\n".join(lines)
        return {
            "proposed_topic": proposed_topic,
            "proposed_slot": proposed_slot,
            "proposed_slots": proposed_slots if proposed_slots else None,
            "messages": [message],
        }

    except Exception as e:
        return {"messages": [f"⚠️ Daily briefing failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: study_picker
# ---------------------------------------------------------------------------

def study_picker(state: AgentState) -> AgentState:
    """
    Handles 'I have X min' flow.
    Validates the requested duration fits a free window, sets proposed_topic/slot.
    """
    try:
        today = date.today()
        config = _load_config()
        duration_min = state.get("duration_min") or 30

        events = _gcal.get_events(today)
        due_topics = _sm2.get_due_topics()
        free_windows = _gap_finder.find_free_windows(events, today, config)

        slot = _gap_finder.find_slot_for_duration(free_windows, duration_min)
        if slot is None:
            return {
                "messages": [
                    f"⚠️ No free window of {duration_min} minutes found today.\n"
                    "Try a shorter duration or check back later."
                ]
            }

        topic = due_topics[0] if due_topics else None
        if topic is None:
            return {"messages": ["🎉 Nothing due for review right now — enjoy your break!"]}

        context = topic.get("weak_areas") or "General review"
        return {
            "proposed_topic": topic["name"],
            "proposed_slot": slot,
            "messages": state.get("messages", []) + [
                f"📚 Ready to study {topic['name']} for {duration_min} min "
                f"at {_fmt_time(slot['start'])}–{_fmt_time(slot['end'])}. Generating brief…"
            ],
        }

    except Exception as e:
        return {"messages": [f"⚠️ Study picker failed: {e}"]}


# ---------------------------------------------------------------------------
# Node: done_parser
# ---------------------------------------------------------------------------

_SUMMARY_PATTERN = re.compile(
    r"📋 Session summary\s*\n"
    r"Topic:\s*(.+)\n"
    r"Duration:\s*(\d+)\s*min\s*\n"
    r"Weak areas:\s*(.+)\n"
    r"Suggestions:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)

_EXPECTED_FORMAT = (
    "📋 Session summary\n"
    "Topic: <topic name>\n"
    "Duration: <N> min\n"
    "Weak areas: <comma-separated>\n"
    "Suggestions: <free text>"
)


def done_parser(state: AgentState) -> AgentState:
    """
    Parses pasted session summary from messages[0].
    Sets session_summary. Fails loudly if malformed.
    """
    raw = (state.get("messages") or [""])[0]
    match = _SUMMARY_PATTERN.search(raw)

    if not match:
        return {
            "_parse_ok": False,
            "messages": [
                "❌ Could not parse session summary. Please use this exact format:\n\n"
                f"<pre>{_EXPECTED_FORMAT}</pre>"
            ],
        }

    topic_name = match.group(1).strip()
    duration_min = int(match.group(2).strip())
    weak_areas = match.group(3).strip()
    suggestions = match.group(4).strip()

    # Validate topic exists
    due_topics = _sm2.get_due_topics()
    # Also query all topics, not just due ones, to match the name
    from src.core.db import get_connection
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, name FROM topics WHERE name = ? COLLATE NOCASE",
            (topic_name,)
        ).fetchone()

    if row is None:
        known = [t["name"] for t in due_topics]
        return {
            "_parse_ok": False,
            "messages": [
                f"❌ Topic '{topic_name}' not found in the database.\n"
                f"Known topics: {', '.join(known) if known else 'none'}"
            ],
        }

    return {
        "_parse_ok": True,
        "session_summary": {
            "topic_id": row["id"],
            "topic_name": row["name"],
            "duration_min": duration_min,
            "weak_areas": weak_areas,
            "suggestions": suggestions,
        },
        "messages": state.get("messages", []),
    }


def route_from_done_parser(state: AgentState) -> str:
    """Conditional edge: if parse succeeded → log_session, else → output (error message)."""
    if state.get("_parse_ok"):
        return "log_session"
    return "output"


# ---------------------------------------------------------------------------
# Node: brief_generator
# ---------------------------------------------------------------------------

def brief_generator(state: AgentState) -> AgentState:
    """Calls Claude API to generate a study brief. Only node that calls an LLM."""
    try:
        topic = state.get("proposed_topic") or "General Study"
        duration_min = state.get("duration_min") or 30

        # Build context from weak_areas if available
        from src.core.db import get_connection
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
    Sends the assembled plan to Telegram with confirmation buttons.
    Does NOT advance state — next trigger arrives via webhook.
    """
    try:
        messages = state.get("messages") or []
        text = messages[-1] if messages else "Ready to study?"
        _telegram.send_buttons(text, ["Yes, book them", "Skip"])
    except Exception as e:
        # Best-effort: try plain message
        try:
            _telegram.send_message(f"⚠️ Button send failed: {e}\n\n{messages[-1] if messages else ''}")
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Node: log_session
# ---------------------------------------------------------------------------

def log_session(state: AgentState) -> AgentState:
    """Logs session to DB and updates SM-2 state."""
    try:
        summary = state.get("session_summary") or {}
        quality = state.get("quality_score") or 3

        topic_id = summary.get("topic_id")
        duration_min = summary.get("duration_min", 0)
        weak_areas = summary.get("weak_areas", "")
        suggestions = summary.get("suggestions", "")

        if not topic_id:
            return {"messages": ["⚠️ Cannot log session: missing topic_id."]}

        # Import log_study_session tool's underlying logic directly
        import sqlite3
        from pathlib import Path as _Path
        from src.core import sm2 as _sm2_mod

        db_path = str(_Path(__file__).parents[2] / "db" / "learning.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO sessions (topic_id, duration_min, quality_score, weak_areas, suggestions)
                VALUES (?, ?, ?, ?, ?)
                """,
                (topic_id, duration_min, quality, weak_areas or None, suggestions or None),
            )
            if weak_areas:
                conn.execute(
                    "UPDATE topics SET weak_areas = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (weak_areas, topic_id),
                )
            conn.commit()
        finally:
            conn.close()

        _sm2_mod.update_topic_after_session(db_path=db_path, topic_id=topic_id, quality=quality)

        return {
            "messages": [
                f"✅ Session logged for {summary.get('topic_name', 'topic')} "
                f"({duration_min} min). SM-2 updated."
            ]
        }

    except Exception as e:
        return {"messages": [f"⚠️ Failed to log session: {e}"]}


# ---------------------------------------------------------------------------
# Node: output
# ---------------------------------------------------------------------------

def output(state: AgentState) -> AgentState:
    """
    Sends final message via Telegram.
    If a confirmed slot exists, books it on Google Calendar.
    """
    try:
        messages = state.get("messages") or []
        text = messages[-1] if messages else "Done."
        _telegram.send_message(text)
    except Exception as e:
        # Log but don't crash — message already in state
        print(f"[output] Telegram send failed: {e}")

    # Book calendar events on confirmation
    trigger = state.get("trigger", "")
    if trigger == "confirm":
        today = date.today()
        slots = state.get("proposed_slots")

        if slots:
            # Daily briefing flow — book every proposed slot
            for slot in slots:
                try:
                    t_start = _fmt_time(slot["start"])
                    t_end = _fmt_time(slot["end"])
                    _gcal.write_event(
                        topic=slot["topic"],
                        start=f"{today.isoformat()}T{t_start}:00",
                        end=f"{today.isoformat()}T{t_end}:00",
                    )
                except Exception as e:
                    print(f"[output] Calendar write failed for {slot.get('topic')}: {e}")
        else:
            # study_picker flow — single slot
            try:
                topic = state.get("proposed_topic")
                slot = state.get("proposed_slot")
                if topic and slot:
                    t_start = _fmt_time(slot["start"])
                    t_end = _fmt_time(slot["end"])
                    _gcal.write_event(
                        topic=topic,
                        start=f"{today.isoformat()}T{t_start}:00",
                        end=f"{today.isoformat()}T{t_end}:00",
                    )
            except Exception as e:
                print(f"[output] Calendar write failed: {e}")

    return {}
