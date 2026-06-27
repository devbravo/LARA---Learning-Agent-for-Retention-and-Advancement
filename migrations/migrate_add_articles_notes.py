"""Migration: add sources, articles, and notes tables to LARA's SQLite database.

This migration is additive only. It does not modify, read from, or depend on
the existing `topics` or `sessions` tables. Safe to run multiple times
(uses CREATE TABLE IF NOT EXISTS).

Usage:
    python migrate_add_articles_notes.py --db-path /path/to/lara.db
"""

import argparse
import sqlite3
import sys
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT,
    site_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    user_interest TEXT NOT NULL DEFAULT 'pending',
    CHECK (user_interest IN ('pending', 'confirmed', 'declined'))
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
"""


def run_migration(db_path: str) -> None:
    """Apply the sources/articles/notes schema to the given SQLite database.

    Args:
        db_path: Path to the existing LARA SQLite database file. The file
            must already exist (this migration does not create a fresh
            LARA database from scratch — it only adds new tables to one
            that already has `topics`/`sessions`).

    Raises:
        FileNotFoundError: If db_path does not point to an existing file.
        sqlite3.Error: If table creation fails (e.g. malformed schema,
            permissions issue, disk full).
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Database not found at {db_path}. This migration adds tables "
            "to an existing LARA database — it does not create one from "
            "scratch. Point --db-path at your existing lara.db."
        )

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topics'"
        )
        if cursor.fetchone() is None:
            print(
                f"Warning: no 'topics' table found in {db_path}. "
                "Proceeding anyway, but confirm this is the right database file.",
                file=sys.stderr,
            )

        conn.executescript(SCHEMA)
        conn.commit()
        print(f"Migration applied successfully to {db_path}")

        for table in ("sources", "articles", "notes"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} rows")

    except sqlite3.Error as e:
        conn.rollback()
        print(f"Migration failed, rolled back: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to the existing LARA SQLite database file.",
    )
    args = parser.parse_args()

    try:
        run_migration(args.db_path)
    except (FileNotFoundError, sqlite3.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
