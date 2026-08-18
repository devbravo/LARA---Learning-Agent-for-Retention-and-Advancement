"""Session repository SQL helpers.

Every function is engineer-scoped and takes ``engineer_id: int`` as its first
parameter, EXCEPT ``update_session_weak_areas``/``update_session_student_weak_areas``
— those are keyed by an already-unique ``session_id``, so no additional scoping
is needed.
"""

import json
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


def get_logged_topic_names_for_today(engineer_id: int) -> set[str]:
    """Return topic names an engineer already has a student-rated session for today (local date).

    Only counts rows where student_quality IS NOT NULL — teacher-only rows
    created by the MCP log_session tool are not considered fully logged until
    the student provides their rating via /done.

    Matches new local-time rows by calendar date and legacy UTC rows by the
    UTC window that corresponds to the current local day.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        Set of topic names logged by this engineer today.
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT t.name FROM sessions s
               JOIN topic_catalog t ON t.id = s.topic_id
               WHERE s.engineer_id = ?
                 AND s.student_quality IS NOT NULL
                 AND (DATE(s.studied_at) = ?
                      OR (s.studied_at >= ? AND s.studied_at < ?))""",
            (engineer_id, local, utc_start, utc_end),
        ).fetchall()
    return {row["name"] for row in rows}


def upsert_today_session(
    engineer_id: int, topic_id: int, duration_min: int, student_quality: int
) -> None:
    """Insert or update an engineer's session row for today (local date).

    Matches new local-time rows by calendar date and legacy UTC rows by the
    UTC window that corresponds to the current local day, preventing duplicate
    rows during the migration transition period.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.
        duration_min: Session duration in minutes.
        student_quality: Student self-assessment quality score (2/3/5).
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    with get_connection() as conn:
        existing = conn.execute(
            """SELECT id, teacher_quality FROM sessions
               WHERE engineer_id = ? AND topic_id = ?
                 AND (DATE(studied_at) = ?
                      OR (studied_at >= ? AND studied_at < ?))""",
            (engineer_id, topic_id, local, utc_start, utc_end),
        ).fetchone()
        if existing:
            teacher_quality = existing["teacher_quality"]
            calibration_gap = (
                student_quality - teacher_quality if teacher_quality is not None else None
            )
            conn.execute(
                """UPDATE sessions
                   SET student_quality = ?, duration_min = ?, calibration_gap = ?
                   WHERE id = ?""",
                (student_quality, duration_min, calibration_gap, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO sessions (engineer_id, topic_id, duration_min, student_quality, studied_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (engineer_id, topic_id, duration_min, student_quality, local_now()),
            )


def get_today_teacher_quality(engineer_id: int, topic_id: int) -> int | None:
    """Return an engineer's teacher_quality for today's session if present, else None.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.

    Returns:
        Teacher quality score (2, 3, or 5) or ``None`` when no teacher
        assessment has been logged for today.
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    with get_connection() as conn:
        row = conn.execute(
            """SELECT teacher_quality FROM sessions
               WHERE engineer_id = ? AND topic_id = ?
                 AND (DATE(studied_at) = ?
                      OR (studied_at >= ? AND studied_at < ?))""",
            (engineer_id, topic_id, local, utc_start, utc_end),
        ).fetchone()
    return row["teacher_quality"] if row else None


def get_today_session_id(engineer_id: int, topic_id: int) -> int | None:
    """Return an engineer's today session id for a topic (local date).

    Matches new local-time rows by calendar date and legacy UTC rows by the
    UTC window that corresponds to the current local day.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.

    Returns:
        Session id when present, else ``None``.
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    with get_connection() as conn:
        row = conn.execute(
            """SELECT id FROM sessions
               WHERE engineer_id = ? AND topic_id = ?
                 AND (DATE(studied_at) = ?
                      OR (studied_at >= ? AND studied_at < ?))""",
            (engineer_id, topic_id, local, utc_start, utc_end),
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


def log_teacher_session(
    engineer_id: int,
    topic_id: int,
    teacher_quality: int,
    teacher_weak_areas: dict,
    teacher_source: str,
    mode: str,
) -> int | None:
    """Log an engineer's teacher assessment for today's session, creating a row if none exists.

    Matches today's session using the same local/UTC range pattern as
    ``upsert_today_session``. Updates teacher fields on an existing row;
    inserts a new row with student fields null when no row exists.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.
        teacher_quality: Teacher quality score (2, 3, or 5).
        teacher_weak_areas: Structured weak areas dict (serialized to JSON).
        teacher_source: Source identifier ('claude' or 'algomonster').
        mode: Session mode ('mock' or 'discuss').

    Returns:
        ``calibration_gap`` (student_quality − teacher_quality) when
        student_quality is present on the row, else ``None``.
    """
    local = local_today()
    utc_start, utc_end = _legacy_utc_range()
    weak_areas_json = json.dumps(teacher_weak_areas)

    with get_connection() as conn:
        existing = conn.execute(
            """SELECT id, student_quality FROM sessions
               WHERE engineer_id = ? AND topic_id = ?
                 AND (DATE(studied_at) = ?
                      OR (studied_at >= ? AND studied_at < ?))""",
            (engineer_id, topic_id, local, utc_start, utc_end),
        ).fetchone()

        if existing:
            student_quality = existing["student_quality"]
            calibration_gap = (
                student_quality - teacher_quality if student_quality is not None else None
            )
            conn.execute(
                """UPDATE sessions
                   SET teacher_quality = ?,
                       teacher_weak_areas = ?,
                       teacher_source = ?,
                       mode = ?,
                       calibration_gap = ?
                   WHERE id = ?""",
                (teacher_quality, weak_areas_json, teacher_source, mode,
                 calibration_gap, existing["id"]),
            )
            return calibration_gap
        else:
            conn.execute(
                """INSERT INTO sessions
                       (engineer_id, topic_id, studied_at, teacher_quality, teacher_weak_areas,
                        teacher_source, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (engineer_id, topic_id, local_now(), teacher_quality, weak_areas_json,
                 teacher_source, mode),
            )
            return None


