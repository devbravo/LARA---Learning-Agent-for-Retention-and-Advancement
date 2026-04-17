"""
LangGraph tool definitions for the Learning Manager agent.

A tool touches something outside the graph's own state (calendar, DB, etc.).
All 5 tools are decorated with @tool for LangGraph/LangChain compatibility.
"""

from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

from src.core import gap_finder as _gap_finder
from src.core import sm2 as _sm2
from src.integrations import gcal as _gcal
from src.repositories import session_repository, topic_repository

_CONFIG_PATH = Path(__file__).parents[2] / "config.yaml"
_DB_PATH = str(Path(__file__).parents[2] / "db" / "learning.db")


def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Calendar tools
# ---------------------------------------------------------------------------

@tool
def get_calendar_events(date_str: str) -> list[dict]:
    """
    Fetch all Google Calendar events for a given date.

    Args:
        date_str: ISO date string, e.g. '2026-04-03'.

    Returns list of event dicts with keys: id, summary, start, end, creator.
    """
    target = date.fromisoformat(date_str)
    return _gcal.get_events(target)


@tool
def find_free_windows(date_str: str) -> list[dict]:
    """
    Compute free study windows for a given date, respecting calendar events,
    protected blocks, and focus windows defined in config.yaml.

    Args:
        date_str: ISO date string, e.g. '2026-04-03'.

    Returns list of dicts: [{'start': time, 'end': time, 'duration_min': int}].
    """
    target = date.fromisoformat(date_str)
    events = _gcal.get_events(target)
    config = _load_config()
    return _gap_finder.find_free_windows(events, target, config)


@tool
def get_due_topics() -> list[dict]:
    """
    Return topics due for review today (next_review <= today, active = 1),
    ordered by tier ASC then easiness_factor ASC (hardest first within tier).

    Returns list of topic dicts with SM-2 state fields.
    """
    return _sm2.get_due_topics(db_path=_DB_PATH)


# ---------------------------------------------------------------------------
# Calendar write tool
# ---------------------------------------------------------------------------

@tool
def write_calendar_event(topic: str, start: str, end: str) -> dict:
    """
    Book a [Study] event on Google Calendar.

    Safety rule: this tool only ever creates new events prefixed with '[Study]'.
    It never modifies existing events. Modifications to events not created by
    this agent are permanently blocked.

    Args:
        topic: Study topic name (e.g. 'System Design').
        start: ISO-8601 datetime string for event start (e.g. '2026-04-03T09:00:00').
        end:   ISO-8601 datetime string for event end.

    Returns the created event dict.
    """
    created = _gcal.write_event(topic=topic, start=start, end=end)

    # Enforce calendar safety rule: verify the created event belongs to us
    if not created.get("creator", {}).get("self", False):
        raise PermissionError(
            f"Cannot modify event not created by this agent (id={created.get('id')})"
        )

    return created


# ---------------------------------------------------------------------------
# Session logging tool
# ---------------------------------------------------------------------------

@tool
def log_study_session(
    topic_id: int,
    duration_min: int,
    quality_score: int,
    weak_areas: str = "",
) -> None:
    """
    Log a completed study session and update SM-2 state for the topic.

    Args:
        topic_id:     ID from the topics table.
        duration_min: Session length in minutes.
        quality_score: SM-2 quality rating — 2 (Hard), 3 (OK), or 5 (Easy).
        weak_areas: Optional notes about weak concepts, mistakes, or areas to review
    """
    if quality_score not in (2, 3, 5):
        raise ValueError(f"quality_score must be 2, 3, or 5 — got {quality_score}")

    session_repository.insert_session(
        topic_id=topic_id,
        duration_min=duration_min,
        quality_score=quality_score,
        weak_areas=weak_areas or None,
    )
    if weak_areas:
        topic_repository.update_topic_weak_areas(topic_id, weak_areas)

    # Update SM-2 state
    _sm2.update_topic_after_session(db_path=_DB_PATH, topic_id=topic_id, quality=quality_score)
