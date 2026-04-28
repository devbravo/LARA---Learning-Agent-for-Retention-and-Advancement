import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytz

from src.infrastructure import db as core_db
from src.infrastructure.time import _tz
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
                "SELECT id, duration_min, student_quality FROM sessions WHERE topic_id = ?",
                (topic_id,),
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration_min"], 60)
        self.assertEqual(rows[0]["student_quality"], 5)

    # ------------------------------------------------------------------
    # Legacy UTC row compat — day-boundary tests
    # ------------------------------------------------------------------

    def _insert_session_with_studied_at(self, topic_id: int, studied_at: str) -> int:
        """Insert a session row with an explicit studied_at value (bypasses repo)."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "INSERT INTO sessions (topic_id, duration_min, student_quality, studied_at) VALUES (?, ?, ?, ?)",
                (topic_id, 30, 3, studied_at),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def test_legacy_utc_row_is_found_by_today_helpers(self) -> None:
        """A legacy row stored as UTC timestamp (01:00 UTC = today local for UTC+X) is returned."""
        topic_id = self._insert_topic(name="Legacy UTC Topic")

        # Compute a UTC timestamp that falls inside today's local window.
        # local midnight in UTC + 1 hour is safely within "today local" for
        # any timezone east of UTC-01:00.
        tz = _tz()
        today_local_midnight = datetime.now(tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        utc_ts = (today_local_midnight + timedelta(hours=1)).astimezone(timezone.utc)
        studied_at_utc = utc_ts.strftime("%Y-%m-%d %H:%M:%S")

        self._insert_session_with_studied_at(topic_id, studied_at_utc)

        logged = session_repository.get_logged_topic_names_for_today()
        self.assertIn("Legacy UTC Topic", logged)

        session_id = session_repository.get_today_session_id(topic_id)
        self.assertIsNotNone(session_id)

    def test_legacy_utc_row_from_yesterday_is_not_found(self) -> None:
        """A UTC row whose local time is yesterday must not appear in today helpers.

        We compute "yesterday noon" in local time and convert to UTC for storage,
        ensuring the timestamp is unambiguously outside today's local window
        regardless of the current UTC/local clock relationship.
        """
        topic_id = self._insert_topic(name="Yesterday UTC Topic")

        tz = _tz()
        yesterday_local_noon = (
            datetime.now(tz) - timedelta(days=1)
        ).replace(hour=12, minute=0, second=0, microsecond=0)
        studied_at_utc = yesterday_local_noon.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        self._insert_session_with_studied_at(topic_id, studied_at_utc)

        logged = session_repository.get_logged_topic_names_for_today()
        self.assertNotIn("Yesterday UTC Topic", logged)

        session_id = session_repository.get_today_session_id(topic_id)
        self.assertIsNone(session_id)

    def test_upsert_does_not_duplicate_legacy_utc_row(self) -> None:
        """upsert_today_session must UPDATE a legacy UTC row, not INSERT a second one."""
        topic_id = self._insert_topic(name="No Duplicate")

        # Insert a legacy UTC row falling inside today local
        tz = _tz()
        today_local_midnight = datetime.now(tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        utc_ts = (today_local_midnight + timedelta(hours=1)).astimezone(timezone.utc)
        self._insert_session_with_studied_at(topic_id, utc_ts.strftime("%Y-%m-%d %H:%M:%S"))

        # Now upsert — should update, not insert
        session_repository.upsert_today_session(topic_id, 60, 5)

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, duration_min, student_quality FROM sessions WHERE topic_id = ?",
                (topic_id,),
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 1, "Expected exactly one row — legacy row should be updated, not duplicated")
        self.assertEqual(rows[0][1], 60)
        self.assertEqual(rows[0][2], 5)

    # ------------------------------------------------------------------
    # get_today_teacher_quality
    # ------------------------------------------------------------------

    def test_get_today_teacher_quality_returns_none_when_no_session(self) -> None:
        topic_id = self._insert_topic(name="No Session")
        result = session_repository.get_today_teacher_quality(topic_id)
        self.assertIsNone(result)

    def test_get_today_teacher_quality_returns_none_when_only_student_logged(self) -> None:
        topic_id = self._insert_topic(name="Student Only")
        session_repository.upsert_today_session(topic_id, 30, 3)
        result = session_repository.get_today_teacher_quality(topic_id)
        self.assertIsNone(result)

    def test_get_today_teacher_quality_returns_value_after_teacher_logs(self) -> None:
        topic_id = self._insert_topic(name="Teacher Logged")
        session_repository.log_teacher_session(topic_id, 2, {}, "claude", "mock")
        result = session_repository.get_today_teacher_quality(topic_id)
        self.assertEqual(result, 2)

    def test_get_today_teacher_quality_returns_value_when_both_logged(self) -> None:
        topic_id = self._insert_topic(name="Both Logged")
        session_repository.upsert_today_session(topic_id, 30, 5)
        session_repository.log_teacher_session(topic_id, 3, {}, "claude", "mock")
        result = session_repository.get_today_teacher_quality(topic_id)
        self.assertEqual(result, 3)

    # ------------------------------------------------------------------
    # calibration_gap written by both paths
    # ------------------------------------------------------------------

    def test_calibration_gap_set_when_student_logs_after_teacher(self) -> None:
        """upsert_today_session computes calibration_gap when teacher row exists."""
        topic_id = self._insert_topic(name="Teacher First")
        session_repository.log_teacher_session(topic_id, 3, {}, "claude", "mock")
        session_repository.upsert_today_session(topic_id, 30, 5)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT calibration_gap FROM sessions WHERE topic_id = ?", (topic_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["calibration_gap"], 2)  # 5 - 3

    def test_calibration_gap_set_when_teacher_logs_after_student(self) -> None:
        """log_teacher_session computes calibration_gap when student row exists."""
        topic_id = self._insert_topic(name="Student First")
        session_repository.upsert_today_session(topic_id, 30, 2)
        session_repository.log_teacher_session(topic_id, 5, {}, "claude", "mock")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT calibration_gap FROM sessions WHERE topic_id = ?", (topic_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["calibration_gap"], -3)  # 2 - 5


class Sm2RepositoryTests(RepositoryDbTestCase):
    def test_fetch_due_topics_filters_and_orders(self) -> None:
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._insert_topic(name="Tier2", tier=2, status="active", easiness_factor=2.5, next_review=today)
        self._insert_topic(name="Tier1Hard", tier=1, status="active", easiness_factor=1.5, next_review=today)
        self._insert_topic(name="Inactive", tier=1, status="inactive", next_review=today)
        self._insert_topic(name="Future", tier=1, status="active", next_review=tomorrow)

        rows = sm2_repository.fetch_due_topics(date.today())
        self.assertEqual([r["name"] for r in rows], ["Tier1Hard", "Tier2"])

    def test_fetch_and_update_sm2_state(self) -> None:
        topic_id = self._insert_topic(name="SM2", easiness_factor=2.3, interval_days=4, repetitions=2)

        state = sm2_repository.fetch_sm2_state(topic_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["interval_days"], 4)

        sm2_repository.update_sm2_state(
            topic_id=topic_id,
            easiness_factor=2.6,
            interval_days=6,
            repetitions=3,
            next_review=(date.today() + timedelta(days=6)).isoformat(),
        )

        state2 = sm2_repository.fetch_sm2_state(topic_id)
        self.assertEqual(state2["easiness_factor"], 2.6)
        self.assertEqual(state2["interval_days"], 6)
        self.assertEqual(state2["repetitions"], 3)

        self.assertIsNone(sm2_repository.fetch_sm2_state(99999))


if __name__ == "__main__":
    unittest.main()

