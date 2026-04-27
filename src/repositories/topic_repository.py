"""Topic repository SQL helpers.

This module contains topic-specific database reads and writes used by service
and node layers.
"""

from typing import Any

from src.infrastructure.db import get_connection
from src.repositories import session_repository


def get_topic_name_by_id(topic_id: int) -> str | None:
    """Return topic name for a given id.

    Args:
        topic_id: Topic primary key.

    Returns:
        Topic name, or ``None`` when the row does not exist.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM topics WHERE id = ?", (topic_id,)).fetchone()
    return row["name"] if row else None


def graduate_topic_to_active(topic_id: int) -> bool:
    """Set topic status to active and reset SM-2 progression fields.

    Args:
        topic_id: Topic primary key.

    Returns:
        ``True`` when a row was updated, else ``False``.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE topics
               SET status = 'active',
                   repetitions = 0,
                   easiness_factor = 2.5,
                   next_review = date('now', '+1 day'),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (topic_id,),
        )
    return cursor.rowcount > 0


def get_in_progress_topics() -> list[dict[str, int | str]]:
    """Return in-progress topics ordered by tier and name.

    Returns:
        List of dicts containing ``id`` and ``name``.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name FROM topics WHERE status = 'in_progress' ORDER BY tier ASC, name ASC"
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]


def get_in_progress_topic_names() -> list[str]:
    """Return in-progress topic names ordered by tier and name."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT name FROM topics WHERE status = 'in_progress' ORDER BY tier ASC, name ASC"
        ).fetchall()
    return [row["name"] for row in rows]


def get_topic_id_by_name(topic_name: str) -> int | None:
    """Return topic id for a case-insensitive topic name.

    Args:
        topic_name: Topic display name.

    Returns:
        Topic id, or ``None`` when no match exists.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM topics WHERE name = ? COLLATE NOCASE",
            (topic_name,),
        ).fetchone()
    return row["id"] if row else None


def get_topic_type_by_id(topic_id: int) -> str | None:
    """Return topic_type for a given topic id.

    Args:
        topic_id: Topic primary key.

    Returns:
        Topic type string, or ``None`` when the row does not exist.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT topic_type FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
    return row["topic_type"] if row else None


def get_topic_weak_areas_by_name(topic_name: str) -> str | None:
    """Return weak-areas text for a topic name.

    Args:
        topic_name: Topic display name (case-insensitive lookup).

    Returns:
        Weak-areas string, or ``None`` when missing/empty.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT weak_areas FROM topics WHERE name = ? COLLATE NOCASE",
            (topic_name,),
        ).fetchone()
    return row["weak_areas"] if row and row["weak_areas"] else None


def update_topic_weak_areas(topic_id: int, weak_areas: str | None) -> None:
    """Set or clear operational weak areas for a topic.

    Args:
        topic_id: Topic primary key.
        weak_areas: Weak-areas text or ``None`` to clear the field.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE topics SET weak_areas = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (weak_areas, topic_id),
        )


def get_inactive_topics_tier1_or2() -> list[dict[str, Any]]:
    """Return inactive tier-1/2 topics ordered by tier and name.

    Returns:
        List of dicts with keys ``id``, ``name``, and ``tier``.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, name, tier FROM topics
               WHERE status = 'inactive' AND tier IN (1, 2)
               ORDER BY tier ASC, name ASC"""
        ).fetchall()
    return [{"id": row["id"], "name": row["name"], "tier": row["tier"]} for row in rows]


def fetch_overdue_topics(today_str: str) -> list[dict[str, Any]]:
    """Fetch active topics whose next review date is before today.

    Args:
        today_str: ISO date string (``YYYY-MM-DD``) for the cutoff.

    Returns:
        List of dicts with ``name``, ``next_review``, and ``weak_areas``,
        ordered most-overdue first.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT name, next_review, weak_areas FROM topics
               WHERE status = 'active' AND next_review < ?
               ORDER BY next_review ASC""",
            (today_str,),
        ).fetchall()
    return [{"name": r["name"], "next_review": r["next_review"], "weak_areas": r["weak_areas"]} for r in rows]


def fetch_due_today_topics(today_str: str) -> list[dict[str, Any]]:
    """Fetch active topics due for review today.

    Args:
        today_str: ISO date string (``YYYY-MM-DD``) for today.

    Returns:
        List of dicts with ``name`` and ``weak_areas``, ordered by
        tier then easiness factor.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT name, weak_areas FROM topics
               WHERE status = 'active' AND next_review = ?
               ORDER BY tier ASC, easiness_factor ASC""",
            (today_str,),
        ).fetchall()
    return [{"name": r["name"], "weak_areas": r["weak_areas"]} for r in rows]


def fetch_in_progress_topics_with_weak_areas() -> list[dict[str, Any]]:
    """Fetch in-progress topics with their weak areas.

    Returns:
        List of dicts with ``name`` and ``weak_areas``, ordered by tier
        then name.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT name, weak_areas FROM topics
               WHERE status = 'in_progress'
               ORDER BY tier ASC, name ASC""",
        ).fetchall()
    return [{"name": r["name"], "weak_areas": r["weak_areas"]} for r in rows]


def get_active_unlogged_topics_today() -> list[dict]:
    """Return active topics not yet logged today, ordered by tier ASC, easiness_factor ASC.

    Returns:
        List of dicts with keys ``id`` and ``name``.
    """
    logged_names = session_repository.get_logged_topic_names_for_today()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, name FROM topics
               WHERE status = 'active'
               ORDER BY tier ASC, easiness_factor ASC"""
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows if row["name"] not in logged_names]


def get_topic_context(topic_id: int) -> dict[str, Any]:
    """Fetch SM-2 state and last session signal for a topic.

    Joins topics with sessions (latest session only) to return both the
    SM-2 scheduling fields and the most recent student signal.

    Args:
        topic_id: Topic primary key.

    Returns:
        Dict with topic SM-2 fields and last-session data (session fields
        are ``None`` when no session exists yet).
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT
                   t.id, t.name, t.topic_type, t.easiness_factor,
                   t.interval_days, t.repetitions, t.next_review, t.weak_areas,
                   s.student_quality, s.studied_at, s.student_weak_areas
               FROM topics t
               LEFT JOIN sessions s ON s.topic_id = t.id
               WHERE t.id = ?
               ORDER BY s.studied_at DESC
               LIMIT 1""",
            (topic_id,),
        ).fetchone()
    if row is None:
        return {}
    return {
        "id": row["id"],
        "name": row["name"],
        "topic_type": row["topic_type"],
        "easiness_factor": row["easiness_factor"],
        "interval_days": row["interval_days"],
        "repetitions": row["repetitions"],
        "next_review": row["next_review"],
        "weak_areas": row["weak_areas"],
        "student_quality": row["student_quality"],
        "studied_at": row["studied_at"],
        "student_weak_areas": row["student_weak_areas"],
    }


def set_topic_in_progress(topic_name: str) -> bool:
    """Set topic status to in_progress for an inactive topic name.

    Args:
        topic_name: Topic display name.

    Returns:
        ``True`` when a row was updated, else ``False``.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE topics SET status = 'in_progress', updated_at = CURRENT_TIMESTAMP
               WHERE name = ? AND status = 'inactive'""",
            (topic_name,),
        )
    return cursor.rowcount > 0

