"""Webhook dispatch utilities: deduplication, idempotency, and safe invocation.

This module centralizes mutable in-memory state used by webhook handling:
- seen update ids (duplicate delivery guard),
- in-flight / confirmed message ids (repeat-tap idempotency),
- safe graph invocation that never propagates exceptions to executor threads.
"""

import logging
import os
import threading
from typing import Any

from src.agent import graph as _graph

logger = logging.getLogger(__name__)

# Deduplication guard — keeps last 1000 processed update_ids in memory
_processed_updates: set[int] = set()
_MAX_PROCESSED = 1000

# Idempotency — tracks message_ids to prevent double-booking / double-rating
_confirmed_message_ids: set[int] = set()
_in_flight_message_ids: set[int] = set()
_MAX_CONFIRMED = 1000
_confirm_lock = threading.Lock()

# Per-chat in-flight lock — prevents concurrent graph invocations for the same chat_id
# (e.g. double-delivered weak_areas webhooks)
_chat_in_flight: set[int] = set()
_chat_lock = threading.Lock()


def is_duplicate(update_id: int) -> bool:
    """Check whether an update id has already been processed.
    Args:
        update_id: Telegram ``update_id``.
    Returns:
        ``True`` if the id was already seen; ``False`` if newly registered.
    """
    if update_id in _processed_updates:
        return True
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED:
        _processed_updates.discard(min(_processed_updates))
    return False


def try_mark_in_flight(message_id: int) -> bool:
    """Attempt to claim a message id for single processing.
    Args:
        message_id: Telegram message id associated with callback buttons.
    Returns:
        ``False`` when already in-flight/confirmed (repeat tap), otherwise
        ``True`` after marking the id as in-flight.
    """
    with _confirm_lock:
        if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
            return False
        _in_flight_message_ids.add(message_id)
        return True


def mark_confirmed(message_id: int) -> None:
    """Mark a message id as confirmed and clear its in-flight state.
    Args:
        message_id: Telegram message id that completed successfully.
    """
    with _confirm_lock:
        _in_flight_message_ids.discard(message_id)
        _confirmed_message_ids.add(message_id)
        if len(_confirmed_message_ids) > _MAX_CONFIRMED:
            _confirmed_message_ids.discard(min(_confirmed_message_ids))


def clear_in_flight(message_id: int) -> None:
    """Remove a message id from in-flight tracking.
    Args:
        message_id: Telegram message id to release, typically on error.
    """
    with _confirm_lock:
        _in_flight_message_ids.discard(message_id)


def invoke_safe(trigger: str, chat_id: int, **kwargs: Any) -> None:
    """Invoke the graph while preventing thread-level crashes.
    Args:
        trigger: Graph trigger to invoke.
        chat_id: Telegram chat id used as LangGraph thread id.
        **kwargs: Optional state payload forwarded to graph invocation.
    Notes:
        On success, eligible message ids are moved to confirmed state.
        On failure, in-flight state is cleared and the exception is logged.
    """
    message_id: int | None = kwargs.get("message_id")

    # Serialize all done-flow triggers per chat to prevent the race condition where
    # the user taps a rating button before the previous node's checkpoint write completes,
    # causing log_session to read a stale current_topic_id.
    # Also prevents double-delivery of weak_areas text messages.
    if trigger in ("done", "rate", "weak_areas", "study_topic", "study_topic_category"):
        wait_event = threading.Event()
        logged_wait = False
        while True:
            with _chat_lock:
                if chat_id not in _chat_in_flight:
                    _chat_in_flight.add(chat_id)
                    break
            if not logged_wait:
                logger.info(
                    "Waiting for prior %s invocation to finish for chat_id=%s",
                    trigger, chat_id,
                )
                logged_wait = True
            wait_event.wait(0.05)

    try:
        logger.info("Invoking graph: trigger=%s, chat_id=%s", trigger, chat_id)
        _graph.invoke(trigger=trigger, chat_id=chat_id, **kwargs)
        state_db_path = os.path.abspath("db/state.db")
        try:
            logger.debug("Graph invoke done, checking state.db size: %s", state_db_path)
            logger.debug("state.db size: %s bytes", os.path.getsize(state_db_path))
        except OSError as e:
            logger.debug("Unable to read state.db size at %s: %s", state_db_path, e)
        logger.info("Graph invocation complete: trigger=%s", trigger)
        if trigger in ("confirm", "on_demand", "rate", "study_topic_confirm", "studied") and message_id is not None:
            mark_confirmed(message_id)
    except Exception as e:
        logger.error(
            "Graph invocation failed [trigger=%s chat_id=%s]: %s",
            trigger, chat_id, e, exc_info=True,
        )
        if message_id is not None:
            clear_in_flight(message_id)
    finally:
        if trigger in ("done", "rate", "weak_areas", "study_topic", "study_topic_category"):
            with _chat_lock:
                _chat_in_flight.discard(chat_id)
