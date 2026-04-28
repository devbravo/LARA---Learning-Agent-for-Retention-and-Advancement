"""SQLite helpers for schema initialization and topic seeding.

This module owns local DB path constants, connection creation, and one-time
bootstrap operations used in development/runtime startup flows.
"""

import sqlite3
import logging
from pathlib import Path
from typing import Any

import yaml

DB_PATH = Path(__file__).parents[2] / "db" / "learning.db"
TOPICS_PATH = Path(__file__).parents[2] / "topics.yaml"

logger = logging.getLogger(__name__)


def get_connection(path: str | Path | None = None) -> sqlite3.Connection:
    """Create a SQLite connection configured with row-name access.

    Args:
        path: Optional path to the database file. Defaults to ``DB_PATH``.

    Returns:
        ``sqlite3.Connection`` with ``row_factory`` set to ``sqlite3.Row``.
    """
    connection = sqlite3.connect(path or DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    """Create required tables and apply lightweight compatibility migrations."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:

        # Inspect existing table structure and apply targeted migrations.
        # We avoid broad exception swallowing by checking for column
        # existence via PRAGMA and only running ALTER TABLE when needed.
        try:
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(topics)").fetchall()}
        except sqlite3.DatabaseError as e:
            # If PRAGMA fails for any reason, log and proceed to creation script
            logger.exception("Failed to read topics table info; proceeding to create tables: %s", e)
            existing = set()

        if existing:
            # topics table exists; run safe, targeted migrations
            if "status" not in existing:
                try:
                    conn.execute("ALTER TABLE topics ADD COLUMN status TEXT DEFAULT 'active'")
                    logger.info("Added 'status' column to topics table")
                except sqlite3.OperationalError as e:
                    logger.exception("Failed adding 'status' column: %s", e)

            # Ensure existing rows have a non-null status (runs whether column
            # was just added or already present — guards against manual inserts
            # that bypassed the DEFAULT).
            try:
                conn.execute("UPDATE topics SET status = 'active' WHERE status IS NULL")
            except sqlite3.OperationalError as e:
                logger.exception("Failed updating NULL status values: %s", e)

            if "topic_type" not in existing:
                try:
                    conn.execute("ALTER TABLE topics ADD COLUMN topic_type TEXT DEFAULT 'conceptual'")
                    logger.info("Added 'topic_type' column to topics table")
                except sqlite3.OperationalError as e:
                    logger.exception("Failed adding 'topic_type' column: %s", e)

            if "default_duration_minutes" not in existing:
                try:
                    conn.execute("ALTER TABLE topics ADD COLUMN default_duration_minutes INTEGER NOT NULL DEFAULT 30")
                    logger.info("Added 'default_duration_minutes' column to topics table")
                except sqlite3.OperationalError as e:
                    logger.exception("Failed adding 'default_duration_minutes' column: %s", e)
        else:
            logger.debug("topics table does not exist yet; skipping column migrations and creating tables")

        # sessions column migrations — silent no-ops when column already exists
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN mode TEXT CHECK(mode IN ('study', 'discuss', 'mock'))")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN student_quality INTEGER CHECK(student_quality IN (2, 3, 5))")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN student_weak_areas TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN teacher_quality INTEGER CHECK(teacher_quality IN (2, 3, 5))")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN teacher_weak_areas TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN teacher_source TEXT CHECK(teacher_source IN ('claude', 'algomonster'))")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN calibration_gap INTEGER")
        except sqlite3.OperationalError:
            pass

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                tier INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                topic_type TEXT NOT NULL DEFAULT 'conceptual',
                default_duration_minutes INTEGER NOT NULL DEFAULT 30,
                easiness_factor REAL DEFAULT 2.5,
                interval_days INTEGER DEFAULT 1,
                repetitions INTEGER DEFAULT 0,
                next_review DATE DEFAULT NULL,
                weak_areas TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id            INTEGER NOT NULL REFERENCES topics(id),
                studied_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                duration_min        INTEGER,
                mode                TEXT CHECK(mode IN ('study', 'discuss', 'mock')),
                quality_score       INTEGER CHECK(quality_score IN (2, 3, 5)),
                weak_areas          TEXT,
                suggestions         TEXT,
                student_quality     INTEGER CHECK(student_quality IN (2, 3, 5)),
                student_weak_areas  TEXT,
                teacher_quality     INTEGER CHECK(teacher_quality IN (2, 3, 5)),
                teacher_weak_areas  TEXT,
                teacher_source      TEXT CHECK(teacher_source IN ('claude', 'algomonster')),
                calibration_gap     INTEGER
            );
        """)


def _map_status(topic: dict[str, Any]) -> dict[str, Any]:
    """Normalize topic status from legacy ``active`` to ``status``.
    Args:
        topic: One topic object loaded from ``topics.yaml``.
    Returns:
        A copied topic mapping guaranteed to contain a ``status`` key
        and a ``topic_type`` key.
    """
    t: dict[str, Any] = dict(topic)
    if "status" not in t:
        t["status"] = "active" if t.get("active", True) else "inactive"
    if "topic_type" not in t:
        t["topic_type"] = "conceptual"
    if "default_duration_minutes" not in t:
        t["default_duration_minutes"] = 30
    return t


def seed_topics() -> None:
    """Upsert topics from ``topics.yaml`` into the ``topics`` table."""
    try:
        with open(TOPICS_PATH) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("topics.yaml not found at %s. Create the file with your topic list.", TOPICS_PATH)
        return
    except yaml.YAMLError as e:
        logger.exception("Failed to parse topics.yaml (%s). Please fix YAML syntax: %s", TOPICS_PATH, e)
        return

    if not isinstance(config, dict) or "topics" not in config or not isinstance(config["topics"], list):
        logger.error("Invalid topics.yaml structure: expected a mapping with a 'topics' list at %s", TOPICS_PATH)
        return

    rows = [_map_status(t) for t in config["topics"]]

    try:
        with get_connection() as conn:
            conn.executemany(
            """INSERT INTO topics (name, tier, status, topic_type, default_duration_minutes, next_review)
               VALUES (:name, :tier, :status, :topic_type, :default_duration_minutes, CASE WHEN :status = 'active' THEN date('now') END)
               ON CONFLICT(name) DO UPDATE SET
                   tier = excluded.tier,
                   status = excluded.status,
                   topic_type = excluded.topic_type,
                   default_duration_minutes = excluded.default_duration_minutes,
                   next_review = CASE
                       WHEN excluded.status = 'active' AND topics.next_review IS NULL THEN date('now')
                       WHEN excluded.status != 'active' THEN NULL
                       ELSE topics.next_review
                   END""",
            rows,
            )
    except sqlite3.DatabaseError as e:
        logger.exception("Database error while seeding topics: %s", e)
        return


if __name__ == "__main__":
    init_db()
    seed_topics()

    with get_connection() as conn:
        topic_rows = conn.execute(
            "SELECT id, name, tier, status, easiness_factor, interval_days, repetitions, next_review FROM topics ORDER BY tier, name"
        ).fetchall()

    print(f"{'ID':<4} {'Name':<35} {'Tier':<6} {'Status':<12} {'EF':<6} {'Interval':<10} {'Reps':<6} {'Next Review'}")
    print("-" * 90)
    for row in topic_rows:
        print(f"{row['id']:<4} {row['name']:<35} {row['tier']:<6} {row['status']:<12} {row['easiness_factor']:<6} {row['interval_days']:<10} {row['repetitions']:<6} {row['next_review']}")
