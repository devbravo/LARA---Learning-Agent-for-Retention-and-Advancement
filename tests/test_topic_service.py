"""
Unit tests for src/services/topic_service.py

Uses a real temp SQLite DB so SQL correctness is verified.
All calls to get_connection are patched to return connections to the temp DB.
"""

import sqlite3
from unittest.mock import patch

import pytest

from src.services import topic_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_db(tmp_path):
    """Create a minimal topics table and return the DB path."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            tier INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            easiness_factor REAL DEFAULT 2.5,
            interval_days INTEGER DEFAULT 1,
            repetitions INTEGER DEFAULT 0,
            next_review DATE DEFAULT NULL,
            weak_areas TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def _make_conn_factory(db_path):
    """Return a callable that creates a fresh sqlite3.Connection to db_path."""
    def _factory():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    return _factory


def _read_topic(db_path, topic_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# graduate_topic
# ---------------------------------------------------------------------------

def test_graduate_topic_sets_active(tmp_path):
    """graduate_topic resets SM-2 fields and sets status to active."""
    db_path = _create_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO topics (name, tier, status, easiness_factor, interval_days, repetitions) "
        "VALUES ('DSA - Arrays', 1, 'in_progress', 2.1, 5, 3)"
    )
    conn.commit()
    conn.close()

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        name = topic_service.graduate_topic(1)

    assert name == "DSA - Arrays"

    row = _read_topic(db_path, 1)
    assert row["status"] == "active"
    assert row["repetitions"] == 0
    assert row["easiness_factor"] == 2.5
    assert row["next_review"] is not None  # date('now', '+1 day')


def test_graduate_topic_raises_for_unknown_id(tmp_path):
    """graduate_topic raises ValueError when the topic id does not exist."""
    db_path = _create_db(tmp_path)

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        with pytest.raises(ValueError, match="not found in DB"):
            topic_service.graduate_topic(999)


# ---------------------------------------------------------------------------
# get_in_progress_topics
# ---------------------------------------------------------------------------

def test_get_in_progress_topics_returns_correct_rows(tmp_path):
    """get_in_progress_topics returns only in_progress topics, ordered by tier then name."""
    db_path = _create_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO topics (name, tier, status) VALUES (?, ?, ?)",
        [
            ("Topic B", 2, "in_progress"),
            ("Topic A", 1, "in_progress"),
            ("Topic C", 1, "active"),        # excluded — not in_progress
            ("Topic D", 1, "in_progress"),
        ],
    )
    conn.commit()
    conn.close()

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        result = topic_service.get_in_progress_topics()

    names = [r["name"] for r in result]
    assert names == ["Topic A", "Topic D", "Topic B"]  # tier 1 first, then tier 2; alpha within tier
    assert all("id" in r and "name" in r for r in result)


def test_get_in_progress_topics_returns_empty_when_none(tmp_path):
    """get_in_progress_topics returns an empty list when no topics are in_progress."""
    db_path = _create_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO topics (name, tier, status) VALUES ('Topic A', 1, 'active')")
    conn.commit()
    conn.close()

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        result = topic_service.get_in_progress_topics()

    assert result == []
