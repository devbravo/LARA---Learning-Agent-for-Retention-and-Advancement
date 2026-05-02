"""
Unit tests for the graduate_topic and activate_topic nodes.

Covers:
  graduate_topic:
    1. Invalid payload (not starting with 'studied:')
    2. Empty or missing payload
    3. Non-integer id
    4. Topic not found in DB (topic_service raises ValueError)
    5. Success path: status updated, confirmation message returned
    6. next_review set to a future date after graduation

  activate_topic:
    7. No in-progress topics → early message, no buttons sent
    8. Valid resume payload graduates the topic
    9. Invalid resume payload (wrong prefix) → warning message
    10. Buttons removed after valid resume
    11. Buttons removed even after invalid resume
"""

import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Temporarily remove the conftest stub so we can import the real build_graph,
# then restore it so other tests that rely on the stub are unaffected.
# ---------------------------------------------------------------------------
_graph_stub = sys.modules.pop("src.agent.graph", None)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

import src.agent.nodes as _nodes  # noqa: E402
from src.agent.graph import build_graph  # noqa: E402
from src.agent.nodes import activate_topic, graduate_topic  # noqa: E402

if _graph_stub is not None:
    sys.modules["src.agent.graph"] = _graph_stub


# ---------------------------------------------------------------------------
# DB helpers (mirrors test_study_topic.py pattern)
# ---------------------------------------------------------------------------

def _make_topics_db(topics: list[dict]) -> str:
    """Create a temp SQLite DB seeded with the given topics. Returns db_path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            tier INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'inactive',
            easiness_factor REAL DEFAULT 2.5,
            interval_days INTEGER DEFAULT 1,
            repetitions INTEGER DEFAULT 0,
            next_review DATE,
            weak_areas TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.executemany(
        "INSERT INTO topics (name, tier, status) VALUES (?, ?, ?)",
        [(t["name"], t["tier"], t["status"]) for t in topics],
    )
    conn.commit()
    conn.close()
    return path


def _make_get_connection(db_path: str):
    def _get_connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    return _get_connection


def _get_topic_id(db_path: str, name: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["id"]


def _get_topic_status(db_path: str, name: str) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM topics WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["status"]


def _make_test_graph():
    """Build an isolated graph backed by an in-memory SQLite checkpointer."""
    # Use an in-memory saver for tests
    return build_graph(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# 1. Invalid payload — does not start with 'studied:'
# ---------------------------------------------------------------------------

def test_graduate_topic_invalid_payload_returns_warning():
    """Payload not starting with 'studied:' returns an error message."""
    result = graduate_topic({"payload": "category:DSA"})
    assert result.get("messages")
    assert "⚠️" in result["messages"][0]


def test_graduate_topic_plain_text_payload_returns_warning():
    """A plain text payload that doesn't match 'studied:' returns an error message."""
    result = graduate_topic({"payload": "some random text"})
    assert result.get("messages")
    assert "⚠️" in result["messages"][0]


# ---------------------------------------------------------------------------
# 2. Empty payload treated as invalid
# ---------------------------------------------------------------------------

def test_graduate_topic_empty_payload_returns_warning():
    """An empty payload returns an error message, not a crash."""
    result = graduate_topic({"payload": ""})
    assert result.get("messages")
    assert "⚠️" in result["messages"][0]


def test_graduate_topic_missing_payload_returns_warning():
    """Missing payload key returns an error message, not a crash."""
    result = graduate_topic({})
    assert result.get("messages")
    assert "⚠️" in result["messages"][0]


# ---------------------------------------------------------------------------
# 3. Non-integer id after 'studied:'
# ---------------------------------------------------------------------------

def test_graduate_topic_non_integer_id_returns_warning():
    """A non-integer topic id after 'studied:' returns an error message."""
    result = graduate_topic({"payload": "studied:abc"})
    assert result.get("messages")
    assert "⚠️" in result["messages"][0]


def test_graduate_topic_empty_id_returns_warning():
    """'studied:' with no id returns an error message."""
    result = graduate_topic({"payload": "studied:"})
    assert result.get("messages")
    assert "⚠️" in result["messages"][0]


# ---------------------------------------------------------------------------
# 4. Topic not found in DB
# ---------------------------------------------------------------------------

def test_graduate_topic_topic_not_found_returns_warning():
    """When topic_service.graduate_topic raises ValueError, a warning message is returned."""
    from src.agent import nodes as nodes_module

    with patch.object(nodes_module.topic_service, "graduate_topic", side_effect=ValueError("not found")):
        result = graduate_topic({"payload": "studied:999"})

    assert result.get("messages")
    assert "⚠️" in result["messages"][0]


# ---------------------------------------------------------------------------
# 5. Success path — full HITL graph (trigger=activate → resume=studied:<id>)
# ---------------------------------------------------------------------------

