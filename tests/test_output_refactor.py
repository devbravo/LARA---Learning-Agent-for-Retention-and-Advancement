"""Tests for the refactored output and book_events nodes.

Covers:
  1.  output sends the last message for a daily trigger
  2.  output sends the last message for a weekend trigger
  3.  output does nothing when messages list is empty
  4.  book_events writes GCal events for all slots in proposed_slots
  5.  book_events falls back to proposed_topic + proposed_slot when no proposed_slots
  6.  book_events sends confirmation message after successful booking
  7.  book_events removes keyboard buttons when message_id is present
  8.  book_events continues booking remaining slots when one slot fails
  9.  log_session → END edge exists in graph.py (not log_session → output)
  10. log_weak_areas → END edge exists in graph.py (not log_weak_areas → output)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


from src.agent import nodes as _nodes
from src.agent.nodes import book_events, output, route_from_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG = {"timezone": "UTC"}


# ---------------------------------------------------------------------------
# 1. output sends the last message for a daily trigger
# ---------------------------------------------------------------------------

def test_output_sends_last_message_for_daily():
    """output sends the final message when trigger is 'daily'."""
    state = {"trigger": "daily", "messages": ["☀️ Good morning Diego — Sunday April 19"]}
    mock_send = MagicMock()
    with patch.object(_nodes._telegram, "send_message", mock_send):
        result = output(state)
    mock_send.assert_called_once_with("☀️ Good morning Diego — Sunday April 19")
    assert result == {}


# ---------------------------------------------------------------------------
# 2. output sends the last message for a weekend trigger
# ---------------------------------------------------------------------------

def test_output_sends_last_message_for_weekend():
    """output sends the final message when trigger is 'weekend'."""
    state = {"trigger": "weekend", "messages": ["Weekend brief here."]}
    mock_send = MagicMock()
    with patch.object(_nodes._telegram, "send_message", mock_send):
        result = output(state)
    mock_send.assert_called_once_with("Weekend brief here.")
    assert result == {}


# ---------------------------------------------------------------------------
# 3. output does nothing when messages list is empty
# ---------------------------------------------------------------------------

def test_output_does_nothing_when_no_messages():
    """output does not call send_message when the messages list is empty."""
    state = {"trigger": "daily", "messages": []}
    mock_send = MagicMock()
    with patch.object(_nodes._telegram, "send_message", mock_send):
        result = output(state)
    mock_send.assert_not_called()
    assert result == {}


# ---------------------------------------------------------------------------
# 4. book_events writes GCal events for all slots in proposed_slots
# ---------------------------------------------------------------------------

def test_book_events_writes_gcal_for_all_proposed_slots():
    """book_events calls write_event once per slot when proposed_slots is set."""
    state = {
        "chat_id": 123,
        "message_id": None,
        "proposed_slots": [
            {"topic": "DSA - Arrays", "start": "09:00", "end": "10:00", "duration_min": 60},
            {"topic": "LangGraph", "start": "11:00", "end": "12:00", "duration_min": 60},
        ],
        "proposed_topic": None,
        "proposed_slot": None,
    }
    mock_write = MagicMock()
    with patch("src.agent.nodes._load_config", return_value=_CONFIG), \
         patch.object(_nodes._gcal, "write_event", mock_write), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons"):
        result = book_events(state)
    assert mock_write.call_count == 2
    topics_written = [c.kwargs["topic"] for c in mock_write.call_args_list]
    assert "DSA - Arrays" in topics_written
    assert "LangGraph" in topics_written
    assert result == {}


# ---------------------------------------------------------------------------
# 5. book_events falls back to proposed_topic + proposed_slot
# ---------------------------------------------------------------------------

def test_book_events_falls_back_to_single_slot():
    """When proposed_slots is absent, book_events uses proposed_topic/slot."""
    state = {
        "chat_id": 123,
        "message_id": None,
        "proposed_slots": None,
        "proposed_topic": "Gen AI System Design",
        "proposed_slot": {"start": "14:00", "end": "15:00", "duration_min": 60},
    }
    mock_write = MagicMock()
    with patch("src.agent.nodes._load_config", return_value=_CONFIG), \
         patch.object(_nodes._gcal, "write_event", mock_write), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons"):
        result = book_events(state)
    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    assert kwargs["topic"] == "Gen AI System Design"
    assert result == {}


# ---------------------------------------------------------------------------
# 6. book_events sends confirmation message after successful booking
# ---------------------------------------------------------------------------

def test_book_events_sends_confirmation_after_booking():
    """book_events sends a ✅ message listing all successfully booked sessions."""
    state = {
        "chat_id": 123,
        "message_id": None,
        "proposed_slots": [
            {"topic": "DSA - Arrays", "start": "09:00", "end": "10:00", "duration_min": 60},
        ],
        "proposed_topic": None,
        "proposed_slot": None,
    }
    mock_send = MagicMock()
    with patch("src.agent.nodes._load_config", return_value=_CONFIG), \
         patch.object(_nodes._gcal, "write_event"), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        book_events(state)
    mock_send.assert_called_once()
    message = mock_send.call_args[0][0]
    assert "✅ Booked 1 mock session(s)" in message
    assert "DSA - Arrays" in message


# ---------------------------------------------------------------------------
# 7. book_events does not remove buttons (done inside interrupt nodes now)
# ---------------------------------------------------------------------------

def test_book_events_does_not_call_remove_buttons():
    """book_events no longer removes buttons — that happens inside the HITL interrupt node."""
    state = {
        "chat_id": 999,
        "message_id": 42,
        "proposed_slots": [
            {"topic": "LangGraph", "start": "09:00", "end": "10:00", "duration_min": 60},
        ],
        "proposed_topic": None,
        "proposed_slot": None,
    }
    mock_remove = MagicMock()
    with patch("src.agent.nodes._load_config", return_value=_CONFIG), \
         patch.object(_nodes._gcal, "write_event"), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons", mock_remove):
        book_events(state)
    mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 8. book_events continues booking when one slot fails
# ---------------------------------------------------------------------------

def test_book_events_continues_when_one_slot_fails():
    """A GCal write failure for one slot does not prevent booking subsequent slots."""
    state = {
        "chat_id": 123,
        "message_id": None,
        "proposed_slots": [
            {"topic": "Topic A", "start": "09:00", "end": "10:00", "duration_min": 60},
            {"topic": "Topic B", "start": "11:00", "end": "12:00", "duration_min": 60},
        ],
        "proposed_topic": None,
        "proposed_slot": None,
    }
    mock_write = MagicMock(side_effect=[Exception("GCal unavailable"), None])
    mock_send = MagicMock()
    with patch("src.agent.nodes._load_config", return_value=_CONFIG), \
         patch.object(_nodes._gcal, "write_event", mock_write), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        book_events(state)
    # Both slots were attempted despite the first failure
    assert mock_write.call_count == 2
    # Only Topic B was successfully booked → confirmation contains Topic B only
    mock_send.assert_called_once()
    message = mock_send.call_args[0][0]
    assert "Topic B" in message
    assert "Topic A" not in message


# ---------------------------------------------------------------------------
# 9. log_session → log_weak_areas edge in graph.py (HITL refactor)
# ---------------------------------------------------------------------------

def test_log_session_edge_goes_to_log_weak_areas():
    """graph.py wires log_session → log_weak_areas (HITL pattern)."""
    src = (Path(__file__).parents[1] / "src" / "agent" / "graph.py").read_text()
    assert 'add_edge("log_session", "log_weak_areas")' in src
    assert 'add_edge("log_session", END)' not in src


# ---------------------------------------------------------------------------
# 10. log_weak_areas → conditional edge (log_session | output) in graph.py
# ---------------------------------------------------------------------------

def test_log_weak_areas_has_conditional_edge():
    """graph.py wires log_weak_areas with a conditional edge (HITL loop pattern)."""
    src = (Path(__file__).parents[1] / "src" / "agent" / "graph.py").read_text()
    assert 'route_from_log_weak_areas' in src
    assert 'add_edge("log_weak_areas", END)' not in src


# ---------------------------------------------------------------------------
# 11. route_from_router maps "skip" to "output" (via default fallback)
# ---------------------------------------------------------------------------

def test_route_from_router_maps_skip_to_output():
    """route_from_router returns 'output' for trigger='skip'."""
    assert route_from_router({"trigger": "skip"}) == "output"

