"""Migration: split single-user `topics` into a shared catalog + per-engineer
progress, add an `engineers` table, and attribute existing `sessions` rows to
one engineer.

This migration is idempotent: re-running it against an already-migrated
database is a no-op (it upserts the engineer by (platform, external_id) and
the catalog by topic name, and only backfills `sessions.engineer_id` where
still NULL).

Usage:
    python migrations/migrate_multi_user.py --db-path db/learning.db \
        --engineer-name "Diego Sabajo" --platform telegram --external-id 123456
"""

import argparse
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS engineers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    platform TEXT NOT NULL CHECK(platform IN ('telegram', 'slack')),
    external_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, external_id)
);

CREATE TABLE IF NOT EXISTS topic_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    tier INTEGER NOT NULL,
    topic_type TEXT NOT NULL DEFAULT 'conceptual',
    default_duration_minutes INTEGER NOT NULL DEFAULT 30
);

CREATE TABLE IF NOT EXISTS engineer_topic_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engineer_id INTEGER NOT NULL REFERENCES engineers(id),
    topic_id INTEGER NOT NULL REFERENCES topic_catalog(id),
    status TEXT NOT NULL DEFAULT 'inactive',
    easiness_factor REAL DEFAULT 2.5,
    interval_days INTEGER DEFAULT 1,
    repetitions INTEGER DEFAULT 0,
    next_review DATE DEFAULT NULL,
    weak_areas TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(engineer_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_progress_engineer_status
    ON engineer_topic_progress(engineer_id, status);
"""


def run_migration(db_path: str, engineer_name: str, platform: str, external_id: str) -> None:
    """Apply the multi-user schema and migrate existing single-user data.

    Args:
        db_path: Path to the existing LARA SQLite database file.
        engineer_name: Display name for the engineer who owns the existing data.
        platform: ``'telegram'`` or ``'slack'`` — the existing bot platform.
        external_id: The existing chat id / external user id on that platform.

    Raises:
        FileNotFoundError: If db_path does not point to an existing file.
        sqlite3.Error: If migration fails (rolled back before re-raising).
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)

        conn.execute(
            """INSERT INTO engineers (name, platform, external_id)
               VALUES (?, ?, ?)
               ON CONFLICT(platform, external_id) DO NOTHING""",
            (engineer_name, platform, external_id),
        )
        engineer = conn.execute(
            "SELECT id FROM engineers WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
        engineer_id = engineer["id"]

        has_legacy_topics = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topics'"
        ).fetchone()
        if has_legacy_topics is None:
            conn.commit()
            print(f"No legacy 'topics' table found — schema created, nothing to migrate for {db_path}")
            return

        old_topics = conn.execute("SELECT * FROM topics").fetchall()
        old_to_new_topic_id: dict[int, int] = {}
        for old in old_topics:
            conn.execute(
                """INSERT INTO topic_catalog (name, tier, topic_type, default_duration_minutes)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       tier = excluded.tier,
                       topic_type = excluded.topic_type,
                       default_duration_minutes = excluded.default_duration_minutes""",
                (old["name"], old["tier"], old["topic_type"], old["default_duration_minutes"]),
            )
            new_id = conn.execute(
                "SELECT id FROM topic_catalog WHERE name = ?", (old["name"],)
            ).fetchone()["id"]
            old_to_new_topic_id[old["id"]] = new_id

            conn.execute(
                """INSERT INTO engineer_topic_progress
                       (engineer_id, topic_id, status, easiness_factor, interval_days,
                        repetitions, next_review, weak_areas, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(engineer_id, topic_id) DO NOTHING""",
                (engineer_id, new_id, old["status"], old["easiness_factor"],
                 old["interval_days"], old["repetitions"], old["next_review"],
                 old["weak_areas"], old["updated_at"]),
            )

        has_engineer_id_col = "engineer_id" in {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if not has_engineer_id_col:
            conn.execute("ALTER TABLE sessions ADD COLUMN engineer_id INTEGER REFERENCES engineers(id)")

        for old_id, new_id in old_to_new_topic_id.items():
            conn.execute(
                """UPDATE sessions SET engineer_id = ?, topic_id = ?
                   WHERE topic_id = ? AND engineer_id IS NULL""",
                (engineer_id, new_id, old_id),
            )

        conn.commit()
        engineer_count = conn.execute("SELECT COUNT(*) FROM engineers").fetchone()[0]
        catalog_count = conn.execute("SELECT COUNT(*) FROM topic_catalog").fetchone()[0]
        progress_count = conn.execute("SELECT COUNT(*) FROM engineer_topic_progress").fetchone()[0]
        print(f"Migration applied to {db_path}: engineers={engineer_count} "
              f"topic_catalog={catalog_count} engineer_topic_progress={progress_count}")

    except sqlite3.Error as e:
        conn.rollback()
        print(f"Migration failed, rolled back: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--engineer-name", required=True)
    parser.add_argument("--platform", required=True, choices=["telegram", "slack"])
    parser.add_argument("--external-id", required=True)
    args = parser.parse_args()

    try:
        run_migration(args.db_path, args.engineer_name, args.platform, args.external_id)
    except (FileNotFoundError, sqlite3.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
