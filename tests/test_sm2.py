import sqlite3
import tempfile
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.sm2 import calculate_next_review, get_due_topics, update_topic_after_session
from src.infrastructure import db as core_db


def test_quality_2_hard_resets_interval_and_repetitions():
    """Quality 2 (Hard): interval resets to 1, repetitions reset to 0."""
    ef, interval, reps = calculate_next_review(
        quality=2, easiness_factor=2.5, interval_days=6, repetitions=3
    )
    assert interval == 1
    assert reps == 0


def test_quality_3_ok_first_session():
    """Quality 3 first session (repetitions=0): interval = 1, repetitions = 1."""
    ef, interval, reps = calculate_next_review(
        quality=3, easiness_factor=2.5, interval_days=1, repetitions=0
    )
    assert interval == 1
    assert reps == 1


def test_quality_3_ok_second_session():
    """Quality 3 second session (repetitions=1): interval = 6, repetitions = 2."""
    ef, interval, reps = calculate_next_review(
        quality=3, easiness_factor=2.5, interval_days=1, repetitions=1
    )
    assert interval == 6
    assert reps == 2


def test_quality_5_easy_third_session():
    """Quality 5 third session (repetitions=2): interval = round(6 * ef), repetitions = 3."""
    starting_ef = 2.5
    ef, interval, reps = calculate_next_review(
        quality=5, easiness_factor=starting_ef, interval_days=6, repetitions=2
    )
    assert interval == round(6 * starting_ef)
    assert reps == 3
    assert ef > starting_ef  # EF should increase on easy


def test_ef_never_drops_below_1_3():
    """EF floor is 1.3 no matter how many hard sessions."""
    ef = 1.4
    interval = 1
    reps = 0
    for _ in range(20):
        ef, interval, reps = calculate_next_review(
            quality=2, easiness_factor=ef, interval_days=interval, repetitions=reps
        )
    assert ef >= 1.3


def test_quality_2_decreases_ef():
    """Quality 2 reduces easiness_factor (above the floor)."""
    ef, _, _ = calculate_next_review(
        quality=2, easiness_factor=2.5, interval_days=1, repetitions=0
    )
    assert ef < 2.5


def test_quality_5_increases_ef():
    """Quality 5 increases easiness_factor."""
    ef, _, _ = calculate_next_review(
        quality=5, easiness_factor=2.5, interval_days=1, repetitions=0
    )
    assert ef > 2.5


