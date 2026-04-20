"""Session repository SQL helpers."""

from src.infrastructure.db import get_connection
from src.infrastructure.time import local_now, local_today


def get_logged_topic_names_for_today() -> set[str]:
    """Return topic names that already have a logged session today (local date)."""
    today = local_today()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT t.name FROM sessions s
               JOIN topics t ON t.id = s.topic_id
               WHERE DATE(s.studied_at) = ?""",
            (today,),
        ).fetchall()
    return {row["name"] for row in rows}


def upsert_today_session(topic_id: int, duration_min: int, quality_score: int) -> None:
    """Insert or update today's session row for a topic (local date).

    Args:
        topic_id: Topic primary key.
        duration_min: Session duration in minutes.
        quality_score: Session quality score (2/3/5).
    """
    today = local_today()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM sessions WHERE topic_id = ? AND DATE(studied_at) = ?",
            (topic_id, today),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE sessions SET quality_score = ?, duration_min = ? WHERE id = ?",
                (quality_score, duration_min, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO sessions (topic_id, duration_min, quality_score, studied_at) VALUES (?, ?, ?, ?)",
                (topic_id, duration_min, quality_score, local_now()),
            )


def get_today_session_id(topic_id: int) -> int | None:
    """Return today's session id for a topic (local date).

    Args:
        topic_id: Topic primary key.

    Returns:
        Session id when present, else ``None``.
    """
    today = local_today()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE topic_id = ? AND DATE(studied_at) = ?",
            (topic_id, today),
        ).fetchone()
    return row["id"] if row else None


def update_session_weak_areas(session_id: int, weak_areas: str) -> None:
    """Update weak-areas notes for a specific session row.

    Args:
        session_id: Session primary key.
        weak_areas: Free-text weak-areas notes.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE sessions SET weak_areas = ? WHERE id = ?",
            (weak_areas, session_id),
        )


def insert_session(topic_id: int, duration_min: int, quality_score: int, weak_areas: str | None) -> None:
    """Insert a new session row with the current local timestamp as studied_at.

    Args:
        topic_id: Topic primary key.
        duration_min: Session duration in minutes.
        quality_score: Session quality score (2/3/5).
        weak_areas: Optional weak-areas notes.
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions (topic_id, duration_min, quality_score, weak_areas, studied_at)
               VALUES (?, ?, ?, ?, ?)""",
            (topic_id, duration_min, quality_score, weak_areas, local_now()),
        )

