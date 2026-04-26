"""Session repository SQL helpers."""

from datetime import datetime, timedelta, timezone

import pytz

from src.infrastructure.db import get_connection
from src.infrastructure.time import _tz, local_now, local_today

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _legacy_utc_range() -> tuple[str, str]:
    """Return the UTC window that covers today in the local timezone.

    Legacy rows were stored via SQLite's DEFAULT CURRENT_TIMESTAMP (UTC).
    Rather than matching a UTC calendar date (which is wrong for timezones
    east of UTC — their "today" spans two UTC dates), we compute the exact
    UTC timestamps for local midnight → next local midnight so the range
    maps precisely to the current local day.

    Returns:
        (utc_start, utc_end) as ``'YYYY-MM-DD HH:MM:SS'`` strings suitable
        for ``studied_at >= ? AND studied_at < ?`` SQL comparisons.
    """
    tz = _tz()
    today_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_local = today_local + timedelta(days=1)
    utc_start = today_local.astimezone(timezone.utc).strftime(_TIMESTAMP_FMT)
    utc_end = tomorrow_local.astimezone(timezone.utc).strftime(_TIMESTAMP_FMT)
    return utc_start, utc_end


def get_logged_topic_names_for_today() -> set[str]:
    """Return topic names that already have a logged session today (local date).

    Matches new local-time rows by calendar date and legacy UTC rows by the
    UTC window that corresponds to the current local day.
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT t.name FROM sessions s
               JOIN topics t ON t.id = s.topic_id
               WHERE DATE(s.studied_at) = ?
                  OR (s.studied_at >= ? AND s.studied_at < ?)""",
            (local, utc_start, utc_end),
        ).fetchall()
    return {row["name"] for row in rows}


def upsert_today_session(topic_id: int, duration_min: int, student_quality: int) -> None:
    """Insert or update today's session row for a topic (local date).

    Matches new local-time rows by calendar date and legacy UTC rows by the
    UTC window that corresponds to the current local day, preventing duplicate
    rows during the migration transition period.

    Args:
        topic_id: Topic primary key.
        duration_min: Session duration in minutes.
        student_quality: Student self-assessment quality score (2/3/5).
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    with get_connection() as conn:
        existing = conn.execute(
            """SELECT id FROM sessions
               WHERE topic_id = ?
                 AND (DATE(studied_at) = ?
                      OR (studied_at >= ? AND studied_at < ?))""",
            (topic_id, local, utc_start, utc_end),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE sessions SET student_quality = ?, duration_min = ? WHERE id = ?",
                (student_quality, duration_min, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO sessions (topic_id, duration_min, student_quality, studied_at) VALUES (?, ?, ?, ?)",
                (topic_id, duration_min, student_quality, local_now()),
            )


def get_today_session_id(topic_id: int) -> int | None:
    """Return today's session id for a topic (local date).

    Matches new local-time rows by calendar date and legacy UTC rows by the
    UTC window that corresponds to the current local day.

    Args:
        topic_id: Topic primary key.

    Returns:
        Session id when present, else ``None``.
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    with get_connection() as conn:
        row = conn.execute(
            """SELECT id FROM sessions
               WHERE topic_id = ?
                 AND (DATE(studied_at) = ?
                      OR (studied_at >= ? AND studied_at < ?))""",
            (topic_id, local, utc_start, utc_end),
        ).fetchone()
    return row["id"] if row else None


def update_session_weak_areas(session_id: int, weak_areas: str) -> None:
    """Update legacy weak-areas column for a specific session row.

    Args:
        session_id: Session primary key.
        weak_areas: Weak-areas text (kept for backward compat with existing rows).
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE sessions SET weak_areas = ? WHERE id = ?",
            (weak_areas, session_id),
        )


def update_session_student_weak_areas(session_id: int, student_weak_areas: str) -> None:
    """Update structured student weak areas JSON for a specific session row.

    Args:
        session_id: Session primary key.
        student_weak_areas: JSON-encoded structured weak-areas data.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE sessions SET student_weak_areas = ? WHERE id = ?",
            (student_weak_areas, session_id),
        )


def insert_session(topic_id: int, duration_min: int, student_quality: int, weak_areas: str | None) -> None:
    """Insert a new session row with the current local timestamp as studied_at.

    Args:
        topic_id: Topic primary key.
        duration_min: Session duration in minutes.
        student_quality: Student self-assessment quality score (2/3/5).
        weak_areas: Optional weak-areas notes.
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions (topic_id, duration_min, student_quality, weak_areas, studied_at)
               VALUES (?, ?, ?, ?, ?)""",
            (topic_id, duration_min, student_quality, weak_areas, local_now()),
        )