def insert_session(
    engineer_id: int, topic_id: int, duration_min: int, student_quality: int, weak_areas: str | None
) -> None:
    """Insert a new session row for an engineer with the current local timestamp as studied_at.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.
        duration_min: Session duration in minutes.
        student_quality: Student self-assessment quality score (2/3/5).
        weak_areas: Optional weak-areas notes.
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions (engineer_id, topic_id, duration_min, student_quality, weak_areas, studied_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (engineer_id, topic_id, duration_min, student_quality, weak_areas, local_now()),
        )


def get_discuss_session_count(engineer_id: int, topic_id: int) -> int:
    """Count an engineer's discuss-mode sessions for a topic.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.

    Returns:
        Number of rows where ``mode = 'discuss'`` for the given engineer and topic.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sessions WHERE engineer_id = ? AND topic_id = ? AND mode = 'discuss'",
            (engineer_id, topic_id),
        ).fetchone()
    return row["cnt"] if row else 0


def get_discuss_sessions(engineer_id: int, topic_id: int, limit: int = 5) -> list[dict]:
    """Return an engineer's recent discuss-mode sessions for a topic.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.
        limit: Maximum number of rows to return (most recent first).

    Returns:
        List of dicts with keys ``teacher_quality``, ``teacher_weak_areas``,
        and ``studied_at``, ordered by ``studied_at`` DESC.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT teacher_quality, teacher_weak_areas, studied_at
               FROM sessions
               WHERE engineer_id = ? AND topic_id = ? AND mode = 'discuss'
               ORDER BY studied_at DESC
               LIMIT ?""",
            (engineer_id, topic_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_mock_sessions(engineer_id: int, topic_id: int, limit: int = 5) -> list[dict]:
    """Return an engineer's recent mock-mode sessions for a topic (includes legacy NULL-mode rows).

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.
        limit: Maximum number of rows to return (most recent first).

    Returns:
        List of dicts with keys ``quality`` (COALESCE of teacher/student/legacy
        quality scores), ``weak_areas`` (COALESCE of ``teacher_weak_areas`` and
        the legacy ``weak_areas`` column), and ``studied_at``, ordered by
        ``studied_at`` DESC.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT COALESCE(teacher_quality, student_quality, quality_score) AS quality,
                      COALESCE(teacher_weak_areas, weak_areas) AS weak_areas,
                      studied_at
               FROM sessions
               WHERE engineer_id = ? AND topic_id = ? AND (mode = 'mock' OR mode IS NULL)
               ORDER BY studied_at DESC
               LIMIT ?""",
            (engineer_id, topic_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def has_mock_history(engineer_id: int, topic_id: int) -> bool:
    """Return True if the engineer has any mock or legacy session for the topic.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.

    Returns:
        ``True`` when at least one row exists with ``mode = 'mock'`` or
        ``mode IS NULL``; ``False`` otherwise.
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM sessions
               WHERE engineer_id = ? AND topic_id = ? AND (mode = 'mock' OR mode IS NULL)
               LIMIT 1""",
            (engineer_id, topic_id),
        ).fetchone()
    return row is not None


def insert_discuss_session(
    engineer_id: int,
    topic_id: int,
    teacher_quality: int,
    teacher_weak_areas: str,
) -> None:
    """Insert a new discuss-mode session row for an engineer with the current local timestamp.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic primary key.
        teacher_quality: Teacher quality score (2, 3, or 5).
        teacher_weak_areas: Structured weak-areas text (typically JSON).
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions
               (engineer_id, topic_id, studied_at, mode, teacher_quality, teacher_weak_areas, teacher_source)
               VALUES (?, ?, ?, 'discuss', ?, ?, 'claude')""",
            (engineer_id, topic_id, local_now(), teacher_quality, teacher_weak_areas),
        )
