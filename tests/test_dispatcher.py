"""
Unit tests for src/api/telegram/dispatcher.py

Pure unit tests — no external dependencies. Tests state mutation of
module-level dedup/idempotency sets.
"""

import pytest

from src.api.telegram import dispatcher
from src.api.telegram.dispatcher import (
    _confirmed_message_ids,
    _in_flight_message_ids,
    _processed_updates,
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
    yield
    _processed_updates.clear()
    _confirmed_message_ids.clear()
    _in_flight_message_ids.clear()


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
