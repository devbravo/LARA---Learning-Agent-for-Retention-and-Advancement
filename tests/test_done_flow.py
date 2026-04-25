"""
Unit and integration tests for the /done flow.

Covers:
  done_parser:
    1. 0 unlogged active topics → "No active sessions" message
    2. Exactly 1 unlogged active topic → rating buttons sent, current_topic_id set
    3. 2+ unlogged active topics → picker buttons sent, current_topic_id is None
    4. Single topic — duration_min pulled from proposed_slots when match found
    5. Single topic — duration_min defaults to 0 when no matching slot

  route_from_done_parser:
    6. has_unlogged_sessions=False → "output"
    7. has_unlogged_sessions=True, current_topic_id set → "log_session"
    8. has_unlogged_sessions=True, current_topic_id=None → "select_done_topic"

  select_done_topic (full HITL via graph):
    9.  Resume with a valid topic name sets current_topic_id + current_topic_name
    10. Resume with a valid topic name sends rating buttons

  log_weak_areas ending:
    11. No remaining unlogged topics → "All done for today" message
    12. Remaining unlogged topics → lists names and asks user to /done again

  get_active_unlogged_topics_today (repository):
    13. Excludes already-logged topics
    14. Excludes non-active (in_progress / inactive) topics
    15. Returns empty list when all active topics are logged
"""

import os
import sqlite3
import sys
import tempfile
from datetime import date
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Remove the conftest stub for src.agent.graph so we can import the real graph.
# Restored after imports so other tests keep their stub.
# ---------------------------------------------------------------------------
_graph_stub = sys.modules.pop("src.agent.graph", None)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

import src.agent.nodes as _nodes  # noqa: E402
from src.agent.graph import build_graph  # noqa: E402
from src.agent.nodes import done_parser, log_weak_areas, route_from_done_parser  # noqa: E402
from src import infrastructure  # noqa: E402
from src.infrastructure import db as core_db  # noqa: E402

if _graph_stub is not None:
    sys.modules["src.agent.graph"] = _graph_stub


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _make_db(topics: list[dict], sessions: list[dict] | None = None) -> str:
    """Create a temp SQLite DB with topics and optional sessions. Returns db_path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            tier INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            easiness_factor REAL DEFAULT 2.5,
            interval_days INTEGER DEFAULT 1,
            repetitions INTEGER DEFAULT 0,
            next_review DATE,
            weak_areas TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL REFERENCES topics(id),
            studied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            duration_min INTEGER,
            quality_score INTEGER,
            weak_areas TEXT,
            suggestions TEXT
        );
    """)
    for t in topics:
        conn.execute(
            "INSERT INTO topics (name, tier, status, easiness_factor) VALUES (?, ?, ?, ?)",
            (t["name"], t["tier"], t.get("status", "active"), t.get("easiness_factor", 2.5)),
        )
    if sessions:
        today = date.today().isoformat()
        for s in sessions:
            topic_row = conn.execute("SELECT id FROM topics WHERE name = ?", (s["topic"],)).fetchone()
            if topic_row:
                conn.execute(
                    "INSERT INTO sessions (topic_id, duration_min, quality_score, studied_at) VALUES (?, ?, ?, ?)",
                    (topic_row["id"], s.get("duration_min", 30), s.get("quality_score", 3), today),
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


def _make_test_graph():
    # Use the in-memory saver for tests (no persistent DB required)
    return build_graph(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# 1. 0 unlogged active topics → "No active sessions" message
# ---------------------------------------------------------------------------

def test_done_parser_no_unlogged_topics():
    """done_parser returns an informative message when no active topics are unlogged."""
    with patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today", return_value=[]):
        result = done_parser({})
    assert result["has_unlogged_sessions"] is False
    assert "No active sessions" in result["messages"][0]


# ---------------------------------------------------------------------------
# 2. Exactly 1 unlogged active topic → rating buttons sent, current_topic_id set
# ---------------------------------------------------------------------------

def test_done_parser_one_unlogged_sends_rating_buttons():
    """done_parser skips picker and sends rating buttons directly for a single topic."""
    mock_send = MagicMock(return_value=99)
    with patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today",
                      return_value=[{"id": 7, "name": "DSA - Trees"}]), \
         patch.object(_nodes._telegram, "send_buttons", mock_send):
        result = done_parser({})

    assert result["has_unlogged_sessions"] is True
    assert result["current_topic_id"] == 7
    assert result["current_topic_name"] == "DSA - Trees"
    assert result["pending_message_id"] == 99
    mock_send.assert_called_once_with("How did DSA - Trees go?", ["😕 Hard", "😐 OK", "😊 Easy"])


# ---------------------------------------------------------------------------
# 3. 2+ unlogged active topics → picker buttons sent, current_topic_id is None
# ---------------------------------------------------------------------------

def test_done_parser_multiple_unlogged_sends_picker():
    """done_parser sends a one-per-row topic picker when 2+ planned topics are unlogged."""
    mock_send = MagicMock(return_value=55)
    topics = [{"id": 1, "name": "DSA - Trees"}, {"id": 2, "name": "System Design"}]
    state = {
        "proposed_slots": [
            {"topic": "DSA - Trees", "duration_min": 45},
            {"topic": "System Design", "duration_min": 60},
        ]
    }
    with patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today",
                      return_value=topics), \
         patch.object(_nodes._telegram, "send_inline_buttons", mock_send):
        result = done_parser(state)

    assert result["has_unlogged_sessions"] is True
    assert result["current_topic_id"] is None
    assert result["pending_message_id"] == 55
    mock_send.assert_called_once_with(
        "Which topic did you just finish?",
        [("DSA - Trees", "DSA - Trees"), ("System Design", "System Design")],
    )


