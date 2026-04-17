"""Topic-focused SQL query functions."""

from typing import Any

from src.core.db import get_connection


def get_topic_name_by_id(topic_id: int) -> str | None:
    """Return topic name for a topic id, or ``None`` when not found."""
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM topics WHERE id = ?", (topic_id,)).fetchone()
    return row["name"] if row else None


def graduate_topic_to_active(topic_id: int) -> bool:
    """Set topic status to active and reset SM-2 progression fields.

    Returns ``True`` when a row was updated, else ``False``.
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
    """Return in-progress topics ordered by tier and name."""
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
    """Return topic id for a name (case-insensitive), or ``None``."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM topics WHERE name = ? COLLATE NOCASE",
            (topic_name,),
        ).fetchone()
    return row["id"] if row else None


def get_topic_weak_areas_by_name(topic_name: str) -> str | None:
    """Return weak areas text for a topic name (case-insensitive), or ``None``."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT weak_areas FROM topics WHERE name = ? COLLATE NOCASE",
            (topic_name,),
        ).fetchone()
    return row["weak_areas"] if row and row["weak_areas"] else None


def update_topic_weak_areas(topic_id: int, weak_areas: str | None) -> None:
    """Set or clear the operational weak-areas field for a topic."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE topics SET weak_areas = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (weak_areas, topic_id),
        )


def get_inactive_topics_tier1_or2() -> list[dict[str, Any]]:
    """Return inactive tier-1/2 topics ordered by tier and name."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, name, tier FROM topics
               WHERE status = 'inactive' AND tier IN (1, 2)
               ORDER BY tier ASC, name ASC"""
        ).fetchall()
    return [{"id": row["id"], "name": row["name"], "tier": row["tier"]} for row in rows]


def set_topic_in_progress(topic_name: str) -> bool:
    """Set topic status to in_progress for an inactive topic name.

    Returns ``True`` when a row was updated, else ``False``.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE topics SET status = 'in_progress', updated_at = CURRENT_TIMESTAMP
               WHERE name = ? AND status = 'inactive'""",
            (topic_name,),
        )
    return cursor.rowcount > 0

