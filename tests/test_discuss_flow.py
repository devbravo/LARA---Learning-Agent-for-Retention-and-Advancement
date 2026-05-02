"""Unit/integration tests for the /discuss LangGraph flow.

Covers discuss_parser and start_discuss nodes end-to-end:

  discuss_parser (direct node calls):
    1. Zero eligible topics → message returned, no picker sent
    2. Single topic → status changed to 'discussing', message returned
    3. Single topic → message contains topic name
    4. Single topic → no inline picker sent
    5. Single topic → 'Session #1' when no prior discuss sessions
    6. Single topic → session number increments with prior sessions
    7. Single topic with weak areas → focus areas appear in message
    8. Single topic with no weak areas → 'Focus areas' line omitted
    9. Multiple topics → inline picker sent, messages list is empty
   10. Multiple topics → picker buttons include all topic names
   11. Stale pending_message_id removed on entry

  start_discuss (full HITL via graph):
   12. Valid resume → topic status set to 'discussing' in DB
   13. Valid resume → confirmation message includes topic name
   14. Buttons removed after valid resume
   15. Command payload → no picker re-sent (dangling buttons); restart message sent
   16. Command payload → message contains /discuss restart instruction
   17. Empty payload → treated same as command (no re-send)
   18. Invalid resume payload (wrong prefix) → warning message
   19. Non-integer topic id in payload → warning message
   20. Unknown topic id → warning message
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Pop the conftest graph stub so we can import the real build_graph, then
# restore it so other tests that rely on the stub are not affected.
# ---------------------------------------------------------------------------
_graph_stub = sys.modules.pop("src.agent.graph", None)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command                  # noqa: E402

import src.agent.nodes as _nodes                     # noqa: E402
from src.agent.graph import build_graph              # noqa: E402
from src.agent.nodes import discuss_parser           # noqa: E402
from src.infrastructure import db as core_db         # noqa: E402

if _graph_stub is not None:
    sys.modules["src.agent.graph"] = _graph_stub


# ---------------------------------------------------------------------------
# DB schema + helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    tier INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'inactive',
    topic_type TEXT NOT NULL DEFAULT 'conceptual',
    easiness_factor REAL DEFAULT 2.5,
    interval_days INTEGER DEFAULT 1,
    repetitions INTEGER DEFAULT 0,
    next_review DATE,
    weak_areas TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    studied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    duration_min INTEGER,
    mode TEXT,
    quality_score INTEGER,
    weak_areas TEXT,
    student_quality INTEGER,
    student_weak_areas TEXT,
    teacher_quality INTEGER,
    teacher_weak_areas TEXT,
    teacher_source TEXT,
    calibration_gap INTEGER
);
"""


def _make_discuss_db(
    topics: list[dict],
    discuss_sessions: list[int] | None = None,
) -> str:
    """Create a temp SQLite DB seeded with the given topics and optional sessions.

    Args:
        topics: Dicts with keys ``name``, ``tier``, ``status``, and optionally
            ``topic_type``, ``weak_areas``.
        discuss_sessions: List of topic_ids for which to insert a prior
            ``mode='discuss'`` session row.

    Returns:
        Path to the temp DB file.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    for t in topics:
        conn.execute(
            """INSERT INTO topics (name, tier, status, topic_type, weak_areas)
               VALUES (:name, :tier, :status, :topic_type, :weak_areas)""",
            {
                "name": t["name"],
                "tier": t.get("tier", 1),
                "status": t.get("status", "in_progress"),
                "topic_type": t.get("topic_type", "conceptual"),
                "weak_areas": t.get("weak_areas"),
            },
        )
    for topic_id in (discuss_sessions or []):
        conn.execute(
            "INSERT INTO sessions (topic_id, mode, studied_at) VALUES (?, 'discuss', datetime('now'))",
            (topic_id,),
        )
    conn.commit()
    conn.close()
    return path


def _get_topic_id(path: str, name: str) -> int:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["id"]


def _get_topic_status(path: str, name: str) -> str:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM topics WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["status"]


def _make_test_graph():
    return build_graph(checkpointer=MemorySaver())


def _patch_db(path: str):
    """Context manager shorthand: swap core_db.DB_PATH, restore on exit."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        orig = core_db.DB_PATH
        core_db.DB_PATH = Path(path)
        try:
            yield
        finally:
            core_db.DB_PATH = orig

    return _ctx()


