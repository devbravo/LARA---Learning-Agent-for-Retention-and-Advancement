import os
import sqlite3
import tempfile
import unittest

from migrations.migrate_multi_user import run_migration


class MigrateMultiUserTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                tier INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                topic_type TEXT NOT NULL DEFAULT 'conceptual',
                default_duration_minutes INTEGER NOT NULL DEFAULT 30,
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
                mode TEXT,
                quality_score INTEGER,
                weak_areas TEXT,
                suggestions TEXT,
                student_quality INTEGER,
                student_weak_areas TEXT,
                teacher_quality INTEGER,
                teacher_weak_areas TEXT,
                teacher_source TEXT,
                calibration_gap INTEGER
            );
            INSERT INTO topics (name, tier, status, easiness_factor, interval_days, repetitions, next_review, weak_areas)
            VALUES ('RAG - Vector Databases', 1, 'active', 2.6, 4, 2, '2026-08-15', 'confuses HNSW with IVF');
            INSERT INTO sessions (topic_id, duration_min, student_quality, studied_at)
            VALUES (1, 30, 3, '2026-08-10 14:00:00');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_migration_creates_engineer_and_preserves_data(self) -> None:
        run_migration(self.db_path, engineer_name="Diego Sabajo", platform="telegram", external_id="123456")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            engineer = conn.execute("SELECT * FROM engineers").fetchone()
            self.assertEqual(engineer["name"], "Diego Sabajo")
            self.assertEqual(engineer["platform"], "telegram")
            self.assertEqual(engineer["external_id"], "123456")

            catalog_row = conn.execute(
                "SELECT * FROM topic_catalog WHERE name = 'RAG - Vector Databases'"
            ).fetchone()
            self.assertIsNotNone(catalog_row)
            self.assertEqual(catalog_row["tier"], 1)

            progress_row = conn.execute(
                """SELECT * FROM engineer_topic_progress
                   WHERE engineer_id = ? AND topic_id = ?""",
                (engineer["id"], catalog_row["id"]),
            ).fetchone()
            self.assertIsNotNone(progress_row)
            self.assertEqual(progress_row["status"], "active")
            self.assertAlmostEqual(progress_row["easiness_factor"], 2.6)
            self.assertEqual(progress_row["weak_areas"], "confuses HNSW with IVF")

            session_row = conn.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(session_row["engineer_id"], engineer["id"])
            self.assertEqual(session_row["topic_id"], catalog_row["id"])
        finally:
            conn.close()

    def test_migration_is_idempotent(self) -> None:
        run_migration(self.db_path, engineer_name="Diego Sabajo", platform="telegram", external_id="123456")
        run_migration(self.db_path, engineer_name="Diego Sabajo", platform="telegram", external_id="123456")

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM engineers").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
