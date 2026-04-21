"""Unit tests for view_service.get_study_snapshot().

Covers:
  1. All three sections populated — correct grouping
  2. Only overdue topics — due today and in_progress sections omitted
  3. Only in_progress topics — overdue and due today sections omitted
  4. Nothing at all — returns empty state
  5. Overdue topics sorted by most overdue first
  6. Weak areas appear on the correct topics
  7. Topics due tomorrow do NOT appear (strict date filter)
"""

import os
import sqlite3
import tempfile
from datetime import date, timedelta
from unittest.mock import patch

from src.services.view_service import get_study_snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date(2026, 4, 19)
YESTERDAY = TODAY - timedelta(days=1)
THREE_DAYS_AGO = TODAY - timedelta(days=3)
TOMORROW = TODAY + timedelta(days=1)


def _make_topics_db(topics: list[dict]) -> str:
    """Create a temp SQLite DB seeded with given topics. Returns db_path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            tier INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            easiness_factor REAL DEFAULT 2.5,
            interval_days INTEGER DEFAULT 1,
            repetitions INTEGER DEFAULT 0,
            next_review DATE,
            weak_areas TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for t in topics:
        conn.execute(
            "INSERT INTO topics (name, tier, status, next_review, weak_areas) VALUES (?, ?, ?, ?, ?)",
            (t["name"], t.get("tier", 1), t["status"], t.get("next_review"), t.get("weak_areas")),
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


# ---------------------------------------------------------------------------
# 1. All three sections populated
# ---------------------------------------------------------------------------

def test_all_sections_populated():
    """All three sections are populated with the correct topics."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "active", "next_review": THREE_DAYS_AGO.isoformat()},
        {"name": "Gen AI Design", "tier": 1, "status": "active", "next_review": YESTERDAY.isoformat()},
        {"name": "LangGraph", "tier": 1, "status": "active", "next_review": TODAY.isoformat()},
        {"name": "LLMOps", "tier": 1, "status": "in_progress"},
    ])
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        snap = get_study_snapshot(TODAY)

    assert len(snap["overdue"]) == 2
    assert len(snap["due_today"]) == 1
    assert len(snap["in_progress"]) == 1
    assert snap["due_today"][0]["name"] == "LangGraph"
    assert snap["in_progress"][0]["name"] == "LLMOps"


# ---------------------------------------------------------------------------
# 2. Only overdue topics
# ---------------------------------------------------------------------------

def test_only_overdue_topics():
    """Due today and in_progress sections are empty when only overdue topics exist."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "active", "next_review": YESTERDAY.isoformat()},
    ])
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        snap = get_study_snapshot(TODAY)

    assert len(snap["overdue"]) == 1
    assert snap["overdue"][0]["name"] == "DSA - Arrays"
    assert snap["due_today"] == []
    assert snap["in_progress"] == []


# ---------------------------------------------------------------------------
# 3. Only in_progress topics
# ---------------------------------------------------------------------------

def test_only_in_progress_topics():
    """Overdue and due today sections are empty when only in_progress topics exist."""
    path = _make_topics_db([
        {"name": "Sales Engineering", "tier": 2, "status": "in_progress"},
    ])
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        snap = get_study_snapshot(TODAY)

    assert snap["overdue"] == []
    assert snap["due_today"] == []
    assert len(snap["in_progress"]) == 1
    assert snap["in_progress"][0]["name"] == "Sales Engineering"


# ---------------------------------------------------------------------------
# 4. Nothing at all
# ---------------------------------------------------------------------------

def test_empty_snapshot():
    """All sections are empty when there are no relevant topics."""
    path = _make_topics_db([])
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        snap = get_study_snapshot(TODAY)

    assert snap == {"overdue": [], "due_today": [], "in_progress": []}


# ---------------------------------------------------------------------------
# 5. Overdue sorted most overdue first
# ---------------------------------------------------------------------------

def test_overdue_sorted_most_overdue_first():
    """Overdue topics appear with the most overdue (largest days_overdue) first."""
    path = _make_topics_db([
        {"name": "Topic A", "tier": 1, "status": "active", "next_review": YESTERDAY.isoformat()},
        {"name": "Topic B", "tier": 1, "status": "active", "next_review": THREE_DAYS_AGO.isoformat()},
    ])
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        snap = get_study_snapshot(TODAY)

    assert snap["overdue"][0]["name"] == "Topic B"
    assert snap["overdue"][0]["days_overdue"] == 3
    assert snap["overdue"][1]["name"] == "Topic A"
    assert snap["overdue"][1]["days_overdue"] == 1


# ---------------------------------------------------------------------------
# 6. Weak areas on correct topics
# ---------------------------------------------------------------------------

def test_weak_areas_on_correct_topics():
    """Weak areas are attached to the right topics and None when absent."""
    path = _make_topics_db([
        {"name": "DSA - Arrays", "tier": 1, "status": "active",
         "next_review": YESTERDAY.isoformat(), "weak_areas": "rotate arrays"},
        {"name": "LangGraph", "tier": 1, "status": "active", "next_review": TODAY.isoformat()},
        {"name": "LLMOps", "tier": 2, "status": "in_progress", "weak_areas": "eval pipelines"},
    ])
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        snap = get_study_snapshot(TODAY)

    assert snap["overdue"][0]["weak_areas"] == "rotate arrays"
    assert snap["due_today"][0]["weak_areas"] is None
    assert snap["in_progress"][0]["weak_areas"] == "eval pipelines"


# ---------------------------------------------------------------------------
# 7. Topics due tomorrow excluded
# ---------------------------------------------------------------------------

def test_topics_due_tomorrow_excluded():
    """Active topics with next_review tomorrow do not appear in any section."""
    path = _make_topics_db([
        {"name": "Future Topic", "tier": 1, "status": "active", "next_review": TOMORROW.isoformat()},
    ])
    with patch("src.repositories.topic_repository.get_connection", _make_get_connection(path)):
        snap = get_study_snapshot(TODAY)

    assert snap["overdue"] == []
    assert snap["due_today"] == []
    assert snap["in_progress"] == []
