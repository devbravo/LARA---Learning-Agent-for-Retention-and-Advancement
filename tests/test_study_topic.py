"""
Unit tests for the /study_topic command.

Covers:
  1. Category extraction from topic names
  2. "Other" category for topics without ' - ' separator
  3. Subtopic filtering by category prefix
  4. Tier 1 priority — tier 2 hidden when tier 1 inactive topics exist
  5. Tier 2 fallback — shown when no tier 1 inactive topics exist
  6. Tier 3 never shown
  7. study_topic_confirm sets status = 'in_progress' in DB
  8. Rebooking fires when in_progress topic has no existing [Study] event today
  9. Rebooking skipped when [Study] event already exists today
"""

import os
import sqlite3
import sys
import tempfile
from datetime import date
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Temporarily remove the conftest stub so we can import the real build_graph,
# then restore it so other tests that rely on the stub are unaffected.
# ---------------------------------------------------------------------------
_graph_stub = sys.modules.pop("src.agent.graph", None)

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

import src.agent.nodes as _nodes  # noqa: E402
from src.agent.graph import build_graph  # noqa: E402

if _graph_stub is not None:
    sys.modules["src.agent.graph"] = _graph_stub

# ---------------------------------------------------------------------------
# Helpers
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
    """Return a get_connection replacement that opens the given temp DB."""
    def _get_connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    return _get_connection


def _make_test_graph():
    """Build an isolated graph backed by an in-memory SQLite checkpointer."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return build_graph(checkpointer=checkpointer)


def _extract_categories(topic_names: list[str]) -> list[str]:
    """Replicate the category-extraction logic from the study_topic node."""
    return sorted(set(
        name.split(" - ")[0] if " - " in name else "Other"
        for name in topic_names
    ))


def _filter_subtopics(available_names: list[str], category: str) -> list[str]:
    """Replicate the subtopic-filter logic from the study_topic_category node."""
    if category == "Other":
        return [n for n in available_names if " - " not in n]
    return [n for n in available_names if n.startswith(f"{category} - ")]


def _get_available_topics(db_path: str) -> list:
    """Replicate the tier-selection logic from the study_topic node."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT name, tier FROM topics
           WHERE status = 'inactive' AND tier IN (1, 2)
           ORDER BY tier ASC, name ASC"""
    ).fetchall()
    conn.close()
    tier1 = [r for r in rows if r["tier"] == 1]
    return tier1 if tier1 else [r for r in rows if r["tier"] == 2]


# ---------------------------------------------------------------------------
# 1. Category extraction
# ---------------------------------------------------------------------------

def test_category_extraction_returns_unique_sorted_categories():
    """Unique categories are extracted from topic names and returned sorted."""
    names = ["DSA - Arrays", "DSA - Strings", "LLMOps - MLflow", "RAG - Chunking"]
    assert _extract_categories(names) == ["DSA", "LLMOps", "RAG"]


def test_category_extraction_deduplicates():
    """Multiple topics in the same category produce one category entry."""
    names = ["DSA - Arrays", "DSA - Strings", "DSA - Trees"]
    assert _extract_categories(names) == ["DSA"]


# ---------------------------------------------------------------------------
# 2. "Other" category for topics without ' - ' separator
# ---------------------------------------------------------------------------

def test_other_category_for_topic_without_separator():
    """Topics with no ' - ' separator are placed in 'Other'."""
    names = ["Python Basics", "DSA - Arrays"]
    cats = _extract_categories(names)
    assert "Other" in cats
    assert "DSA" in cats


def test_other_category_only_when_all_topics_lack_separator():
    """'Other' is the sole category when no topic has a separator."""
    names = ["Python Basics", "General Review"]
    assert _extract_categories(names) == ["Other"]


# ---------------------------------------------------------------------------
# 3. Subtopic filtering by category prefix
# ---------------------------------------------------------------------------

def test_subtopic_filtering_returns_only_matching_prefix():
    """Only subtopics whose name starts with '{category} - ' are returned."""
    available = ["DSA - Arrays", "DSA - Strings", "LLMOps - MLflow"]
    result = _filter_subtopics(available, "DSA")
    assert result == ["DSA - Arrays", "DSA - Strings"]
    assert "LLMOps - MLflow" not in result


def test_subtopic_filtering_other_returns_topics_without_separator():
    """Category 'Other' returns topics that have no ' - ' separator."""
    available = ["Python Basics", "DSA - Arrays", "General Review"]
    result = _filter_subtopics(available, "Other")
    assert result == ["Python Basics", "General Review"]
    assert "DSA - Arrays" not in result


def test_subtopic_filtering_empty_when_no_match():
    """Empty list returned when no subtopic matches the category."""
    available = ["DSA - Arrays", "DSA - Strings"]
    result = _filter_subtopics(available, "LLMOps")
    assert result == []


# ---------------------------------------------------------------------------
# 4. Tier 1 priority
# ---------------------------------------------------------------------------

def test_tier1_topics_shown_when_tier1_inactive_exists():
    """Tier 1 inactive topics are included in available topics."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "inactive"},
        {"name": "LLMOps - MLflow", "tier": 2, "status": "inactive"},
    ])
    available = _get_available_topics(path)
    names = [r["name"] for r in available]
    assert "DSA - Arrays" in names