def test_graduate_topic_success_sets_active_status():
    """graduate_topic() promotes the topic to 'active' status in the DB.

    Prior discuss session (count=1) bypasses the soft guard so the normal
    activation path is exercised.
    """
    path = _make_topics_db([
        {"name": "DSA - Linked Lists", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "DSA - Linked Lists")

    g = _make_test_graph()
    chat_id = 5001
    config = {"configurable": {"thread_id": str(chat_id)}}

    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=1), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)

    assert _get_topic_status(path, "DSA - Linked Lists") == "active"


def test_graduate_topic_success_message_mentions_sm2():
    """Confirmation message tells the user first SM-2 review is scheduled.

    Prior discuss session (count=1) bypasses the soft guard so the normal
    activation path is exercised.
    """
    path = _make_topics_db([
        {"name": "Gen AI - RAG", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "Gen AI - RAG")

    g = _make_test_graph()
    chat_id = 5002
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=1), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)

    mock_send.assert_called()
    msg = mock_send.call_args[0][0]
    assert "SM-2" in msg or "tomorrow" in msg.lower()


def test_graduate_topic_success_sets_next_review_to_tomorrow():
    """After graduation, next_review is set to a future date.

    Prior discuss session (count=1) bypasses the soft guard so the normal
    activation path is exercised.
    """
    from datetime import date

    path = _make_topics_db([
        {"name": "Gen AI - RAG", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "Gen AI - RAG")

    g = _make_test_graph()
    chat_id = 5003
    config = {"configurable": {"thread_id": str(chat_id)}}

    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=1), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT next_review FROM topics WHERE id = ?", (topic_id,)).fetchone()
    conn.close()
    assert row["next_review"] is not None
    assert row["next_review"] > date.today().isoformat()


# ---------------------------------------------------------------------------
# 6. activate_topic — no in-progress topics
# ---------------------------------------------------------------------------

def test_activate_topic_no_in_progress_returns_message():
    """activate_topic returns a message when there are no in-progress topics."""
    from src.agent import nodes as nodes_module

    with patch.object(nodes_module.topic_service, "get_in_progress_topics", return_value=[]):
        result = activate_topic({"chat_id": 123})

    assert result.get("messages")
    assert "No topics" in result["messages"][0] or "in progress" in result["messages"][0].lower()


def test_activate_topic_no_in_progress_does_not_send_buttons():
    """No Telegram buttons are sent when there are no in-progress topics."""
    from src.agent import nodes as nodes_module

    mock_send = MagicMock()
    with patch.object(nodes_module.topic_service, "get_in_progress_topics", return_value=[]), \
         patch.object(nodes_module._telegram, "send_inline_buttons", mock_send):
        activate_topic({"chat_id": 123})

    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Full HITL: valid resume graduates the topic
# ---------------------------------------------------------------------------

def test_activate_topic_valid_payload_stored_in_state():
    """A 'studied:<id>' resume graduates the topic and sends a confirmation.

    Prior discuss session (count=1) bypasses the soft guard so the normal
    activation path is exercised.
    """
    path = _make_topics_db([
        {"name": "DSA - Linked Lists", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "DSA - Linked Lists")

    g = _make_test_graph()
    chat_id = 5004
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=1), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)

    assert _get_topic_status(path, "DSA - Linked Lists") == "active"
    mock_send.assert_called()
    assert "✅" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# 8. Full HITL: invalid resume payload returns warning
# ---------------------------------------------------------------------------

def test_activate_topic_invalid_resume_returns_warning():
    """A resume payload that doesn't start with 'studied:' returns a warning message."""
    topics = [{"id": 7, "name": "DSA - Linked Lists"}]

    g = _make_test_graph()
    chat_id = 5005
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch.object(_nodes.topic_service, "get_in_progress_topics", return_value=topics), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="category:DSA"), config=config)

    mock_send.assert_called_once()
    assert "⚠️" in mock_send.call_args[0][0]


def test_activate_topic_empty_resume_returns_warning():
    """An empty resume payload returns a warning message."""
    topics = [{"id": 7, "name": "DSA - Linked Lists"}]

    g = _make_test_graph()
    chat_id = 5006
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch.object(_nodes.topic_service, "get_in_progress_topics", return_value=topics), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=""), config=config)

    mock_send.assert_called_once()
    assert "⚠️" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# 9. Full HITL: buttons removed after resume (valid and invalid)
# ---------------------------------------------------------------------------

