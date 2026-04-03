import sqlite3
from pathlib import Path
import yaml

DB_PATH = Path(__file__).parents[2] / "db" / "learning.db"
CONFIG_PATH = Path(__file__).parents[2] / "config.yaml"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        # Migrate existing DB: add active column if missing
        try:
            conn.execute("ALTER TABLE topics ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass  # column already exists

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                tier INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                easiness_factor REAL DEFAULT 2.5,
                interval_days INTEGER DEFAULT 1,
                repetitions INTEGER DEFAULT 0,
                next_review DATE NOT NULL DEFAULT (date('now')),
                weak_areas TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL REFERENCES topics(id),
                studied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                duration_min INTEGER,
                quality_score INTEGER CHECK(quality_score IN (2, 3, 5)),
                weak_areas TEXT,
                suggestions TEXT
            );
        """)


def seed_topics() -> None:
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    with get_connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO topics (name, tier) VALUES (:name, :tier)",
            config["topics"],
        )


if __name__ == "__main__":
    init_db()
    seed_topics()

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, tier, easiness_factor, interval_days, repetitions, next_review FROM topics ORDER BY tier, name"
        ).fetchall()

    print(f"{'ID':<4} {'Name':<25} {'Tier':<6} {'EF':<6} {'Interval':<10} {'Reps':<6} {'Next Review'}")
    print("-" * 70)
    for row in rows:
        print(f"{row['id']:<4} {row['name']:<25} {row['tier']:<6} {row['easiness_factor']:<6} {row['interval_days']:<10} {row['repetitions']:<6} {row['next_review']}")