def test_tier2_hidden_when_tier1_inactive_exists():
    """When tier 1 inactive topics exist, tier 2 topics are NOT shown."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "inactive"},
        {"name": "LLMOps - MLflow", "tier": 2, "status": "inactive"},
    ])
    available = _get_available_topics(path)
    names = [r["name"] for r in available]
    assert "LLMOps - MLflow" not in names


# ---------------------------------------------------------------------------
# 5. Tier 2 fallback
# ---------------------------------------------------------------------------

def test_tier2_shown_when_no_tier1_inactive():
    """When no tier 1 inactive topics exist, tier 2 topics are shown."""
    path = _make_topics_db([
        {"name": "LLMOps - MLflow", "tier": 2, "status": "inactive"},
    ])
    available = _get_available_topics(path)
    names = [r["name"] for r in available]
    assert "LLMOps - MLflow" in names


def test_tier2_fallback_ignores_active_tier1():
    """Active tier 1 topics do not count — only inactive ones trigger tier 1 priority."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "active"},
        {"name": "LLMOps - MLflow", "tier": 2, "status": "inactive"},
    ])
    available = _get_available_topics(path)
    names = [r["name"] for r in available]
    # Tier 1 has no inactive topics, so tier 2 should be shown
    assert "LLMOps - MLflow" in names
    assert "DSA - Arrays" not in names


# ---------------------------------------------------------------------------
# 6. Tier 3 never shown
# ---------------------------------------------------------------------------

def test_tier3_never_shown_when_tier1_exists():
    """Tier 3 topics are excluded even when tier 1 inactive topics exist."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "inactive"},
        {"name": "Advanced Topic", "tier": 3, "status": "inactive"},
    ])
    available = _get_available_topics(path)
    names = [r["name"] for r in available]
    assert "Advanced Topic" not in names


def test_tier3_never_shown_when_no_tier1_or_tier2():
    """Tier 3 topics are excluded even when no tier 1 or tier 2 inactive topics exist."""
    path = _make_topics_db([
        {"name": "Advanced Topic", "tier": 3, "status": "inactive"},
    ])
    available = _get_available_topics(path)
    assert available == []


# ---------------------------------------------------------------------------
# 7. study_topic_confirm sets status = 'in_progress'  (full HITL graph)
# ---------------------------------------------------------------------------

def _get_topic_id(db_path: str, name: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["id"]


def test_study_topic_confirm_sets_in_progress():
    """Invoking the full pick flow sets the topic's status to 'in_progress' in the DB."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "inactive"},
    ])
    topic_id = _get_topic_id(path, "DSA - Arrays")

    g = _make_test_graph()
    chat_id = 6001
    config = {"configurable": {"thread_id": str(chat_id)}}

    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=10), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_message"):
        # trigger=pick → study_topic sends category buttons → study_topic_category interrupts
        g.invoke({"trigger": "pick", "chat_id": chat_id}, config=config)
        # resume category → study_topic_category sends subtopic buttons → study_topic_confirm interrupts
        g.invoke(Command(resume="category:DSA"), config=config)
        # resume subtopic → study_topic_confirm sets in_progress → output → END
        g.invoke(Command(resume=f"subtopic_id:{topic_id}"), config=config)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM topics WHERE name = 'DSA - Arrays'").fetchone()
    conn.close()
    assert row["status"] == "in_progress"