# ---------------------------------------------------------------------------
# 1. Zero topics → message, no picker
# ---------------------------------------------------------------------------

def test_discuss_parser_no_topics_returns_message():
    path = _make_discuss_db([])
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons") as mock_btn:
        result = discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    assert result.get("messages")
    assert "No topics" in result["messages"][0]
    mock_btn.assert_not_called()


# ---------------------------------------------------------------------------
# 2–4. Single topic — status set, message content, no picker
# ---------------------------------------------------------------------------

def test_discuss_parser_single_topic_sets_discussing():
    path = _make_discuss_db([{"name": "Gen AI - RAG", "status": "in_progress"}])
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons"):
        discuss_parser({"chat_id": 1, "pending_message_id": None})
    status = _get_topic_status(path, "Gen AI - RAG")
    os.remove(path)

    assert status == "discussing"


def test_discuss_parser_single_topic_message_contains_topic_name():
    path = _make_discuss_db([{"name": "Gen AI - RAG", "status": "in_progress"}])
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons"):
        result = discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    assert result.get("messages")
    assert "Gen AI - RAG" in result["messages"][0]


def test_discuss_parser_single_topic_no_picker_sent():
    path = _make_discuss_db([{"name": "Gen AI - RAG", "status": "in_progress"}])
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons") as mock_btn:
        discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    mock_btn.assert_not_called()


# ---------------------------------------------------------------------------
# 5–6. Session numbering
# ---------------------------------------------------------------------------

def test_discuss_parser_first_session_shows_session_1():
    """No prior discuss sessions → message contains 'Session #1'."""
    path = _make_discuss_db([{"name": "Topic A", "status": "in_progress"}])
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons"):
        result = discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    assert "Session #1" in result["messages"][0]


def test_discuss_parser_session_number_increments_with_prior_sessions():
    """2 prior discuss sessions → message contains 'Session #3'."""
    path = _make_discuss_db([{"name": "Topic A", "status": "in_progress"}])
    topic_id = _get_topic_id(path, "Topic A")

    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sessions (topic_id, mode, studied_at) VALUES (?, 'discuss', datetime('now'))", (topic_id,))
    conn.execute("INSERT INTO sessions (topic_id, mode, studied_at) VALUES (?, 'discuss', datetime('now'))", (topic_id,))
    conn.commit()
    conn.close()

    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons"):
        result = discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    assert "Session #3" in result["messages"][0]


# ---------------------------------------------------------------------------
# 7–8. Weak areas
# ---------------------------------------------------------------------------

def test_discuss_parser_weak_areas_appear_in_message():
    """Weak area VALUES (not structural key names) appear in the session-ready message.

    topics.weak_areas stores content in the values, not the keys.  For example,
    a conceptual topic writes {"unclear": "CAP theorem vs PACELC"} — the focus
    area to surface is "CAP theorem vs PACELC", not the structural label "unclear".
    A DSA topic writes {"breakdown": "Edge case, Time complexity"} — the focus
    area is "Edge case, Time complexity", not "breakdown".
    """
    weak_json = json.dumps({
        "unclear": "CAP theorem vs PACELC",
        "breakdown": "Edge case, Time complexity",
    })
    path = _make_discuss_db([{
        "name": "Topic A",
        "status": "in_progress",
        "weak_areas": weak_json,
    }])
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons"):
        result = discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    msg = result["messages"][0]
    # Values are shown — the structural key names must NOT appear
    assert "CAP theorem vs PACELC" in msg
    assert "Edge case, Time complexity" in msg
    assert "unclear" not in msg
    assert "breakdown" not in msg


