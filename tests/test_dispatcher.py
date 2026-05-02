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

def _mock_graph_no_interrupt():
    """Return a mock graph where get_state reports no pending interrupt.

    Both ``tasks`` and ``next`` must be explicitly empty — ``has_pending_interrupt``
    falls back to ``bool(state.next)`` and a MagicMock-auto-created attribute
    would be truthy, taking the resume branch by accident.
    """
    from unittest.mock import MagicMock
    mock_graph = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.tasks = []  # no pending interrupt
    mock_snapshot.next = ()   # graph has finished, no pending node
    mock_graph.graph.get_state.return_value = mock_snapshot
    mock_graph.graph.invoke.return_value = None
    return mock_graph


def test_chat_in_flight_cleared_on_success():
    """_chat_in_flight is released after a successful graph invocation."""
    with patch("src.api.telegram.dispatcher._graph", _mock_graph_no_interrupt()):
        dispatcher.invoke_safe(42, "/done")

    assert 42 not in _chat_in_flight


def test_chat_in_flight_cleared_on_exception():
    """_chat_in_flight is released even when graph.invoke raises."""
    mock_graph = _mock_graph_no_interrupt()
    mock_graph.graph.invoke.side_effect = RuntimeError("boom")
    with patch("src.api.telegram.dispatcher._graph", mock_graph):
        dispatcher.invoke_safe(42, "/done")

    assert 42 not in _chat_in_flight


def test_second_call_waits_for_first_to_finish():
    """Second invoke_safe for the same chat_id starts only after the first completes."""
    CHAT_ID = 99
    call_order: list[str] = []
    barrier = threading.Barrier(2)

    invoke_count = [0]
    invoke_lock = threading.Lock()

    def graph_invoke(state_or_cmd, config=None):
        with invoke_lock:
            invoke_count[0] += 1
            n = invoke_count[0]
        if n == 1:
            call_order.append("first_start")
            time.sleep(0.15)
            call_order.append("first_end")
        else:
            call_order.append("second_start")
            call_order.append("second_end")

    mock_graph = _mock_graph_no_interrupt()
    mock_graph.graph.invoke.side_effect = graph_invoke

    with patch("src.api.telegram.dispatcher._graph", mock_graph):
        t1 = threading.Thread(
            target=lambda: (barrier.wait(), dispatcher.invoke_safe(CHAT_ID, "/done"))
        )
        t2 = threading.Thread(
            target=lambda: (barrier.wait(), dispatcher.invoke_safe(CHAT_ID, "/done"))
        )

        t1.start()
        t2.start()
        t1.join(timeout=3)
        t2.join(timeout=3)

    assert call_order.index("second_start") > call_order.index("first_end"), (
        f"second call started before first finished: {call_order}"
    )
    assert CHAT_ID not in _chat_in_flight


def test_all_invocations_use_chat_in_flight():
    """All invoke_safe calls now use per-chat serialization (HITL pattern)."""
    with patch("src.api.telegram.dispatcher._graph", _mock_graph_no_interrupt()):
        dispatcher.invoke_safe(55, "yes, book them")

    assert 55 not in _chat_in_flight  # cleared after completion


# ---------------------------------------------------------------------------
# resolve_trigger
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected_trigger", [
    ("/done",     "done"),
    ("/study",    "study"),
    ("/plan",     "daily"),
    ("/pick",     "pick"),
    ("/activate", "activate"),
    ("/discuss",  "discuss"),
])
def test_resolve_trigger_known_commands(command: str, expected_trigger: str):
    """Each registered command maps to its graph trigger name."""
    assert dispatcher.resolve_trigger(command) == expected_trigger


def test_resolve_trigger_unknown_payload_returned_as_is():
    """An unrecognised payload (e.g. a button callback) is returned unchanged."""
    assert dispatcher.resolve_trigger("category:DSA") == "category:DSA"


def test_resolve_trigger_is_case_insensitive():
    """Command matching is case-insensitive (Telegram delivers commands lowercase,
    but defensive handling keeps the mapping robust)."""
    assert dispatcher.resolve_trigger("/DONE") == "done"
    assert dispatcher.resolve_trigger("/Discuss") == "discuss"