def test_activate_topic_buttons_removed_after_valid_resume():
    """remove_buttons is called with the correct chat_id and message_id after a valid resume."""
    path = _make_topics_db([
        {"name": "DSA - Linked Lists", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "DSA - Linked Lists")

    g = _make_test_graph()
    chat_id = 5007
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_remove = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons", mock_remove):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)

    mock_remove.assert_called_once_with(chat_id, 42)


def test_activate_topic_buttons_removed_after_invalid_resume():
    """remove_buttons is called even when the resume payload is invalid."""
    topics = [{"id": 7, "name": "DSA - Linked Lists"}]
    chat_id = 456

    g = _make_test_graph()
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_remove = MagicMock()
    with patch.object(_nodes.topic_service, "get_in_progress_topics", return_value=topics), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=99), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons", mock_remove):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="not_a_valid_payload"), config=config)

    mock_remove.assert_called_once_with(chat_id, 99)


# ---------------------------------------------------------------------------
# 10. Soft guard — in_progress topic with 0 discuss sessions
# ---------------------------------------------------------------------------

def test_activate_soft_guard_sends_warning_buttons():
    """Activating an in_progress topic with no discuss sessions sends a soft-guard
    warning (not a graduation) and leaves the topic status unchanged.

    This exercises the ``graduate_topic`` → ``confirm_graduate`` path where the
    topic has never been discussed before.
    """
    path = _make_topics_db([
        {"name": "DSA - Tries", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "DSA - Tries")

    g = _make_test_graph()
    chat_id = 5020
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send_buttons = MagicMock(return_value=77)
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=0), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_buttons", mock_send_buttons), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)

    # Soft-guard buttons must be sent with the expected choices.
    mock_send_buttons.assert_called_once()
    _, call_buttons = mock_send_buttons.call_args[0]
    assert "Yes, activate" in call_buttons
    assert "Do discuss first" in call_buttons

    # Topic is NOT yet activated — guard is paused waiting for the user.
    assert _get_topic_status(path, "DSA - Tries") == "in_progress"


def test_soft_guard_yes_activate_promotes_topic_to_active():
    """Tapping 'Yes, activate' after the soft-guard warning activates the topic
    and sends a graduation confirmation.

    Full 3-step HITL: /activate → topic selection → 'Yes, activate'.
    """
    path = _make_topics_db([
        {"name": "DSA - Heaps", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "DSA - Heaps")

    g = _make_test_graph()
    chat_id = 5021
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=0), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_buttons", return_value=77), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)
        g.invoke(Command(resume="Yes, activate"), config=config)

    assert _get_topic_status(path, "DSA - Heaps") == "active"
    mock_send.assert_called_once()
    assert "✅" in mock_send.call_args[0][0]


def test_soft_guard_do_discuss_first_sets_topic_to_discussing():
    """Tapping 'Do discuss first' sets the topic to 'discussing' and sends a
    discuss-session-ready notification without activating the topic.

    Full 3-step HITL: /activate → topic selection → 'Do discuss first'.
    """
    path = _make_topics_db([
        {"name": "DSA - Graphs", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "DSA - Graphs")

    g = _make_test_graph()
    chat_id = 5022
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=0), \
         patch.object(_nodes.topic_repository, "get_topic_context",
                      return_value={"topic_type": "dsa", "weak_areas": None}), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_buttons", return_value=77), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)
        g.invoke(Command(resume="Do discuss first"), config=config)

    assert _get_topic_status(path, "DSA - Graphs") == "discussing"
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][0]
    assert "discuss" in msg.lower()


# ---------------------------------------------------------------------------
# 11. Hard block — discussing topic cannot be activated
# ---------------------------------------------------------------------------

def test_soft_guard_unknown_payload_does_not_mutate_state():
    """A non-button resume value (command or free text) must NOT flip the topic
    to 'discussing' — the soft guard returns a warning and exits cleanly.

    This prevents silently changing topic status when the user types /done,
    /help, or arbitrary text while the soft-guard interrupt is pending.
    """
    path = _make_topics_db([
        {"name": "DSA - DP", "tier": 1, "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "DSA - DP")

    g = _make_test_graph()
    chat_id = 5024
    config = {"configurable": {"thread_id": str(chat_id)}}

    # set_topic_discussing must not be called on an unknown payload.
    mock_set_discussing = MagicMock()
    mock_send = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.session_repository, "get_discuss_session_count", return_value=0), \
         patch.object(_nodes.topic_repository, "set_topic_discussing", mock_set_discussing), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_buttons", return_value=77), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)
        g.invoke(Command(resume="/done"), config=config)

    # Topic status unchanged — still in_progress, not discussing or active.
    assert _get_topic_status(path, "DSA - DP") == "in_progress"
    # No state-mutating call to set_topic_discussing.
    mock_set_discussing.assert_not_called()
    # Warning message sent.
    mock_send.assert_called_once()
    assert "⚠️" in mock_send.call_args[0][0]


def test_activate_discussing_block_sends_hard_block_message():
    """Trying to activate a topic that is in 'discussing' status sends a hard-block
    message and leaves the topic status unchanged.

    The discussing topic is injected into the picker mock so the user can
    'select' it; the hard block fires inside ``graduate_topic``.
    """
    path = _make_topics_db([
        {"name": "DSA - Trees", "tier": 1, "status": "discussing"},
    ])
    topic_id = _get_topic_id(path, "DSA - Trees")

    g = _make_test_graph()
    chat_id = 5023
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes.topic_service, "get_in_progress_topics",
                      return_value=[{"id": topic_id, "name": "DSA - Trees"}]), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=42), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "activate", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"studied:{topic_id}"), config=config)

    mock_send.assert_called_once()
    msg = mock_send.call_args[0][0]
    assert "🚫" in msg or "discuss mode" in msg.lower()

    # Status unchanged — hard block does not modify the topic.
    assert _get_topic_status(path, "DSA - Trees") == "discussing"