def test_discuss_parser_no_weak_areas_omits_focus_line():
    """'Focus areas' line is absent when the topic has no recorded weak areas."""
    path = _make_discuss_db([{"name": "Topic A", "status": "in_progress"}])
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons"):
        result = discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    assert "Focus areas" not in result["messages"][0]


# ---------------------------------------------------------------------------
# 9–10. Multiple topics → picker sent
# ---------------------------------------------------------------------------

def test_discuss_parser_multiple_topics_sends_picker():
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])
    mock_btn = MagicMock(return_value=77)
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons", mock_btn):
        result = discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    mock_btn.assert_called_once()
    assert result.get("messages") == []
    assert result.get("pending_message_id") == 77


def test_discuss_parser_multiple_topics_picker_contains_all_names():
    """Every eligible topic appears as a button label in the picker."""
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])
    mock_btn = MagicMock(return_value=77)
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_inline_buttons", mock_btn):
        discuss_parser({"chat_id": 1, "pending_message_id": None})
    os.remove(path)

    # call_args[0][1] is the list of (label, callback_data) tuples
    labels = [btn[0] for btn in mock_btn.call_args[0][1]]
    assert "Topic A" in labels
    assert "Topic B" in labels


# ---------------------------------------------------------------------------
# 11. Stale pending_message_id cleaned up on entry
# ---------------------------------------------------------------------------

def test_discuss_parser_removes_stale_pending_message():
    """discuss_parser calls remove_buttons with the stale pending_message_id."""
    path = _make_discuss_db([{"name": "Topic A", "status": "in_progress"}])
    mock_remove = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes._telegram, "remove_buttons", mock_remove), \
         patch.object(_nodes._telegram, "send_inline_buttons"):
        discuss_parser({"chat_id": 42, "pending_message_id": 99})
    os.remove(path)

    mock_remove.assert_called_once_with(42, 99)


# ---------------------------------------------------------------------------
# 12. Full HITL: valid resume → status set to 'discussing'
# ---------------------------------------------------------------------------

def test_start_discuss_valid_resume_sets_discussing():
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])
    topic_a_id = _get_topic_id(path, "Topic A")

    g = _make_test_graph()
    chat_id = 7001
    config = {"configurable": {"thread_id": str(chat_id)}}

    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=55), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"discuss_topic:{topic_a_id}"), config=config)

    assert _get_topic_status(path, "Topic A") == "discussing"
    os.remove(path)


# ---------------------------------------------------------------------------
# 13. Full HITL: confirmation message includes topic name
# ---------------------------------------------------------------------------

def test_start_discuss_valid_resume_message_mentions_topic():
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])
    topic_a_id = _get_topic_id(path, "Topic A")

    g = _make_test_graph()
    chat_id = 7002
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=55), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"discuss_topic:{topic_a_id}"), config=config)
    os.remove(path)

    mock_send.assert_called_once()
    assert "Topic A" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# 14. Full HITL: buttons removed after valid resume
# ---------------------------------------------------------------------------

def test_start_discuss_buttons_removed_after_resume():
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])
    topic_a_id = _get_topic_id(path, "Topic A")

    g = _make_test_graph()
    chat_id = 7003
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_remove = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=55), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons", mock_remove):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=f"discuss_topic:{topic_a_id}"), config=config)
    os.remove(path)

    mock_remove.assert_called_once_with(chat_id, 55)


# ---------------------------------------------------------------------------
# 15a. Full HITL: command payload → picker re-sent with fallback message
# ---------------------------------------------------------------------------