# ---------------------------------------------------------------------------
# 4. Single topic — duration_min pulled from proposed_slots
# ---------------------------------------------------------------------------

def test_done_parser_single_topic_reads_duration_from_proposed_slots():
    """done_parser sets duration_min from proposed_slots when topic name matches."""
    state = {
        "proposed_slots": [
            {"topic": "DSA - Trees", "duration_min": 45},
            {"topic": "Other", "duration_min": 30},
        ]
    }
    with patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today",
                      return_value=[{"id": 7, "name": "DSA - Trees"}]), \
         patch.object(_nodes._telegram, "send_buttons", return_value=1):
        result = done_parser(state)

    assert result["duration_min"] == 45


# ---------------------------------------------------------------------------
# 5. Single topic — duration_min defaults to 0 when no matching slot
# ---------------------------------------------------------------------------

def test_done_parser_single_topic_defaults_duration_when_no_slot():
    """done_parser sets duration_min=0 when proposed_slots has no matching topic."""
    with patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today",
                      return_value=[{"id": 7, "name": "DSA - Trees"}]), \
         patch.object(_nodes._telegram, "send_buttons", return_value=1):
        result = done_parser({"proposed_slots": []})

    assert result["duration_min"] == 0


# ---------------------------------------------------------------------------
# New bug regression: picker must not show topics outside today's plan
# ---------------------------------------------------------------------------

def test_done_parser_ignores_active_topics_not_in_plan():
    """done_parser only shows topics from proposed_slots, not all active DB topics."""
    mock_send = MagicMock(return_value=1)
    # DB has many active topics; only one was planned today
    all_active = [
        {"id": 1, "name": "DSA - Trees"},
        {"id": 2, "name": "System Design"},
        {"id": 3, "name": "LLMOps"},
        {"id": 4, "name": "Gen AI"},
    ]
    state = {"proposed_slots": [{"topic": "DSA - Trees", "duration_min": 60}]}
    with patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today",
                      return_value=all_active), \
         patch.object(_nodes._telegram, "send_buttons", mock_send):
        result = done_parser(state)

    # Only 1 topic in plan → skip picker, send rating buttons directly
    assert result["current_topic_id"] == 1
    assert result["current_topic_name"] == "DSA - Trees"
    mock_send.assert_called_once_with("How did DSA - Trees go?", ["😕 Hard", "😐 OK", "😊 Easy"])


def test_done_parser_no_proposed_slots_falls_back_to_all_active():
    """When no proposed_slots are stored, done_parser accepts any unlogged active topic."""
    mock_send = MagicMock(return_value=1)
    with patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today",
                      return_value=[{"id": 7, "name": "DSA - Trees"}]), \
         patch.object(_nodes._telegram, "send_buttons", mock_send):
        result = done_parser({})  # no proposed_slots key at all

    assert result["current_topic_id"] == 7


# ---------------------------------------------------------------------------
# 6–8. route_from_done_parser routing logic
# ---------------------------------------------------------------------------

def test_route_from_done_parser_no_sessions():
    assert route_from_done_parser({"has_unlogged_sessions": False}) == "output"