# ---------------------------------------------------------------------------
# safe_chat_invoke — service-layer safe invocation entry point
# ---------------------------------------------------------------------------

def _mock_graph_with_interrupt():
    """Return a mock graph where get_state reports a pending interrupt."""
    from unittest.mock import MagicMock
    mock_graph = MagicMock()
    mock_snapshot = MagicMock()
    # Simulate a paused task with at least one pending interrupt.
    mock_task = MagicMock()
    mock_task.interrupts = [MagicMock()]
    mock_snapshot.tasks = [mock_task]
    mock_snapshot.next = ("some_node",)
    mock_graph.graph.get_state.return_value = mock_snapshot
    mock_graph.graph.invoke.return_value = None
    return mock_graph


def test_safe_chat_invoke_runs_when_no_pending_interrupt():
    """When the chat has no pending interrupt, the graph is invoked with the
    supplied state and True is returned."""
    mock_graph = _mock_graph_no_interrupt()
    fresh_state = {"trigger": "discuss_ready_confirm", "current_topic_id": 7}

    with patch("src.api.telegram.dispatcher._graph", mock_graph):
        invoked = dispatcher.safe_chat_invoke(42, fresh_state)

    assert invoked is True
    mock_graph.graph.invoke.assert_called_once()
    invoke_args, invoke_kwargs = mock_graph.graph.invoke.call_args
    # Same dict object is forwarded; chat_id auto-filled.
    assert invoke_args[0]["trigger"] == "discuss_ready_confirm"
    assert invoke_args[0]["chat_id"] == 42
    assert invoke_kwargs["config"]["configurable"]["thread_id"] == "42"
    assert 42 not in _chat_in_flight


def test_safe_chat_invoke_skips_when_pending_interrupt():
    """When the chat already has a pending interrupt, the graph is NOT invoked
    and False is returned — preventing checkpoint corruption of the paused flow."""
    mock_graph = _mock_graph_with_interrupt()

    with patch("src.api.telegram.dispatcher._graph", mock_graph):
        invoked = dispatcher.safe_chat_invoke(42, {"trigger": "discuss_ready_confirm"})

    assert invoked is False
    mock_graph.graph.invoke.assert_not_called()
    assert 42 not in _chat_in_flight  # lock still released


def test_safe_chat_invoke_releases_lock_on_exception():
    """Per-chat lock is released even when graph.invoke raises."""
    mock_graph = _mock_graph_no_interrupt()
    mock_graph.graph.invoke.side_effect = RuntimeError("boom")

    with patch("src.api.telegram.dispatcher._graph", mock_graph):
        with pytest.raises(RuntimeError):
            dispatcher.safe_chat_invoke(42, {"trigger": "x"})

    assert 42 not in _chat_in_flight


def test_safe_chat_invoke_serializes_with_invoke_safe():
    """safe_chat_invoke must wait for an in-flight invoke_safe on the same chat
    to finish before running — they share the per-chat lock."""
    CHAT_ID = 77
    call_order: list[str] = []

    def graph_invoke(state_or_cmd, config=None):
        # The first caller (invoke_safe) holds the lock during this sleep.
        if call_order == []:
            call_order.append("invoke_safe_start")
            time.sleep(0.15)
            call_order.append("invoke_safe_end")
        else:
            call_order.append("safe_chat_invoke_start")
            call_order.append("safe_chat_invoke_end")

    mock_graph = _mock_graph_no_interrupt()
    mock_graph.graph.invoke.side_effect = graph_invoke

    with patch("src.api.telegram.dispatcher._graph", mock_graph):
        t1 = threading.Thread(target=lambda: dispatcher.invoke_safe(CHAT_ID, "/done"))
        t2 = threading.Thread(
            target=lambda: dispatcher.safe_chat_invoke(CHAT_ID, {"trigger": "discuss_ready_confirm"})
        )
        t1.start()
        time.sleep(0.02)  # ensure t1 acquires the lock first
        t2.start()
        t1.join(timeout=3)
        t2.join(timeout=3)

    assert call_order.index("safe_chat_invoke_start") > call_order.index("invoke_safe_end"), (
        f"safe_chat_invoke ran before invoke_safe finished: {call_order}"
    )
    assert CHAT_ID not in _chat_in_flight