def test_interval_grows_beyond_second_session():
    """After repetitions > 1, interval = round(interval * ef)."""
    ef, interval, reps = calculate_next_review(
        quality=5, easiness_factor=2.5, interval_days=6, repetitions=2
    )
    assert interval == round(6 * 2.5)  # 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(topics: list[dict]) -> tuple[Path, int]:
    """Create a temp SQLite DB seeded with the given topics for one engineer.

    Returns:
        (db_path, engineer_id).
    """
    fd, path_str = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path_str)
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
            tier INTEGER NOT NULL,
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
            next_review DATE,
            weak_areas TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(engineer_id, topic_id)
        );
    """)
    cursor = conn.execute(
        "INSERT INTO engineers (name, platform, external_id) VALUES ('Diego', 'telegram', '1')"
    )
    engineer_id = cursor.lastrowid
    for t in topics:
        cursor = conn.execute(
            "INSERT INTO topic_catalog (name, tier) VALUES (?, ?)", (t["name"], t["tier"])
        )
        topic_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO engineer_topic_progress
                   (engineer_id, topic_id, status, easiness_factor, interval_days, repetitions, next_review)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (engineer_id, topic_id, t["status"], t["easiness_factor"],
             t["interval_days"], t["repetitions"], t["next_review"]),
        )
    conn.commit()
    conn.close()
    return Path(path_str), engineer_id


# ---------------------------------------------------------------------------
# get_due_topics tests
# ---------------------------------------------------------------------------

def test_get_due_topics_returns_topics_due_today():
    """Topics with next_review <= today and status='active' are returned."""
    db_path, engineer_id = _make_db([
        {"name": "Topic A", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
    ])
    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id)
    assert len(result) == 1
    assert result[0]["name"] == "Topic A"


def test_get_due_topics_excludes_future_topics():
    """Topics with next_review > today are not returned."""
    db_path, engineer_id = _make_db([
        {"name": "Future Topic", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0,
         "next_review": (date.today() + timedelta(days=3)).isoformat()},
    ])
    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id)
    assert result == []


def test_get_due_topics_excludes_inactive_topics():
    """Topics with status != 'active' are not returned even if due."""
    db_path, engineer_id = _make_db([
        {"name": "Inactive Topic", "tier": 1, "status": "inactive", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
    ])
    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id)
    assert result == []


def test_get_due_topics_target_date_tomorrow():
    """Passing tomorrow as target_date includes topics due tomorrow."""
    tomorrow = date.today() + timedelta(days=1)
    db_path, engineer_id = _make_db([
        {"name": "Tomorrow Topic", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": tomorrow.isoformat()},
    ])
    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id, target_date=tomorrow)
    assert len(result) == 1
    assert result[0]["name"] == "Tomorrow Topic"


def test_get_due_topics_target_date_tomorrow_excludes_today_only():
    """target_date=tomorrow returns both today's and tomorrow's due topics."""
    tomorrow = date.today() + timedelta(days=1)
    db_path, engineer_id = _make_db([
        {"name": "Due Today", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
        {"name": "Due Tomorrow", "tier": 2, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": tomorrow.isoformat()},
    ])
    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id, target_date=tomorrow)
    names = [r["name"] for r in result]
    assert "Due Today" in names
    assert "Due Tomorrow" in names


def test_get_due_topics_null_next_review_excluded():
    """Topics with NULL next_review are not returned."""
    db_path, engineer_id = _make_db([
        {"name": "No Review Date", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": None},
    ])
    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id)
    assert result == []


def test_get_due_topics_ordered_by_tier_then_easiness():
    """Results are ordered tier ASC, easiness_factor ASC."""
    db_path, engineer_id = _make_db([
        {"name": "Tier2 Easy", "tier": 2, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
        {"name": "Tier1 Hard", "tier": 1, "status": "active", "easiness_factor": 1.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
        {"name": "Tier1 Easy", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
    ])
    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id)
    assert result[0]["name"] == "Tier1 Hard"
    assert result[1]["name"] == "Tier1 Easy"
    assert result[2]["name"] == "Tier2 Easy"


def test_get_due_topics_does_not_return_other_engineers_topics():
    """A due topic belonging to a different engineer must not appear."""
    db_path, engineer_id = _make_db([
        {"name": "Mine", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
    ])
    conn = sqlite3.connect(db_path)
    other_engineer = conn.execute(
        "INSERT INTO engineers (name, platform, external_id) VALUES ('Other', 'telegram', '2')"
    ).lastrowid
    topic_id = conn.execute("INSERT INTO topic_catalog (name, tier) VALUES ('Theirs', 1)").lastrowid
    conn.execute(
        """INSERT INTO engineer_topic_progress
               (engineer_id, topic_id, status, easiness_factor, interval_days, repetitions, next_review)
           VALUES (?, ?, 'active', 2.5, 1, 0, ?)""",
        (other_engineer, topic_id, date.today().isoformat()),
    )
    conn.commit()
    conn.close()

    with patch.object(core_db, "DB_PATH", db_path):
        result = get_due_topics(engineer_id)
    names = [r["name"] for r in result]
    assert names == ["Mine"]


# ---------------------------------------------------------------------------
# update_topic_after_session tests
# ---------------------------------------------------------------------------

def test_update_topic_after_session_persists_recomputed_fields():
    """A studied topic's SM-2 fields are recalculated and persisted."""
    db_path, engineer_id = _make_db([
        {"name": "Topic A", "tier": 1, "status": "active", "easiness_factor": 2.5,
         "interval_days": 1, "repetitions": 0, "next_review": date.today().isoformat()},
    ])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    topic_id = conn.execute("SELECT id FROM topic_catalog WHERE name = 'Topic A'").fetchone()["id"]
    conn.close()

    with patch.object(core_db, "DB_PATH", db_path):
        update_topic_after_session(engineer_id, topic_id=topic_id, quality=5)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT easiness_factor, repetitions FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
            (engineer_id, topic_id),
        ).fetchone()
        conn.close()

    assert row["repetitions"] == 1
    assert row["easiness_factor"] > 2.5


def test_update_topic_after_session_raises_for_unknown_catalog_topic():
    """Raises when the topic doesn't exist in the catalog at all."""
    db_path, engineer_id = _make_db([])

    with patch.object(core_db, "DB_PATH", db_path):
        with pytest.raises(ValueError, match="not found in catalog"):
            update_topic_after_session(engineer_id, topic_id=999, quality=3)


def test_update_topic_after_session_raises_when_engineer_has_no_progress_row():
    """Raises a distinct, actionable error when the catalog topic exists but this
    engineer has no progress row for it yet — must not claim the topic is missing."""
    db_path, engineer_id = _make_db([])
    conn = sqlite3.connect(db_path)
    topic_id = conn.execute("INSERT INTO topic_catalog (name, tier) VALUES ('Untouched', 1)").lastrowid
    conn.commit()
    conn.close()
    # No progress row inserted for this engineer.

    with patch.object(core_db, "DB_PATH", db_path):
        with pytest.raises(ValueError) as exc_info:
            update_topic_after_session(engineer_id, topic_id=topic_id, quality=3)

    message = str(exc_info.value)
    assert "not found in catalog" not in message
    assert str(engineer_id) in message
    assert "Untouched" in message