def test_route_from_done_parser_one_topic_set():
    state = {"has_unlogged_sessions": True, "current_topic_id": 7}
    assert route_from_done_parser(state) == "log_session"


def test_route_from_done_parser_topic_id_none():
    state = {"has_unlogged_sessions": True, "current_topic_id": None}
    assert route_from_done_parser(state) == "select_done_topic"


# ---------------------------------------------------------------------------
# 9–10. select_done_topic (full HITL via graph)
# ---------------------------------------------------------------------------

def test_select_done_topic_sets_topic_id_on_resume():
    """Resuming select_done_topic with a topic name sets current_topic_id in state."""
    path = _make_db([
        {"name": "DSA - Trees", "tier": 1, "status": "active"},
        {"name": "System Design", "tier": 1, "status": "active"},
    ])
    try:
        g = _make_test_graph()
        chat_id = 9001
        config = {"configurable": {"thread_id": str(chat_id)}}
        planned = [
            {"topic": "DSA - Trees", "duration_min": 45},
            {"topic": "System Design", "duration_min": 60},
        ]

        with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
             patch("src.repositories.session_repository.get_connection", _make_get_connection(path)), \
             patch.object(_nodes._telegram, "send_buttons", return_value=10), \
             patch.object(_nodes._telegram, "send_inline_buttons", return_value=10), \
             patch.object(_nodes._telegram, "send_message"), \
             patch.object(_nodes._telegram, "remove_buttons"):
            # done trigger → done_parser sends picker (2 planned topics), pauses at select_done_topic
            g.invoke({"trigger": "done", "chat_id": chat_id, "proposed_slots": planned}, config=config)
            # resume with topic choice → select_done_topic runs, log_session pauses
            g.invoke(Command(resume="DSA - Trees"), config=config)

        snapshot = g.get_state(config)
        state = snapshot.values
        assert state.get("current_topic_name") == "DSA - Trees"
        assert state.get("current_topic_id") is not None
    finally:
        os.remove(path)


def test_select_done_topic_sends_rating_buttons_on_resume():
    """Resuming select_done_topic sends rating buttons for the selected topic."""
    path = _make_db([
        {"name": "DSA - Trees", "tier": 1, "status": "active"},
        {"name": "System Design", "tier": 1, "status": "active"},
    ])
    try:
        g = _make_test_graph()
        chat_id = 9002
        config = {"configurable": {"thread_id": str(chat_id)}}
        mock_send_buttons = MagicMock(return_value=20)
        planned = [
            {"topic": "DSA - Trees", "duration_min": 45},
            {"topic": "System Design", "duration_min": 60},
        ]

        with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
             patch("src.repositories.session_repository.get_connection", _make_get_connection(path)), \
             patch.object(_nodes._telegram, "send_buttons", mock_send_buttons), \
             patch.object(_nodes._telegram, "send_inline_buttons", return_value=10), \
             patch.object(_nodes._telegram, "send_message"), \
             patch.object(_nodes._telegram, "remove_buttons"):
            g.invoke({"trigger": "done", "chat_id": chat_id, "proposed_slots": planned}, config=config)
            mock_send_buttons.reset_mock()
            g.invoke(Command(resume="System Design"), config=config)

        mock_send_buttons.assert_called_once_with("How did System Design go?", ["😕 Hard", "😐 OK", "😊 Easy"])
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# 11. log_weak_areas ending — no remaining unlogged → "All done" message
# ---------------------------------------------------------------------------

def test_log_weak_areas_all_done_message():
    """log_weak_areas returns 'All done' when no topics remain unlogged."""
    state = {"chat_id": 1, "pending_message_id": 5, "current_topic_id": 7, "current_topic_name": "DSA - Trees"}
    with patch("src.agent.nodes.interrupt", return_value="skip"), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes.session_repository, "get_today_session_id", return_value=None), \
         patch.object(_nodes.topic_repository, "update_topic_weak_areas"), \
         patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today", return_value=[]):
        result = log_weak_areas(state)

    assert result["has_unlogged_sessions"] is False
    assert "All done for today" in result["messages"][0]
    assert "DSA - Trees" in result["messages"][0]


# ---------------------------------------------------------------------------
# 12. log_weak_areas ending — remaining topics listed
# ---------------------------------------------------------------------------