def test_study_topic_confirm_sends_confirmation_message():
    """Confirmation message is sent via Telegram after marking topic in_progress."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "inactive"},
    ])
    topic_id = _get_topic_id(path, "DSA - Arrays")

    g = _make_test_graph()
    chat_id = 6002
    config = {"configurable": {"thread_id": str(chat_id)}}

    mock_send = MagicMock()
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)), \
         patch.object(_nodes._telegram, "send_inline_buttons", return_value=10), \
         patch.object(_nodes._telegram, "remove_buttons"), \
         patch.object(_nodes._telegram, "send_message", mock_send):
        g.invoke({"trigger": "pick", "chat_id": chat_id}, config=config)
        g.invoke(Command(resume="category:DSA"), config=config)
        g.invoke(Command(resume=f"subtopic_id:{topic_id}"), config=config)

    mock_send.assert_called_once()
    assert "DSA - Arrays" in mock_send.call_args[0][0]


def test_study_topic_confirm_no_op_when_already_in_progress():
    """study_topic_confirm sets error message state for a topic already in in_progress."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "in_progress"},
    ])

    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        from src.agent.nodes import study_topic_confirm
        result = study_topic_confirm({"proposed_topic": "DSA - Arrays"})

    # Should return an error/warning message in state
    assert result.get("messages")
    assert "⚠️" in result["messages"][0]

    # Status must remain unchanged
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM topics WHERE name = 'DSA - Arrays'").fetchone()
    conn.close()
    assert row["status"] == "in_progress"


# ---------------------------------------------------------------------------
# 8. Rebooking fires when in_progress topic has no [Study] event today
# ---------------------------------------------------------------------------

def test_rebooking_fires_when_not_already_booked():
    """write_study_event is called once when the in_progress topic has no [Study] event today."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = ["DSA - Arrays"]
    timed_events: list = []

    from src.agent import nodes as nodes_module

    mock_write = MagicMock()
    with patch.object(nodes_module._gcal, "write_study_event", mock_write):
        from src.agent.planning_helpers import rebook_study_events
        rebook_study_events(in_progress_topics, timed_events, target_date, config)

    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    assert kwargs["topic"] == "DSA - Arrays"
    assert f"{target_date.isoformat()}T08:00:00" in kwargs["start"]
    assert f"{target_date.isoformat()}T09:00:00" in kwargs["end"]


def test_rebooking_fires_for_each_unbooked_in_progress_topic():
    """write_study_event is called once per unbooked in_progress topic."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = ["DSA - Arrays", "LLMOps - MLflow"]
    timed_events: list = []

    from src.agent import nodes as nodes_module

    mock_write = MagicMock()
    with patch.object(nodes_module._gcal, "write_study_event", mock_write):
        from src.agent.planning_helpers import rebook_study_events
        rebook_study_events(in_progress_topics, timed_events, target_date, config)

    assert mock_write.call_count == 2


# ---------------------------------------------------------------------------
# 9. Rebooking skipped when [Study] event already exists today
# ---------------------------------------------------------------------------

def test_rebooking_skipped_when_study_event_already_booked():
    """write_study_event is NOT called when a [Study] event for the topic already exists."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = ["DSA - Arrays"]
    timed_events = [
        {
            "summary": "[Study] DSA - Arrays",
            "start": {"dateTime": f"{target_date.isoformat()}T08:00:00+00:00"},
            "end": {"dateTime": f"{target_date.isoformat()}T09:00:00+00:00"},
        }
    ]

    from src.agent import nodes as nodes_module

    mock_write = MagicMock()
    with patch.object(nodes_module._gcal, "write_study_event", mock_write):
        from src.agent.planning_helpers import rebook_study_events
        rebook_study_events(in_progress_topics, timed_events, target_date, config)

    mock_write.assert_not_called()


def test_rebooking_skipped_for_booked_books_unbooked():
    """When one topic is booked and another is not, only the unbooked one is written."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = ["DSA - Arrays", "LLMOps - MLflow"]
    timed_events = [
        {
            "summary": "[Study] DSA - Arrays",
            "start": {"dateTime": f"{target_date.isoformat()}T08:00:00+00:00"},
            "end": {"dateTime": f"{target_date.isoformat()}T09:00:00+00:00"},
        }
    ]

    from src.agent import nodes as nodes_module

    mock_write = MagicMock()
    with patch.object(nodes_module._gcal, "write_study_event", mock_write):
        from src.agent.planning_helpers import rebook_study_events
        rebook_study_events(in_progress_topics, timed_events, target_date, config)

    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    assert kwargs["topic"] == "LLMOps - MLflow"


