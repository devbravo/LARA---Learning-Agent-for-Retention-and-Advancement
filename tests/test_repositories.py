import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.core import db as core_db
from src.repositories import session_repository, sm2_repository, topic_repository


class RepositoryDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._create_schema(self.db_path)

        self._orig_db_path = core_db.DB_PATH
        core_db.DB_PATH = Path(self.db_path)

    def tearDown(self) -> None:
        core_db.DB_PATH = self._orig_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    @staticmethod
    def _create_schema(path: str) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    tier INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
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
                    quality_score INTEGER,
                    weak_areas TEXT,
                    suggestions TEXT
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_topic(self, **kwargs) -> int:
        defaults = {
            "name": "Topic",
            "tier": 1,
            "status": "active",
            "easiness_factor": 2.5,
            "interval_days": 1,
            "repetitions": 0,
            "next_review": date.today().isoformat(),
            "weak_areas": None,
        }
        defaults.update(kwargs)

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """
                INSERT INTO topics (name, tier, status, easiness_factor, interval_days, repetitions, next_review, weak_areas)
                VALUES (:name, :tier, :status, :easiness_factor, :interval_days, :repetitions, :next_review, :weak_areas)
                """,
                defaults,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


class TopicRepositoryTests(RepositoryDbTestCase):
    def test_topic_lookup_helpers(self) -> None:
        topic_id = self._insert_topic(name="LangGraph Core")

        self.assertEqual(topic_repository.get_topic_name_by_id(topic_id), "LangGraph Core")
        self.assertEqual(topic_repository.get_topic_id_by_name("langgraph core"), topic_id)
        self.assertIsNone(topic_repository.get_topic_id_by_name("missing"))

    def test_graduate_topic_to_active_resets_sm2_fields(self) -> None:
        topic_id = self._insert_topic(
            name="RAG",
            status="in_progress",
            repetitions=5,
            easiness_factor=1.7,
            interval_days=10,
        )

        updated = topic_repository.graduate_topic_to_active(topic_id)
        self.assertTrue(updated)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, repetitions, easiness_factor, next_review FROM topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["status"], "active")
        self.assertEqual(row["repetitions"], 0)
        self.assertEqual(row["easiness_factor"], 2.5)
        self.assertIsNotNone(row["next_review"])

    def test_in_progress_queries_and_set_in_progress(self) -> None:
        self._insert_topic(name="A", tier=2, status="in_progress")
        self._insert_topic(name="B", tier=1, status="in_progress")
        self._insert_topic(name="C", tier=1, status="inactive")

        topics = topic_repository.get_in_progress_topics()
        self.assertEqual([t["name"] for t in topics], ["B", "A"])
        self.assertEqual(topic_repository.get_in_progress_topic_names(), ["B", "A"])

        self.assertTrue(topic_repository.set_topic_in_progress("C"))
        self.assertFalse(topic_repository.set_topic_in_progress("C"))


class SessionRepositoryTests(RepositoryDbTestCase):
    def test_insert_and_read_session_helpers(self) -> None:
        topic_id = self._insert_topic(name="System Design")

        session_repository.insert_session(topic_id, 45, 3, "latency")
        logged = session_repository.get_logged_topic_names_for_today()
        self.assertEqual(logged, {"System Design"})

        session_id = session_repository.get_today_session_id(topic_id)
        self.assertIsNotNone(session_id)

        session_repository.update_session_weak_areas(session_id, "caching")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT weak_areas FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["weak_areas"], "caching")

    def test_upsert_today_session_updates_existing_row(self) -> None:
        topic_id = self._insert_topic(name="Agents")

        session_repository.upsert_today_session(topic_id, 30, 2)
        session_repository.upsert_today_session(topic_id, 60, 5)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, duration_min, quality_score FROM sessions WHERE topic_id = ?",
                (topic_id,),
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration_min"], 60)
        self.assertEqual(rows[0]["quality_score"], 5)


class Sm2RepositoryTests(RepositoryDbTestCase):
    def test_fetch_due_topics_filters_and_orders(self) -> None:
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._insert_topic(name="Tier2", tier=2, status="active", easiness_factor=2.5, next_review=today)
        self._insert_topic(name="Tier1Hard", tier=1, status="active", easiness_factor=1.5, next_review=today)
        self._insert_topic(name="Inactive", tier=1, status="inactive", next_review=today)
        self._insert_topic(name="Future", tier=1, status="active", next_review=tomorrow)

        rows = sm2_repository.fetch_due_topics(self.db_path, date.today())
        self.assertEqual([r["name"] for r in rows], ["Tier1Hard", "Tier2"])

    def test_fetch_and_update_sm2_state(self) -> None:
        topic_id = self._insert_topic(name="SM2", easiness_factor=2.3, interval_days=4, repetitions=2)

        state = sm2_repository.fetch_sm2_state(self.db_path, topic_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["interval_days"], 4)

        sm2_repository.update_sm2_state(
            path=self.db_path,
            topic_id=topic_id,
            easiness_factor=2.6,
            interval_days=6,
            repetitions=3,
            next_review=(date.today() + timedelta(days=6)).isoformat(),
        )

        state2 = sm2_repository.fetch_sm2_state(self.db_path, topic_id)
        self.assertEqual(state2["easiness_factor"], 2.6)
        self.assertEqual(state2["interval_days"], 6)
        self.assertEqual(state2["repetitions"], 3)

        self.assertIsNone(sm2_repository.fetch_sm2_state(self.db_path, 99999))


if __name__ == "__main__":
    unittest.main()

