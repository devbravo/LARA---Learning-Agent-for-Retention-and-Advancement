"""
Dispatcher — invocation control, deduplication, and idempotency state.

Owns all dedup sets and the invoke_safe function.
All state is module-level so it persists across requests in the same process.
"""

import logging
import os
import threading

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


def is_duplicate(update_id: int) -> bool:
    """Check and register update_id. Returns True if already seen (duplicate)."""
    if update_id in _processed_updates:
        return True
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED:
        _processed_updates.discard(min(_processed_updates))
    return False


def try_mark_in_flight(message_id: int) -> bool:
    """
    Attempt to claim message_id as in-flight.
    Returns False if already in-flight or confirmed (repeat tap); True if newly claimed.
    """
    with _confirm_lock:
        if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
            return False
        _in_flight_message_ids.add(message_id)
        return True


def mark_confirmed(message_id: int) -> None:
    """Move message_id from in-flight to confirmed (called on successful invocation)."""
    with _confirm_lock:
        _in_flight_message_ids.discard(message_id)
        _confirmed_message_ids.add(message_id)
        if len(_confirmed_message_ids) > _MAX_CONFIRMED:
            _confirmed_message_ids.discard(min(_confirmed_message_ids))


def clear_in_flight(message_id: int) -> None:
    """Remove message_id from in-flight (called on error)."""
    with _confirm_lock:
        _in_flight_message_ids.discard(message_id)


def invoke_safe(trigger: str, chat_id: int, **kwargs) -> None:
    """Invoke the graph, catching all exceptions so executor threads never crash."""
    message_id: int | None = kwargs.get("message_id")
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