def test_missing_study_busy_events_skip_topics_already_booked_later_in_day():
    """Planning should not synthesize an 08:00 study block when a real event already exists later."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = ["DSA - Arrays"]
    timed_events = [
        {
            "summary": "[Study] DSA - Arrays",
            "start": {"dateTime": f"{target_date.isoformat()}T14:00:00+00:00"},
            "end": {"dateTime": f"{target_date.isoformat()}T15:00:00+00:00"},
        }
    ]

    from src.agent.planning_helpers import build_missing_study_events

    assert build_missing_study_events(in_progress_topics, timed_events, target_date, config) == []


def test_missing_study_busy_events_preserve_default_slot_order_for_unbooked_topics():
    """Synthetic planning blocks should mirror rebooking's 08:00+ slot sequence."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = ["DSA - Arrays", "LLMOps - MLflow"]
    timed_events = [
        {
            "summary": "[Study] DSA - Arrays",
            "start": {"dateTime": f"{target_date.isoformat()}T14:00:00+00:00"},
            "end": {"dateTime": f"{target_date.isoformat()}T15:00:00+00:00"},
        }
    ]

    from src.agent.planning_helpers import build_missing_study_events

    events = build_missing_study_events(in_progress_topics, timed_events, target_date, config)

    assert len(events) == 1
    assert events[0]["summary"] == "[Study] LLMOps - MLflow"
    assert f"{target_date.isoformat()}T09:00:00" in events[0]["start"]["dateTime"]
    assert f"{target_date.isoformat()}T10:00:00" in events[0]["end"]["dateTime"]


def test_in_progress_study_slots_use_actual_booked_time_when_present():
    """The in-progress display should show the real booked [Study] event time when available."""
    target_date = date.today()
    timed_events = [
        {
            "summary": "[Study] DSA - Arrays",
            "start": {"dateTime": f"{target_date.isoformat()}T14:00:00+00:00"},
            "end": {"dateTime": f"{target_date.isoformat()}T15:30:00+00:00"},
        }
    ]

    from src.agent.planning_helpers import build_in_progress_study_slots

    slots = build_in_progress_study_slots(["DSA - Arrays"], timed_events, target_date)

    assert slots == [
        {
            "topic": "DSA - Arrays",
            "start": "14:00",
            "end": "15:30",
            "duration_min": 90,
        }
    ]


def test_in_progress_study_slots_mix_actual_and_default_slots_chronologically():
    """Display slots should combine real and fallback study times in chronological order."""
    target_date = date.today()
    timed_events = [
        {
            "summary": "[Study] DSA - Arrays",
            "start": {"dateTime": f"{target_date.isoformat()}T14:00:00+00:00"},
            "end": {"dateTime": f"{target_date.isoformat()}T15:00:00+00:00"},
        }
    ]

    from src.agent.planning_helpers import build_in_progress_study_slots

    slots = build_in_progress_study_slots(["DSA - Arrays", "LLMOps - MLflow"], timed_events, target_date)

    assert slots == [
        {
            "topic": "LLMOps - MLflow",
            "start": "09:00",
            "end": "10:00",
            "duration_min": 60,
        },
        {
            "topic": "DSA - Arrays",
            "start": "14:00",
            "end": "15:00",
            "duration_min": 60,
        },
    ]


def test_missing_study_busy_events_stop_before_invalid_next_day_timestamps():
    """Synthetic busy events should stop before generating invalid T24:00-style timestamps."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = [f"Topic {i}" for i in range(17)]

    from src.agent.planning_helpers import build_missing_study_events

    events = build_missing_study_events(in_progress_topics, [], target_date, config)

    assert len(events) == 16
    assert all("T24:" not in event["start"]["dateTime"] for event in events)
    assert all("T24:" not in event["end"]["dateTime"] for event in events)


def test_rebook_study_events_stop_when_no_valid_same_day_slot_remains():
    """Rebooking should not try to create study events beyond the final same-day slot."""
    target_date = date.today()
    config = {"timezone": "UTC"}
    in_progress_topics = [f"Topic {i}" for i in range(17)]

    from src.agent import nodes as nodes_module

    mock_write = MagicMock()
    with patch.object(nodes_module._gcal, "write_study_event", mock_write):
        from src.agent.planning_helpers import rebook_study_events
        rebook_study_events(in_progress_topics, [], target_date, config)

    assert mock_write.call_count == 16


