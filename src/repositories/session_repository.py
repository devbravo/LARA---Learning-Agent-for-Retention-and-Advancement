"""Session-focused SQL query functions."""

from src.core.db import get_connection


def get_logged_topic_names_for_today() -> set[str]:
    """Return topic names that already have a logged session today."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT t.name FROM sessions s
               JOIN topics t ON t.id = s.topic_id
               WHERE date(s.studied_at) = date('now')"""
        ).fetchall()
    return {row["name"] for row in rows}


def upsert_today_session(topic_id: int, duration_min: int, quality_score: int) -> None:
    """Insert or update today's session row for a topic."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM sessions WHERE topic_id = ? AND DATE(studied_at) = DATE('now')",
            (topic_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE sessions SET quality_score = ?, duration_min = ? WHERE id = ?",
                (quality_score, duration_min, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO sessions (topic_id, duration_min, quality_score) VALUES (?, ?, ?)",
                (topic_id, duration_min, quality_score),
            )


def get_today_session_id(topic_id: int) -> int | None:
    """Return today's session id for a topic, or ``None`` if not logged."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE topic_id = ? AND DATE(studied_at) = DATE('now')",
            (topic_id,),
        ).fetchone()
    return row["id"] if row else None


def update_session_weak_areas(session_id: int, weak_areas: str) -> None:
    """Update weak_areas for a specific session row."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE sessions SET weak_areas = ? WHERE id = ?",
            (weak_areas, session_id),
        )


def insert_session(topic_id: int, duration_min: int, quality_score: int, weak_areas: str | None) -> None:
    """Insert a new session row."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions (topic_id, duration_min, quality_score, weak_areas)
               VALUES (?, ?, ?, ?)""",
            (topic_id, duration_min, quality_score, weak_areas),
        )

