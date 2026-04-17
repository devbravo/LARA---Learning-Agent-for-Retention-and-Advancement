"""
Topic service — database operations for topic lifecycle management.

Extracted from src/webhook_handler.py as part of the telegram package refactor.
"""

from src.repositories import topic_repository


def graduate_topic(topic_id: int) -> str:
    """Promote an in-progress topic to active and reset SM-2 scheduling fields.

    Args:
        topic_id: Database id of the topic to graduate.

    Returns:
        Topic name for user-facing confirmation messages.

    Raises:
        ValueError: If the topic cannot be found.
    """
    updated = topic_repository.graduate_topic_to_active(topic_id)
    if not updated:
        raise ValueError(f"Topic id={topic_id} not found in DB")

    topic_name = topic_repository.get_topic_name_by_id(topic_id)
    if topic_name is None:
        raise ValueError(f"Topic id={topic_id} not found in DB")
    return topic_name


def get_in_progress_topics() -> list[dict[str, int | str]]:
    """Return all in-progress topics ordered by tier then name.

    Returns:
        List of dictionaries with keys ``id`` and ``name``.
    """
    return topic_repository.get_in_progress_topics()


def get_topic_name_by_id(topic_id: int) -> str | None:
    """Return topic name for a topic id, or ``None`` when not found."""
    return topic_repository.get_topic_name_by_id(topic_id)
