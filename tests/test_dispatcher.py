"""
Unit tests for src/api/telegram/dispatcher.py

Pure unit tests — no external dependencies. Tests state mutation of
module-level dedup/idempotency sets and per-chat serialization locking.
"""

import threading
import time
import pytest
from unittest.mock import patch

from src.api.telegram import dispatcher
from src.api.telegram.dispatcher import (
    _confirmed_message_ids,
    _in_flight_message_ids,
    _processed_updates,
    _chat_in_flight,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_state():
    """Reset all module-level sets before and after each test."""
    _processed_updates.clear()
    _confirmed_message_ids.clear()
    _in_flight_message_ids.clear()
    _chat_in_flight.clear()
    yield
    _processed_updates.clear()
    _confirmed_message_ids.clear()
    _in_flight_message_ids.clear()
    _chat_in_flight.clear()


# ---------------------------------------------------------------------------
# is_duplicate
# ---------------------------------------------------------------------------

def test_is_duplicate_returns_false_first_time():
    """First call with a new update_id returns False and registers it."""
    assert dispatcher.is_duplicate(1001) is False
    assert 1001 in _processed_updates


def test_is_duplicate_returns_true_second_time():
    """Second call with the same update_id returns True."""
    dispatcher.is_duplicate(1001)
    assert dispatcher.is_duplicate(1001) is True


# ---------------------------------------------------------------------------
# try_mark_in_flight
# ---------------------------------------------------------------------------

def test_try_mark_in_flight_returns_true_first_time():
    """First call for a new message_id marks it in-flight and returns True."""
    assert dispatcher.try_mark_in_flight(500) is True
    assert 500 in _in_flight_message_ids


def test_try_mark_in_flight_returns_false_on_repeat():
    """Second call for the same message_id returns False (already in-flight)."""
    dispatcher.try_mark_in_flight(500)
    assert dispatcher.try_mark_in_flight(500) is False


def test_try_mark_in_flight_returns_false_when_already_confirmed():
    """Returns False when message_id is already in the confirmed set."""
    _confirmed_message_ids.add(500)
    assert dispatcher.try_mark_in_flight(500) is False
    assert 500 not in _in_flight_message_ids


# ---------------------------------------------------------------------------
# mark_confirmed
# ---------------------------------------------------------------------------

def test_mark_confirmed_moves_from_in_flight_to_confirmed():
    """mark_confirmed removes from in-flight and adds to confirmed."""
    dispatcher.try_mark_in_flight(600)
    assert 600 in _in_flight_message_ids

    dispatcher.mark_confirmed(600)

    assert 600 not in _in_flight_message_ids
    assert 600 in _confirmed_message_ids


# ---------------------------------------------------------------------------
# clear_in_flight
# ---------------------------------------------------------------------------

def test_clear_in_flight_removes_from_in_flight():
    """clear_in_flight removes a message_id from the in-flight set."""
    dispatcher.try_mark_in_flight(700)
    assert 700 in _in_flight_message_ids

    dispatcher.clear_in_flight(700)

    assert 700 not in _in_flight_message_ids
    assert 700 not in _confirmed_message_ids


# ---------------------------------------------------------------------------
# invoke_safe — per-chat serialization (_chat_in_flight)
# ---------------------------------------------------------------------------

def test_chat_in_flight_cleared_on_success():
    """_chat_in_flight is released after a successful graph invocation."""
    with patch("src.api.telegram.dispatcher._graph") as mock_graph:
        mock_graph.invoke.return_value = None
        dispatcher.invoke_safe("rate", chat_id=42)

    assert 42 not in _chat_in_flight


def test_chat_in_flight_cleared_on_exception():
    """_chat_in_flight is released even when _graph.invoke raises."""
    with patch("src.api.telegram.dispatcher._graph") as mock_graph:
        mock_graph.invoke.side_effect = RuntimeError("boom")
        dispatcher.invoke_safe("rate", chat_id=42)

    assert 42 not in _chat_in_flight


def test_second_call_waits_for_first_to_finish():
    """Second invoke_safe for the same chat_id starts only after the first completes.

    The first invocation holds _chat_in_flight for ~150 ms. We record
    the order in which each call enters (and exits) _graph.invoke to
    verify strict serialization.
    """
    CHAT_ID = 99
    call_order: list[str] = []
    barrier = threading.Barrier(2)   # synchronises both threads to start together

    def slow_invoke(**_kwargs):
        call_order.append("first_start")
        time.sleep(0.15)
        call_order.append("first_end")

    def fast_invoke(**_kwargs):
        call_order.append("second_start")
        call_order.append("second_end")

    invoke_calls = [slow_invoke, fast_invoke]
    invoke_iter = iter(invoke_calls)
    invoke_lock = threading.Lock()

    def graph_invoke(trigger, chat_id, **kwargs):
        with invoke_lock:
            fn = next(invoke_iter)
        fn(trigger=trigger, chat_id=chat_id, **kwargs)

    with patch("src.api.telegram.dispatcher._graph") as mock_graph:
        mock_graph.invoke.side_effect = graph_invoke

        t1 = threading.Thread(
            target=lambda: (barrier.wait(), dispatcher.invoke_safe("rate", chat_id=CHAT_ID))
        )
        t2 = threading.Thread(
            target=lambda: (barrier.wait(), dispatcher.invoke_safe("rate", chat_id=CHAT_ID))
        )

        t1.start()
        t2.start()
        t1.join(timeout=3)
        t2.join(timeout=3)

    # The second call must not start before the first finishes.
    assert call_order.index("second_start") > call_order.index("first_end"), (
        f"second call started before first finished: {call_order}"
    )
    assert 42 not in _chat_in_flight


def test_non_serialized_triggers_do_not_block():
    """Triggers outside the serialization list do not use _chat_in_flight."""
    with patch("src.api.telegram.dispatcher._graph") as mock_graph:
        mock_graph.invoke.return_value = None
        dispatcher.invoke_safe("confirm", chat_id=55)

    assert 55 not in _chat_in_flight

