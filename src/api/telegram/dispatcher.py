"""Webhook dispatch utilities: deduplication, idempotency, and HITL-safe invocation.

This module centralises mutable in-memory state used by webhook handling:
- seen update ids (duplicate delivery guard),
- in-flight / confirmed message ids (repeat-tap idempotency),
- safe graph invocation supporting both fresh triggers and Command(resume=…).

The only place in the HTTP layer that reads graph state — solely to check
has_pending_interrupt() before deciding between fresh invoke and resume.
"""

import logging
import threading
from typing import Any

from langgraph.types import Command

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

# Per-chat in-flight lock
_chat_in_flight: set[int] = set()
_chat_lock = threading.Lock()


def is_duplicate(update_id: int) -> bool:
    """Return True if update_id was already processed; register it otherwise."""
    if update_id in _processed_updates:
        return True
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED:
        _processed_updates.discard(min(_processed_updates))
    return False


def try_mark_in_flight(message_id: int) -> bool:
    """Claim message_id for single processing. Returns False on duplicate tap."""
    with _confirm_lock:
        if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
            return False
        _in_flight_message_ids.add(message_id)
        return True


def mark_confirmed(message_id: int) -> None:
    """Move message_id from in-flight to confirmed."""
    with _confirm_lock:
        _in_flight_message_ids.discard(message_id)
        _confirmed_message_ids.add(message_id)
        if len(_confirmed_message_ids) > _MAX_CONFIRMED:
            _confirmed_message_ids.discard(min(_confirmed_message_ids))


def clear_in_flight(message_id: int) -> None:
    """Release message_id from in-flight tracking (e.g. on error)."""
    with _confirm_lock:
        _in_flight_message_ids.discard(message_id)


def has_pending_interrupt(state: Any) -> bool:
    """Return True when the graph is paused at an interrupt() call."""
    tasks = getattr(state, "tasks", [])
    return any(getattr(t, "interrupts", None) for t in tasks)


def resolve_trigger(payload: str) -> str:
    """Map a command string to its graph trigger name."""
    mapping = {
        "/done":     "done",
        "/study":    "study",
        "/plan":     "daily",
        "/pick":     "pick",
        "/activate": "activate",
    }
    return mapping.get(payload.lower().strip(), payload)


def invoke_safe(chat_id: int, payload: str, **kwargs: Any) -> None:
    """Invoke the graph safely, choosing resume vs fresh based on interrupt state.

    Reads graph state once to check has_pending_interrupt(). If an interrupt
    is pending, resumes with Command(resume=payload). Otherwise resolves payload
    to a trigger and starts a fresh invocation.
    """
    config = {"configurable": {"thread_id": str(chat_id)}}

    # Serialize per-chat to prevent race conditions on concurrent webhooks
    wait_event = threading.Event()
    logged_wait = False
    while True:
        with _chat_lock:
            if chat_id not in _chat_in_flight:
                _chat_in_flight.add(chat_id)
                break
        if not logged_wait:
            logger.info("Waiting for prior invocation to finish for chat_id=%s", chat_id)
            logged_wait = True
        wait_event.wait(0.05)

    try:
        state_snapshot = _graph.graph.get_state(config)

        if has_pending_interrupt(state_snapshot):
            logger.info("Resuming interrupted graph: chat_id=%s payload=%r", chat_id, payload)
            _graph.graph.invoke(Command(resume=payload), config=config)
        else:
            trigger = resolve_trigger(payload)
            logger.info("Fresh graph invocation: trigger=%s chat_id=%s", trigger, chat_id)
            initial_state: dict = {"trigger": trigger, "chat_id": chat_id}
            for key in ("message_id", "duration_min", "proposed_topic", "proposed_slot",
                        "quality_score", "messages", "current_topic_id", "current_topic_name",
                        "study_topic_category"):
                if kwargs.get(key) is not None:
                    initial_state[key] = kwargs[key]
            _graph.graph.invoke(initial_state, config=config)

        logger.info("Graph invocation complete: chat_id=%s", chat_id)

    except Exception as e:
        logger.error(
            "Graph invocation failed [chat_id=%s payload=%r]: %s",
            chat_id, payload, e, exc_info=True,
        )
    finally:
        with _chat_lock:
            _chat_in_flight.discard(chat_id)
