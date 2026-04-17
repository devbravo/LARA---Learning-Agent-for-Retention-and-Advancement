"""SM-2 related SQL query functions."""

import sqlite3
from datetime import date


def fetch_due_topics(path: str, target_date: date) -> list[dict]:
    """Fetch active topics due on or before ``target_date``."""
    date_str = target_date.isoformat()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, name, tier, easiness_factor, interval_days, repetitions, next_review, weak_areas
            FROM topics
            WHERE next_review <= ?
              AND status = 'active'
            ORDER BY tier ASC, easiness_factor ASC
            """,
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def fetch_sm2_state(path: str, topic_id: int) -> dict | None:
    """Fetch SM-2 fields for a topic id, or ``None`` when not found."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT easiness_factor, interval_days, repetitions FROM topics WHERE id = ?",
            (topic_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def update_sm2_state(
    path: str,
    topic_id: int,
    easiness_factor: float,
    interval_days: int,
    repetitions: int,
    next_review: str,
) -> None:
    """Persist recalculated SM-2 fields for a topic."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            UPDATE topics
            SET easiness_factor = ?,
                interval_days   = ?,
                repetitions     = ?,
                next_review     = ?,
                updated_at      = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (easiness_factor, interval_days, repetitions, next_review, topic_id),
        )
        conn.commit()
    finally:
        conn.close()