def test_log_weak_areas_lists_remaining_topics():
    """log_weak_areas lists only planned remaining topics as bullets, one per line."""
    state = {
        "chat_id": 1,
        "pending_message_id": 5,
        "current_topic_id": 7,
        "current_topic_name": "DSA - Queue",
        "proposed_slots": [
            {"topic": "DSA - Queue", "duration_min": 60},
            {"topic": "System Design", "duration_min": 60},
            {"topic": "LangGraph", "duration_min": 60},
        ],
    }
    # DB returns many active topics; only the planned ones should appear
    all_active = [
        {"id": 2, "name": "System Design"},
        {"id": 3, "name": "LangGraph"},
        {"id": 4, "name": "DSA - Linked Lists"},   # active but NOT in plan — must be excluded
    ]
    with patch("src.agent.nodes.interrupt", return_value="skip"), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes.session_repository, "get_today_session_id", return_value=None), \
         patch.object(_nodes.topic_repository, "update_topic_weak_areas"), \
         patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today", return_value=all_active):
        result = log_weak_areas(state)

    assert result["has_unlogged_sessions"] is False
    msg = result["messages"][0]
    assert "DSA - Queue logged" in msg
    assert "• System Design" in msg
    assert "• LangGraph" in msg
    assert "DSA - Linked Lists" not in msg   # not in today's plan
    assert "/done" in msg


def test_log_weak_areas_remaining_topics_one_per_line():
    """Each remaining topic appears on its own bullet line."""
    state = {
        "chat_id": 1,
        "pending_message_id": 5,
        "current_topic_id": 7,
        "current_topic_name": "DSA - Queue",
        "proposed_slots": [
            {"topic": "DSA - Queue", "duration_min": 60},
            {"topic": "System Design", "duration_min": 60},
        ],
    }
    remaining = [{"id": 2, "name": "System Design"}]
    with patch("src.agent.nodes.interrupt", return_value="skip"), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes.session_repository, "get_today_session_id", return_value=None), \
         patch.object(_nodes.topic_repository, "update_topic_weak_areas"), \
         patch.object(_nodes.topic_repository, "get_active_unlogged_topics_today", return_value=remaining):
        result = log_weak_areas(state)

    msg = result["messages"][0]
    assert "\n• System Design\n" in msg


# ---------------------------------------------------------------------------
# 13–15. get_active_unlogged_topics_today (repository — real SQLite)
# ---------------------------------------------------------------------------

class TestGetActiveUnloggedTopicsToday:
    def setup_method(self):
        self.path = _make_db([
            {"name": "DSA - Trees", "tier": 1, "status": "active", "easiness_factor": 2.5},
            {"name": "System Design", "tier": 1, "status": "active", "easiness_factor": 2.0},
            {"name": "In Progress Topic", "tier": 1, "status": "in_progress"},
            {"name": "Inactive Topic", "tier": 1, "status": "inactive"},
        ])
        self._orig = core_db.DB_PATH
        from pathlib import Path
        core_db.DB_PATH = Path(self.path)

    def teardown_method(self):
        core_db.DB_PATH = self._orig
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_excludes_logged_topics(self):
        """Topics with a session today are excluded from the result."""
        path = _make_db(
            [{"name": "DSA - Trees", "tier": 1, "status": "active"},
             {"name": "System Design", "tier": 1, "status": "active"}],
            sessions=[{"topic": "DSA - Trees"}],
        )
        from pathlib import Path
        core_db.DB_PATH = Path(path)
        from src.repositories.topic_repository import get_active_unlogged_topics_today
        result = get_active_unlogged_topics_today()
        names = [r["name"] for r in result]
        assert "DSA - Trees" not in names
        assert "System Design" in names
        os.remove(path)

    def test_excludes_non_active_topics(self):
        """in_progress and inactive topics are not returned."""
        from src.repositories.topic_repository import get_active_unlogged_topics_today
        result = get_active_unlogged_topics_today()
        names = [r["name"] for r in result]
        assert "In Progress Topic" not in names
        assert "Inactive Topic" not in names

    def test_empty_when_all_logged(self):
        """Returns empty list when every active topic has a session today."""
        path = _make_db(
            [{"name": "DSA - Trees", "tier": 1, "status": "active"}],
            sessions=[{"topic": "DSA - Trees"}],
        )
        from pathlib import Path
        core_db.DB_PATH = Path(path)
        from src.repositories.topic_repository import get_active_unlogged_topics_today
        result = get_active_unlogged_topics_today()
        assert result == []
        os.remove(path)