def test_start_discuss_command_payload_does_not_resend_picker():
    """Typing a command instead of tapping a button must NOT re-send the picker.

    Re-sending buttons here would leave them dangling: the interrupt is already
    consumed so ``has_pending_interrupt`` is False and any button tap would be
    treated as a fresh no-op trigger.  The correct behaviour is to end the flow
    and tell the user to restart with /discuss.
    """
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])

    g = _make_test_graph()
    chat_id = 7010
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_btn = MagicMock(return_value=55)  # only the initial picker should fire
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", mock_btn), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="/pick"), config=config)
    os.remove(path)

    # Only the initial picker from discuss_parser — no second send.
    assert mock_btn.call_count == 1


def test_start_discuss_command_payload_sends_restart_message():
    """Fallback message after a command payload tells the user to restart with /discuss."""
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])

    g = _make_test_graph()
    chat_id = 7011
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=55), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="/pick"), config=config)
    os.remove(path)

    mock_send.assert_called_once()
    assert "/discuss" in mock_send.call_args[0][0]


def test_start_discuss_empty_payload_does_not_resend_picker():
    """An empty resume string is treated the same as a command payload: no re-send."""
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])

    g = _make_test_graph()
    chat_id = 7012
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_btn = MagicMock(return_value=55)
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", mock_btn), \
         patch.object(_nodes._telegram, "send_message"), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume=""), config=config)
    os.remove(path)

    assert mock_btn.call_count == 1


# ---------------------------------------------------------------------------
# 15b. Full HITL: invalid resume payload → warning
# ---------------------------------------------------------------------------

def test_start_discuss_invalid_payload_prefix_sends_warning():
    """Payload with the wrong prefix (e.g. 'studied:') triggers a warning."""
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])

    g = _make_test_graph()
    chat_id = 7004
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=55), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="studied:99"), config=config)
    os.remove(path)

    mock_send.assert_called_once()
    assert "⚠️" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# 16. Full HITL: non-integer id → warning
# ---------------------------------------------------------------------------

def test_start_discuss_non_integer_id_sends_warning():
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])

    g = _make_test_graph()
    chat_id = 7005
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=55), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="discuss_topic:abc"), config=config)
    os.remove(path)

    mock_send.assert_called_once()
    assert "⚠️" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# 17. Full HITL: unknown topic id → warning
# ---------------------------------------------------------------------------

def test_start_discuss_unknown_topic_id_sends_warning():
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
        {"name": "Topic B", "status": "active"},
    ])

    g = _make_test_graph()
    chat_id = 7006
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=55), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        g.invoke({"trigger": "discuss", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="discuss_topic:9999"), config=config)
    os.remove(path)

    mock_send.assert_called_once()
    assert "⚠️" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# 21. await_discuss_activation: rowcount=0 surfaces a warning, not success
# ---------------------------------------------------------------------------

def test_await_discuss_activation_rowcount_zero_returns_warning():
    """When activate_topic_from_discuss returns False (no row updated, e.g.
    topic deleted between the readiness check and the user's tap), the user
    must see a warning — not a misleading success message.
    """
    path = _make_discuss_db([
        {"name": "Topic A", "status": "in_progress"},
    ])
    topic_id = _get_topic_id(path, "Topic A")

    g = _make_test_graph()
    chat_id = 7100
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with _patch_db(path), \
         patch.object(_nodes.topic_repository, "activate_topic_from_discuss",
                      return_value=False), \
         patch.object(_nodes._telegram, "send_buttons", return_value=88), \
         patch.object(_nodes._telegram, "send_message", mock_send), \
         patch.object(_nodes._telegram, "remove_buttons"):
        # Enter the readiness-confirm flow as the discuss service would
        g.invoke(
            {
                "trigger": "discuss_ready_confirm",
                "chat_id": chat_id,
                "current_topic_id": topic_id,
                "current_topic_name": "Topic A",
                "pending_message_id": None,
                "messages": [],
            },
            config=config,
        )
        g.invoke(Command(resume="Yes, activate"), config=config)
    os.remove(path)

    mock_send.assert_called_once()
    msg = mock_send.call_args[0][0]
    assert "⚠️" in msg
    assert "could not" in msg.lower() or "no longer" in msg.lower()
