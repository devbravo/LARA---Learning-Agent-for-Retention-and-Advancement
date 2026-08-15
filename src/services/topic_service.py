"""
Topic service — database operations for topic lifecycle management.

Extracted from src/webhook_handler.py as part of the telegram package refactor.
"""

from src.repositories import topic_repository


def graduate_topic(engineer_id: int, topic_id: int) -> str:
    """Promote an engineer's in-progress topic to active and reset SM-2 fields.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Database id of the topic to graduate.

    Returns:
        Topic name for user-facing confirmation messages.

    Raises:
        ValueError: If the topic does not exist in the catalog, or the topic
            exists but this engineer has no progress row for it yet.
    """
    updated = topic_repository.graduate_topic_to_active(engineer_id, topic_id)
    topic_name = topic_repository.get_topic_name_by_id(topic_id)
    if not updated:
        if topic_name is None:
            raise ValueError(f"Topic id={topic_id} not found in catalog")
        raise ValueError(
            f"Engineer id={engineer_id} has no progress record for "
            f"topic id={topic_id} ('{topic_name}')"
        )
    return topic_name


def get_in_progress_topics(engineer_id: int) -> list[dict[str, int | str]]:
    """Return all of an engineer's in-progress topics ordered by tier then name.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        List of dictionaries with keys ``id`` and ``name``.
    """
    return topic_repository.get_in_progress_topics(engineer_id)


def get_topic_name_by_id(topic_id: int) -> str | None:
    """Return catalog topic name for a topic id, or ``None`` when not found."""
    return topic_repository.get_topic_name_by_id(topic_id)
