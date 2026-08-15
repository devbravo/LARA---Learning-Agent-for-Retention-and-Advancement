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
    """Create engineer/catalog/progress tables and return the DB path."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE engineers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_id TEXT NOT NULL,
            UNIQUE(platform, external_id)
        );
        CREATE TABLE topic_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            tier INTEGER NOT NULL DEFAULT 1,
            topic_type TEXT NOT NULL DEFAULT 'conceptual',
            default_duration_minutes INTEGER NOT NULL DEFAULT 30
        );
        CREATE TABLE engineer_topic_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engineer_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'inactive',
            easiness_factor REAL DEFAULT 2.5,
            interval_days INTEGER DEFAULT 1,
            repetitions INTEGER DEFAULT 0,
            next_review DATE DEFAULT NULL,
            weak_areas TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(engineer_id, topic_id)
        );
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


def _insert_engineer(db_path, engineer_id: int = 1) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO engineers (id, name, platform, external_id) VALUES (?, 'Diego', 'telegram', ?)",
        (engineer_id, str(engineer_id)),
    )
    conn.commit()
    conn.close()
    return engineer_id


def _insert_catalog_topic(db_path, topic_id: int, name: str, tier: int = 1) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO topic_catalog (id, name, tier) VALUES (?, ?, ?)",
        (topic_id, name, tier),
    )
    conn.commit()
    conn.close()
    return topic_id


def _insert_progress(db_path, engineer_id: int, topic_id: int, **kwargs) -> None:
    defaults = {"status": "in_progress", "easiness_factor": 2.5, "interval_days": 1, "repetitions": 0}
    defaults.update(kwargs)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO engineer_topic_progress (engineer_id, topic_id, status, easiness_factor, interval_days, repetitions)
           VALUES (:engineer_id, :topic_id, :status, :easiness_factor, :interval_days, :repetitions)""",
        {**defaults, "engineer_id": engineer_id, "topic_id": topic_id},
    )
    conn.commit()
    conn.close()


def _read_progress(db_path, engineer_id: int, topic_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
        (engineer_id, topic_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# graduate_topic
# ---------------------------------------------------------------------------

def test_graduate_topic_sets_active(tmp_path):
    """graduate_topic resets SM-2 fields and sets status to active."""
    db_path = _create_db(tmp_path)
    engineer_id = _insert_engineer(db_path)
    _insert_catalog_topic(db_path, 1, "DSA - Arrays")
    _insert_progress(db_path, engineer_id, 1, status="in_progress", easiness_factor=2.1, interval_days=5, repetitions=3)

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        name = topic_service.graduate_topic(engineer_id, 1)

    assert name == "DSA - Arrays"

    row = _read_progress(db_path, engineer_id, 1)
    assert row["status"] == "active"
    assert row["repetitions"] == 0
    assert row["easiness_factor"] == 2.5
    assert row["next_review"] is not None  # date('now', '+1 day')


def test_graduate_topic_raises_for_unknown_id(tmp_path):
    """graduate_topic raises ValueError when the topic has no progress row for this engineer."""
    db_path = _create_db(tmp_path)
    engineer_id = _insert_engineer(db_path)

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        with pytest.raises(ValueError, match="not found in DB"):
            topic_service.graduate_topic(engineer_id, 999)


# ---------------------------------------------------------------------------
# get_in_progress_topics
# ---------------------------------------------------------------------------

def test_get_in_progress_topics_returns_correct_rows(tmp_path):
    """get_in_progress_topics returns only in_progress topics, ordered by tier then name."""
    db_path = _create_db(tmp_path)
    engineer_id = _insert_engineer(db_path)
    _insert_catalog_topic(db_path, 1, "Topic B", tier=2)
    _insert_catalog_topic(db_path, 2, "Topic A", tier=1)
    _insert_catalog_topic(db_path, 3, "Topic C", tier=1)  # active — excluded
    _insert_catalog_topic(db_path, 4, "Topic D", tier=1)
    _insert_progress(db_path, engineer_id, 1, status="in_progress")
    _insert_progress(db_path, engineer_id, 2, status="in_progress")
    _insert_progress(db_path, engineer_id, 3, status="active")
    _insert_progress(db_path, engineer_id, 4, status="in_progress")

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        result = topic_service.get_in_progress_topics(engineer_id)

    names = [r["name"] for r in result]
    assert names == ["Topic A", "Topic D", "Topic B"]  # tier 1 first, then tier 2; alpha within tier
    assert all("id" in r and "name" in r for r in result)


def test_get_in_progress_topics_returns_empty_when_none(tmp_path):
    """get_in_progress_topics returns an empty list when no topics are in_progress."""
    db_path = _create_db(tmp_path)
    engineer_id = _insert_engineer(db_path)
    _insert_catalog_topic(db_path, 1, "Topic A")
    _insert_progress(db_path, engineer_id, 1, status="active")

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        result = topic_service.get_in_progress_topics(engineer_id)

    assert result == []


# ---------------------------------------------------------------------------
# get_topic_name_by_id — catalog-only, no engineer_id
# ---------------------------------------------------------------------------

def test_get_topic_name_by_id_is_catalog_only(tmp_path):
    db_path = _create_db(tmp_path)
    _insert_catalog_topic(db_path, 1, "DSA - Arrays")

    with patch("src.repositories.topic_repository.get_connection", side_effect=_make_conn_factory(db_path)):
        assert topic_service.get_topic_name_by_id(1) == "DSA - Arrays"
        assert topic_service.get_topic_name_by_id(999) is None
