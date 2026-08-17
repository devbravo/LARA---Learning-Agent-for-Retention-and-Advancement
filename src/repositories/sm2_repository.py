"""SM-2 repository SQL helpers.

All functions are engineer-scoped and take ``engineer_id: int`` as their
first parameter — SM-2 progress lives in ``engineer_topic_progress``, joined
to ``topic_catalog`` for shared fields (name, tier). See
``src/repositories/topic_repository.py`` for the lazy-row convention: an
engineer with no progress row for a topic is treated as inactive and is
correctly excluded from due-topic queries (which only ever return 'active'
rows, so a plain JOIN is sufficient — no LEFT JOIN needed here).
"""

from datetime import date

from src.infrastructure.db import get_connection


def fetch_due_topics(engineer_id: int, target_date: date) -> list[dict]:
    """Fetch an engineer's active topics due on or before a date.

    Args:
        engineer_id: Engineer primary key.
        target_date: Due-date cutoff.

    Returns:
        List of due topic dictionaries ordered by tier and easiness factor.
    """
    date_str = target_date.isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT tc.id, tc.name, tc.tier, etp.easiness_factor, etp.interval_days,
                   etp.repetitions, etp.next_review, etp.weak_areas
            FROM engineer_topic_progress etp
            JOIN topic_catalog tc ON tc.id = etp.topic_id
            WHERE etp.engineer_id = ?
              AND etp.next_review <= ?
              AND etp.status = 'active'
            ORDER BY tc.tier ASC, etp.easiness_factor ASC
            """,
            (engineer_id, date_str),
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_sm2_state(engineer_id: int, topic_id: int) -> dict | None:
    """Fetch an engineer's SM-2 state fields for a topic id.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.

    Returns:
        Dict containing ``easiness_factor``, ``interval_days``, and
        ``repetitions``; ``None`` when the engineer has no progress row for
        this topic (lazy-row semantics, or the topic doesn't exist).
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT easiness_factor, interval_days, repetitions
               FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?""",
            (engineer_id, topic_id),
        ).fetchone()
    return dict(row) if row else None


def update_sm2_state(
    engineer_id: int,
    topic_id: int,
    easiness_factor: float,
    interval_days: int,
    repetitions: int,
    next_review: str,
) -> None:
    """Persist recalculated SM-2 fields for an engineer's progress on a topic.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.
        easiness_factor: Updated easiness factor.
        interval_days: Updated review interval in days.
        repetitions: Updated repetition count.
        next_review: Next review date in ISO format.
    """
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE engineer_topic_progress
            SET easiness_factor = ?,
                interval_days   = ?,
                repetitions     = ?,
                next_review     = ?,
                updated_at      = CURRENT_TIMESTAMP
            WHERE engineer_id = ? AND topic_id = ?
            """,
            (easiness_factor, interval_days, repetitions, next_review, engineer_id, topic_id),
        )
