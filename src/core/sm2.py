"""SM-2 scheduling helpers and SQLite persistence utilities.

This module contains pure interval/EF calculation plus DB read/write helpers
for due-topic selection and post-session updates.
"""

from datetime import date, timedelta

from src.repositories import sm2_repository, topic_repository


def calculate_next_review(
    quality: int,
    easiness_factor: float,
    interval_days: int,
    repetitions: int,
) -> tuple[float, int, int]:
    """
    Returns (new_easiness_factor, new_interval_days, new_repetitions).

    SM-2 rules:
    - quality < 3:  reset interval to 1, reset repetitions to 0
    - quality >= 3:
        - repetitions == 0: interval = 1
        - repetitions == 1: interval = 6
        - repetitions > 1:  interval = round(interval * easiness_factor)
    - EF update: ef + (0.1 - (5-q) * (0.08 + (5-q) * 0.02))
    - EF minimum: 1.3
    - Increment repetitions by 1 if quality >= 3
    """
    new_ef = easiness_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ef = max(1.3, new_ef)

    if quality < 3:
        return new_ef, 1, 0

    if repetitions == 0:
        new_interval = 1
    elif repetitions == 1:
        new_interval = 6
    else:
        new_interval = round(interval_days * easiness_factor)

    return new_ef, new_interval, repetitions + 1


def get_due_topics(engineer_id: int, target_date: date | None = None) -> list[dict]:
    """Return an engineer's topics due on or before target_date, ordered by tier ASC, easiness_factor ASC.

    Args:
        engineer_id: Engineer primary key.
        target_date: The date to check due topics against. Defaults to today.
                     Pass date.today() + timedelta(days=1) for tomorrow's due topics
                     (used by the evening briefing).

    Returns:
        List of topic rows as dictionaries.
    """
    if target_date is None:
        target_date = date.today()
    return sm2_repository.fetch_due_topics(engineer_id=engineer_id, target_date=target_date)


def update_topic_after_session(engineer_id: int, topic_id: int = 0, quality: int = 3) -> None:
    """Recompute and persist SM-2 fields for an engineer's studied topic.

    Unlike the read-path lazy-row helpers in topic_repository (which treat a
    missing progress row as 'inactive' with SM-2 defaults), this function
    deliberately raises instead of upserting a default row. A session should
    never reach this point for a topic the engineer hasn't activated —
    get_active_unlogged_topics_today only surfaces 'active' topics, and both
    activation paths (graduate_topic_to_active, activate_topic_from_discuss)
    already reject a missing progress row before returning success. If this
    branch fires, it means a session got logged without a valid activation
    record, i.e. a genuine data-integrity bug — silently fabricating a
    plausible-looking progress row here would hide that bug rather than
    surface it.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.
        quality: Session quality score (2, 3, or 5).

    Raises:
        ValueError: If the topic doesn't exist in the catalog, or the topic
            exists but this engineer has no progress row for it yet.
    """
    row = sm2_repository.fetch_sm2_state(engineer_id=engineer_id, topic_id=topic_id)
    if row is None:
        topic_name = topic_repository.get_topic_name_by_id(topic_id)
        if topic_name is None:
            raise ValueError(f"Topic id={topic_id} not found in catalog")
        raise ValueError(
            f"Engineer id={engineer_id} has no progress record for "
            f"topic id={topic_id} ('{topic_name}')"
        )

    new_ef, new_interval, new_reps = calculate_next_review(
        quality=quality,
        easiness_factor=row["easiness_factor"],
        interval_days=row["interval_days"],
        repetitions=row["repetitions"],
    )
    next_review = (date.today() + timedelta(days=new_interval)).isoformat()

    sm2_repository.update_sm2_state(
        engineer_id=engineer_id,
        topic_id=topic_id,
        easiness_factor=new_ef,
        interval_days=new_interval,
        repetitions=new_reps,
        next_review=next_review,
    )
