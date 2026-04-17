"""
Topic service — database operations for topic lifecycle management.

Extracted from src/webhook_handler.py as part of the telegram package refactor.
"""

from src.core.db import get_connection


def graduate_topic(topic_id: int) -> str:
    """
    Set topic status to active, reset SM-2 state.
    Returns topic name on success.
    Raises ValueError if topic not found.
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
        if cursor.rowcount == 0:
            raise ValueError(f"Topic id={topic_id} not found in DB")
        topic_name = conn.execute(
            "SELECT name FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()["name"]
    return topic_name


def get_in_progress_topics() -> list[dict]:
    """
    Return all in_progress topics ordered by tier ASC, name ASC.
    Returns list of dicts with keys: id, name.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name FROM topics WHERE status = 'in_progress' ORDER BY tier ASC, name ASC"
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]
