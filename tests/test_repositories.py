import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
                    engineer_id INTEGER,
                    topic_id INTEGER NOT NULL,
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

                CREATE TABLE engineers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL CHECK(platform IN ('telegram', 'slack')),
                    external_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
            "topic_type": "conceptual",
            "default_duration_minutes": 30,
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
                INSERT INTO topics (name, tier, status, topic_type, default_duration_minutes,
                                    easiness_factor, interval_days, repetitions, next_review, weak_areas)
                VALUES (:name, :tier, :status, :topic_type, :default_duration_minutes,
                        :easiness_factor, :interval_days, :repetitions, :next_review, :weak_areas)
                """,
                defaults,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def _insert_engineer(self, **kwargs) -> int:
        defaults = {"name": "Diego", "platform": "telegram", "external_id": "1"}
        defaults.update(kwargs)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "INSERT INTO engineers (name, platform, external_id) VALUES (:name, :platform, :external_id)",
                defaults,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def _insert_catalog_topic(self, **kwargs) -> int:
        defaults = {
            "name": "Topic",
            "tier": 1,
            "topic_type": "conceptual",
            "default_duration_minutes": 30,
        }
        defaults.update(kwargs)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """INSERT INTO topic_catalog (name, tier, topic_type, default_duration_minutes)
                   VALUES (:name, :tier, :topic_type, :default_duration_minutes)""",
                defaults,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def _insert_progress(self, engineer_id: int, topic_id: int, **kwargs) -> int:
        defaults = {
            "status": "active",
            "easiness_factor": 2.5,
            "interval_days": 1,
            "repetitions": 0,
            "next_review": date.today().isoformat(),
            "weak_areas": None,
        }
        defaults.update(kwargs)
        defaults["engineer_id"] = engineer_id
        defaults["topic_id"] = topic_id
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """INSERT INTO engineer_topic_progress
                       (engineer_id, topic_id, status, easiness_factor, interval_days, repetitions, next_review, weak_areas)
                   VALUES (:engineer_id, :topic_id, :status, :easiness_factor, :interval_days, :repetitions, :next_review, :weak_areas)""",
                defaults,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


class TopicRepositoryTests(RepositoryDbTestCase):
    # ------------------------------------------------------------------
    # Catalog-only lookups — no engineer_id, query topic_catalog directly
    # ------------------------------------------------------------------

    def test_topic_lookup_helpers_are_catalog_only(self) -> None:
        topic_id = self._insert_catalog_topic(name="LangGraph Core")

        self.assertEqual(topic_repository.get_topic_name_by_id(topic_id), "LangGraph Core")
        self.assertEqual(topic_repository.get_topic_id_by_name("langgraph core"), topic_id)
        self.assertIsNone(topic_repository.get_topic_id_by_name("missing"))

    def test_get_default_duration_returns_seeded_value(self) -> None:
        self._insert_catalog_topic(name="DSA - Trees", default_duration_minutes=45)
        result = topic_repository.get_default_duration_by_name("DSA - Trees")
        self.assertEqual(result, 45)

    def test_get_default_duration_is_case_insensitive(self) -> None:
        self._insert_catalog_topic(name="DSA - Trees", default_duration_minutes=45)
        result = topic_repository.get_default_duration_by_name("dsa - trees")
        self.assertEqual(result, 45)

    def test_get_default_duration_returns_30_for_unknown_topic(self) -> None:
        result = topic_repository.get_default_duration_by_name("Nonexistent Topic")
        self.assertEqual(result, 30)

    def test_get_topic_type_by_id_is_catalog_only(self) -> None:
        topic_id = self._insert_catalog_topic(name="RAG", topic_type="dsa")
        self.assertEqual(topic_repository.get_topic_type_by_id(topic_id), "dsa")
        self.assertIsNone(topic_repository.get_topic_type_by_id(99999))

    # ------------------------------------------------------------------
    # graduate_topic_to_active / activate_topic_from_discuss — engineer-scoped
    # ------------------------------------------------------------------

    def test_graduate_topic_to_active_resets_sm2_fields(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(
            engineer_id, topic_id,
            status="in_progress", repetitions=5, easiness_factor=1.7, interval_days=10,
        )

        updated = topic_repository.graduate_topic_to_active(engineer_id, topic_id)
        self.assertTrue(updated)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT status, repetitions, easiness_factor, next_review
                   FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?""",
                (engineer_id, topic_id),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["status"], "active")
        self.assertEqual(row["repetitions"], 0)
        self.assertEqual(row["easiness_factor"], 2.5)
        self.assertIsNotNone(row["next_review"])

    def test_graduate_topic_to_active_returns_false_when_no_progress_row(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        # No progress row inserted — lazy row never created for this engineer.
        self.assertFalse(topic_repository.graduate_topic_to_active(engineer_id, topic_id))

    def test_graduate_topic_to_active_only_affects_target_engineer(self) -> None:
        engineer_a = self._insert_engineer(external_id="a")
        engineer_b = self._insert_engineer(external_id="b")
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_a, topic_id, status="in_progress")
        self._insert_progress(engineer_b, topic_id, status="in_progress")

        topic_repository.graduate_topic_to_active(engineer_a, topic_id)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row_b = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_b, topic_id),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row_b["status"], "in_progress")

    def test_activate_topic_from_discuss_sets_next_review_today(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_id, topic_id, status="discussing")

        updated = topic_repository.activate_topic_from_discuss(engineer_id, topic_id)
        self.assertTrue(updated)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, next_review FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, topic_id),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["next_review"], date.today().isoformat())

    # ------------------------------------------------------------------
    # get_topic_status_by_id — lazy-row semantics
    # ------------------------------------------------------------------

    def test_get_topic_status_by_id_returns_inactive_for_lazy_missing_row(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        # No progress row — engineer has never touched this topic.
        self.assertEqual(topic_repository.get_topic_status_by_id(engineer_id, topic_id), "inactive")

    def test_get_topic_status_by_id_returns_existing_status(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_id, topic_id, status="discussing")
        self.assertEqual(topic_repository.get_topic_status_by_id(engineer_id, topic_id), "discussing")

    def test_get_topic_status_by_id_returns_none_for_unknown_topic(self) -> None:
        engineer_id = self._insert_engineer()
        self.assertIsNone(topic_repository.get_topic_status_by_id(engineer_id, 99999))

    # ------------------------------------------------------------------
    # get_in_progress_topics / get_in_progress_topic_names / set_topic_in_progress
    # ------------------------------------------------------------------

    def test_in_progress_queries_and_set_in_progress(self) -> None:
        engineer_id = self._insert_engineer()
        topic_a = self._insert_catalog_topic(name="A", tier=2)
        topic_b = self._insert_catalog_topic(name="B", tier=1)
        self._insert_catalog_topic(name="C", tier=1)  # no progress row — lazy inactive
        self._insert_progress(engineer_id, topic_a, status="in_progress")
        self._insert_progress(engineer_id, topic_b, status="in_progress")

        topics = topic_repository.get_in_progress_topics(engineer_id)
        self.assertEqual([t["name"] for t in topics], ["B", "A"])
        self.assertEqual(topic_repository.get_in_progress_topic_names(engineer_id), ["B", "A"])

        self.assertTrue(topic_repository.set_topic_in_progress(engineer_id, "C"))
        self.assertFalse(topic_repository.set_topic_in_progress(engineer_id, "C"))

    def test_set_topic_in_progress_creates_lazy_row_when_none_exists(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_catalog_topic(name="C", tier=1)

        self.assertTrue(topic_repository.set_topic_in_progress(engineer_id, "C"))

        self.assertEqual(topic_repository.get_in_progress_topic_names(engineer_id), ["C"])

    def test_set_topic_in_progress_does_not_overwrite_active_status(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="C", tier=1)
        self._insert_progress(engineer_id, topic_id, status="active")

        self.assertFalse(topic_repository.set_topic_in_progress(engineer_id, "C"))

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, topic_id),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "active")

    def test_set_topic_in_progress_returns_false_for_unknown_topic_name(self) -> None:
        engineer_id = self._insert_engineer()
        self.assertFalse(topic_repository.set_topic_in_progress(engineer_id, "Nonexistent"))

    def test_set_topic_in_progress_only_affects_target_engineer(self) -> None:
        engineer_a = self._insert_engineer(external_id="a")
        engineer_b = self._insert_engineer(external_id="b")
        self._insert_catalog_topic(name="C", tier=1)

        topic_repository.set_topic_in_progress(engineer_a, "C")

        self.assertEqual(topic_repository.get_in_progress_topic_names(engineer_a), ["C"])
        self.assertEqual(topic_repository.get_in_progress_topic_names(engineer_b), [])

    # ------------------------------------------------------------------
    # get_topic_weak_areas_by_name / update_topic_weak_areas
    # ------------------------------------------------------------------

    def test_get_topic_weak_areas_by_name_returns_engineer_scoped_value(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_id, topic_id, weak_areas="confuses HNSW with IVF")

        result = topic_repository.get_topic_weak_areas_by_name(engineer_id, "rag")
        self.assertEqual(result, "confuses HNSW with IVF")

    def test_get_topic_weak_areas_by_name_returns_none_when_empty(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_id, topic_id, weak_areas=None)

        self.assertIsNone(topic_repository.get_topic_weak_areas_by_name(engineer_id, "RAG"))

    def test_update_topic_weak_areas_sets_value(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_id, topic_id, weak_areas=None)

        topic_repository.update_topic_weak_areas(engineer_id, topic_id, "new gap")

        self.assertEqual(topic_repository.get_topic_weak_areas_by_name(engineer_id, "RAG"), "new gap")

    def test_update_topic_weak_areas_clears_with_none(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_id, topic_id, weak_areas="old gap")

        topic_repository.update_topic_weak_areas(engineer_id, topic_id, None)

        self.assertIsNone(topic_repository.get_topic_weak_areas_by_name(engineer_id, "RAG"))

    # ------------------------------------------------------------------
    # get_inactive_topics_tier1_or2 — lazy-row LEFT JOIN semantics
    # ------------------------------------------------------------------

    def test_get_inactive_topics_includes_lazy_missing_rows(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_catalog_topic(name="Never Touched", tier=1)

        names = [t["name"] for t in topic_repository.get_inactive_topics_tier1_or2(engineer_id)]
        self.assertIn("Never Touched", names)

    def test_get_inactive_topics_excludes_active_and_in_progress(self) -> None:
        engineer_id = self._insert_engineer()
        active_id = self._insert_catalog_topic(name="Active", tier=1)
        ip_id = self._insert_catalog_topic(name="InProgress", tier=1)
        self._insert_progress(engineer_id, active_id, status="active")
        self._insert_progress(engineer_id, ip_id, status="in_progress")

        names = [t["name"] for t in topic_repository.get_inactive_topics_tier1_or2(engineer_id)]
        self.assertNotIn("Active", names)
        self.assertNotIn("InProgress", names)

    def test_get_inactive_topics_excludes_tier3(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_catalog_topic(name="Advanced", tier=3)

        names = [t["name"] for t in topic_repository.get_inactive_topics_tier1_or2(engineer_id)]
        self.assertNotIn("Advanced", names)

    # ------------------------------------------------------------------
    # fetch_overdue_topics / fetch_due_today_topics / fetch_in_progress_topics_with_weak_areas
    # ------------------------------------------------------------------

    def test_fetch_overdue_topics_returns_only_past_due_active(self) -> None:
        engineer_id = self._insert_engineer()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        overdue_id = self._insert_catalog_topic(name="Overdue")
        current_id = self._insert_catalog_topic(name="Current")
        self._insert_progress(engineer_id, overdue_id, status="active", next_review=yesterday, weak_areas="gap")
        self._insert_progress(engineer_id, current_id, status="active", next_review=today)

        rows = topic_repository.fetch_overdue_topics(engineer_id, today)
        self.assertEqual([r["name"] for r in rows], ["Overdue"])
        self.assertEqual(rows[0]["weak_areas"], "gap")

    def test_fetch_due_today_topics_orders_by_tier_then_easiness(self) -> None:
        engineer_id = self._insert_engineer()
        today = date.today().isoformat()
        hard_id = self._insert_catalog_topic(name="Hard", tier=1)
        easy_id = self._insert_catalog_topic(name="Easy", tier=1)
        self._insert_progress(engineer_id, hard_id, status="active", next_review=today, easiness_factor=1.5)
        self._insert_progress(engineer_id, easy_id, status="active", next_review=today, easiness_factor=2.8)

        rows = topic_repository.fetch_due_today_topics(engineer_id, today)
        self.assertEqual([r["name"] for r in rows], ["Hard", "Easy"])

    def test_fetch_in_progress_with_weak_areas_includes_discussing(self) -> None:
        engineer_id = self._insert_engineer()
        ip_id = self._insert_catalog_topic(name="IP")
        dis_id = self._insert_catalog_topic(name="DIS")
        act_id = self._insert_catalog_topic(name="ACT")
        self._insert_progress(engineer_id, ip_id, status="in_progress", weak_areas="area1")
        self._insert_progress(engineer_id, dis_id, status="discussing", weak_areas="area2")
        self._insert_progress(engineer_id, act_id, status="active", weak_areas="area3")

        names = [t["name"] for t in topic_repository.fetch_in_progress_topics_with_weak_areas(engineer_id)]
        self.assertIn("IP", names)
        self.assertIn("DIS", names)
        self.assertNotIn("ACT", names)

    # ------------------------------------------------------------------
    # get_topic_context — LEFT JOIN progress + engineer-scoped sessions
    # ------------------------------------------------------------------

    def test_get_topic_context_returns_progress_and_last_session(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="RAG", topic_type="conceptual")
        self._insert_progress(engineer_id, topic_id, easiness_factor=2.3, weak_areas="gap")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO sessions (engineer_id, topic_id, student_quality, studied_at) VALUES (?, ?, ?, ?)",
            (engineer_id, topic_id, 3, "2026-01-01 10:00:00"),
        )
        conn.commit()
        conn.close()

        ctx = topic_repository.get_topic_context(engineer_id, topic_id)
        self.assertEqual(ctx["name"], "RAG")
        self.assertEqual(ctx["easiness_factor"], 2.3)
        self.assertEqual(ctx["weak_areas"], "gap")
        self.assertEqual(ctx["student_quality"], 3)

    def test_get_topic_context_defaults_when_no_progress_row(self) -> None:
        """Lazy-row semantics: a never-touched topic still returns catalog fields with SM-2 defaults."""
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="Never Touched")

        ctx = topic_repository.get_topic_context(engineer_id, topic_id)
        self.assertEqual(ctx["name"], "Never Touched")
        self.assertEqual(ctx["easiness_factor"], 2.5)
        self.assertEqual(ctx["repetitions"], 0)
        self.assertIsNone(ctx["student_quality"])

    def test_get_topic_context_returns_empty_dict_for_unknown_topic(self) -> None:
        engineer_id = self._insert_engineer()
        self.assertEqual(topic_repository.get_topic_context(engineer_id, 99999), {})

    def test_get_topic_context_does_not_leak_other_engineers_sessions(self) -> None:
        engineer_a = self._insert_engineer(external_id="a")
        engineer_b = self._insert_engineer(external_id="b")
        topic_id = self._insert_catalog_topic(name="RAG")
        self._insert_progress(engineer_a, topic_id)
        self._insert_progress(engineer_b, topic_id)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO sessions (engineer_id, topic_id, student_quality, studied_at) VALUES (?, ?, ?, ?)",
            (engineer_b, topic_id, 5, "2026-01-01 10:00:00"),
        )
        conn.commit()
        conn.close()

        ctx = topic_repository.get_topic_context(engineer_a, topic_id)
        self.assertIsNone(ctx["student_quality"])

    # ------------------------------------------------------------------
    # get_active_unlogged_topics_today
    # ------------------------------------------------------------------

    def test_get_active_unlogged_topics_today_excludes_logged(self) -> None:
        engineer_id = self._insert_engineer()
        logged_id = self._insert_catalog_topic(name="Logged")
        unlogged_id = self._insert_catalog_topic(name="Unlogged")
        self._insert_progress(engineer_id, logged_id, status="active")
        self._insert_progress(engineer_id, unlogged_id, status="active")

        with patch.object(
            topic_repository.session_repository,
            "get_logged_topic_names_for_today",
            return_value={"Logged"},
        ):
            result = topic_repository.get_active_unlogged_topics_today(engineer_id)

        names = [t["name"] for t in result]
        self.assertEqual(names, ["Unlogged"])


class SessionRepositoryTests(RepositoryDbTestCase):
    def test_insert_and_read_session_helpers(self) -> None:
        topic_id = self._insert_catalog_topic(name="System Design")

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
        topic_id = self._insert_catalog_topic(name="Legacy UTC Topic")

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
        topic_id = self._insert_catalog_topic(name="Yesterday UTC Topic")

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
        engineer_id = self._insert_engineer()
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        tier2 = self._insert_catalog_topic(name="Tier2", tier=2)
        tier1_hard = self._insert_catalog_topic(name="Tier1Hard", tier=1)
        inactive = self._insert_catalog_topic(name="Inactive", tier=1)
        future = self._insert_catalog_topic(name="Future", tier=1)
        self._insert_progress(engineer_id, tier2, status="active", easiness_factor=2.5, next_review=today)
        self._insert_progress(engineer_id, tier1_hard, status="active", easiness_factor=1.5, next_review=today)
        self._insert_progress(engineer_id, inactive, status="inactive", next_review=today)
        self._insert_progress(engineer_id, future, status="active", next_review=tomorrow)

        rows = sm2_repository.fetch_due_topics(engineer_id, date.today())
        self.assertEqual([r["name"] for r in rows], ["Tier1Hard", "Tier2"])

    def test_fetch_due_topics_only_returns_target_engineer(self) -> None:
        engineer_a = self._insert_engineer(external_id="a")
        engineer_b = self._insert_engineer(external_id="b")
        today = date.today().isoformat()
        topic_id = self._insert_catalog_topic(name="Shared", tier=1)
        self._insert_progress(engineer_a, topic_id, status="active", next_review=today)
        self._insert_progress(engineer_b, topic_id, status="active", next_review=today)

        rows_a = sm2_repository.fetch_due_topics(engineer_a, date.today())
        self.assertEqual(len(rows_a), 1)

        # Deactivating engineer B's progress must not affect engineer A's due list.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE engineer_topic_progress SET status = 'inactive' WHERE engineer_id = ?",
                (engineer_b,),
            )
        rows_a_after = sm2_repository.fetch_due_topics(engineer_a, date.today())
        self.assertEqual(len(rows_a_after), 1)

    def test_fetch_and_update_sm2_state(self) -> None:
        # Two throwaway engineers but only one throwaway topic inserted first,
        # so the two AUTOINCREMENT sequences diverge and engineer_id/topic_id
        # never coincidentally share a value — otherwise a transposed
        # WHERE clause (engineer_id <-> topic_id swapped) would pass unnoticed.
        self._insert_engineer(external_id="throwaway1")
        self._insert_engineer(external_id="throwaway2")
        self._insert_catalog_topic(name="Throwaway")
        engineer_id = self._insert_engineer(external_id="real")
        topic_id = self._insert_catalog_topic(name="SM2")
        self.assertNotEqual(engineer_id, topic_id)
        self._insert_progress(engineer_id, topic_id, easiness_factor=2.3, interval_days=4, repetitions=2)

        state = sm2_repository.fetch_sm2_state(engineer_id, topic_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["interval_days"], 4)

        sm2_repository.update_sm2_state(
            engineer_id=engineer_id,
            topic_id=topic_id,
            easiness_factor=2.6,
            interval_days=6,
            repetitions=3,
            next_review=(date.today() + timedelta(days=6)).isoformat(),
        )

        state2 = sm2_repository.fetch_sm2_state(engineer_id, topic_id)
        self.assertEqual(state2["easiness_factor"], 2.6)
        self.assertEqual(state2["interval_days"], 6)
        self.assertEqual(state2["repetitions"], 3)

        self.assertIsNone(sm2_repository.fetch_sm2_state(engineer_id, 99999))

    def test_fetch_sm2_state_returns_none_for_lazy_missing_row(self) -> None:
        engineer_id = self._insert_engineer()
        topic_id = self._insert_catalog_topic(name="Never Touched")
        # No progress row inserted — lazy row semantics.
        self.assertIsNone(sm2_repository.fetch_sm2_state(engineer_id, topic_id))

    def test_update_sm2_state_only_affects_target_engineer(self) -> None:
        engineer_a = self._insert_engineer(external_id="a")
        engineer_b = self._insert_engineer(external_id="b")
        topic_id = self._insert_catalog_topic(name="Shared")
        # A second, untouched topic for engineer A pins the topic_id
        # predicate — if it were dropped from the UPDATE, this row would
        # get overwritten too, since it belongs to the same engineer_id.
        other_topic_id = self._insert_catalog_topic(name="Untouched")
        self._insert_progress(engineer_a, topic_id, easiness_factor=2.5)
        self._insert_progress(engineer_a, other_topic_id, easiness_factor=2.5)
        self._insert_progress(engineer_b, topic_id, easiness_factor=2.5)

        sm2_repository.update_sm2_state(
            engineer_id=engineer_a,
            topic_id=topic_id,
            easiness_factor=1.8,
            interval_days=1,
            repetitions=0,
            next_review=date.today().isoformat(),
        )

        state_a = sm2_repository.fetch_sm2_state(engineer_a, topic_id)
        self.assertEqual(state_a["easiness_factor"], 1.8)

        state_a_other = sm2_repository.fetch_sm2_state(engineer_a, other_topic_id)
        self.assertEqual(state_a_other["easiness_factor"], 2.5)

        state_b = sm2_repository.fetch_sm2_state(engineer_b, topic_id)
        self.assertEqual(state_b["easiness_factor"], 2.5)


class DiscussingTopicRepositoryTests(RepositoryDbTestCase):
    """Tests for the discussing-status topic repository functions (engineer-scoped)."""

    # ------------------------------------------------------------------
    # get_discussing_topics
    # ------------------------------------------------------------------

    def test_get_discussing_topics_returns_discussing_only(self) -> None:
        engineer_id = self._insert_engineer()
        d1 = self._insert_catalog_topic(name="D1", tier=1)
        d2 = self._insert_catalog_topic(name="D2", tier=2)
        active_id = self._insert_catalog_topic(name="Active", tier=1)
        ip_id = self._insert_catalog_topic(name="InProgress", tier=1)
        self._insert_progress(engineer_id, d1, status="discussing")
        self._insert_progress(engineer_id, d2, status="discussing")
        self._insert_progress(engineer_id, active_id, status="active")
        self._insert_progress(engineer_id, ip_id, status="in_progress")

        result = topic_repository.get_discussing_topics(engineer_id)
        names = [t["name"] for t in result]
        self.assertIn("D1", names)
        self.assertIn("D2", names)
        self.assertNotIn("Active", names)
        self.assertNotIn("InProgress", names)

    def test_get_discussing_topics_empty_when_none(self) -> None:
        engineer_id = self._insert_engineer()
        active_id = self._insert_catalog_topic(name="Active", tier=1)
        self._insert_progress(engineer_id, active_id, status="active")
        self.assertEqual(topic_repository.get_discussing_topics(engineer_id), [])

    def test_get_discussing_topics_ordered_by_tier_then_name(self) -> None:
        engineer_id = self._insert_engineer()
        z_id = self._insert_catalog_topic(name="Z", tier=1)
        a_id = self._insert_catalog_topic(name="A", tier=2)
        m_id = self._insert_catalog_topic(name="M", tier=1)
        for tid in (z_id, a_id, m_id):
            self._insert_progress(engineer_id, tid, status="discussing")

        names = [t["name"] for t in topic_repository.get_discussing_topics(engineer_id)]
        self.assertEqual(names, ["M", "Z", "A"])

    def test_get_discussing_topics_returns_id_and_name(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="D", tier=1)
        self._insert_progress(engineer_id, tid, status="discussing")

        result = topic_repository.get_discussing_topics(engineer_id)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], tid)
        self.assertEqual(result[0]["name"], "D")

    def test_get_discussing_topics_only_returns_target_engineer(self) -> None:
        engineer_a = self._insert_engineer(external_id="a")
        engineer_b = self._insert_engineer(external_id="b")
        tid = self._insert_catalog_topic(name="D", tier=1)
        self._insert_progress(engineer_b, tid, status="discussing")

        self.assertEqual(topic_repository.get_discussing_topics(engineer_a), [])

    # ------------------------------------------------------------------
    # set_topic_discussing
    # ------------------------------------------------------------------

    def test_set_topic_discussing_changes_status(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="T", tier=1)
        self._insert_progress(engineer_id, tid, status="in_progress")

        topic_repository.set_topic_discussing(engineer_id, tid)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, tid),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "discussing")

    def test_set_topic_discussing_nonexistent_id_is_noop(self) -> None:
        engineer_id = self._insert_engineer()
        # Should not raise even when the id doesn't exist
        topic_repository.set_topic_discussing(engineer_id, 99999)

    def test_set_topic_discussing_from_active_status(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="ActiveTopic", tier=1)
        self._insert_progress(engineer_id, tid, status="active")

        topic_repository.set_topic_discussing(engineer_id, tid)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, tid),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "discussing")

    # ------------------------------------------------------------------
    # set_topic_back_to_in_progress
    # ------------------------------------------------------------------

    def test_set_topic_back_to_in_progress_changes_status(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="T", tier=1)
        self._insert_progress(engineer_id, tid, status="discussing")

        topic_repository.set_topic_back_to_in_progress(engineer_id, tid)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, tid),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "in_progress")

    def test_set_topic_back_to_in_progress_returns_true_on_success(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="T2", tier=1)
        self._insert_progress(engineer_id, tid, status="discussing")
        self.assertTrue(topic_repository.set_topic_back_to_in_progress(engineer_id, tid))

    def test_set_topic_back_to_in_progress_returns_false_for_nonexistent_id(self) -> None:
        engineer_id = self._insert_engineer()
        self.assertFalse(topic_repository.set_topic_back_to_in_progress(engineer_id, 99999))

    def test_set_topic_back_to_in_progress_does_not_overwrite_active_status(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="ActiveTopic", tier=1)
        self._insert_progress(engineer_id, tid, status="active")

        result = topic_repository.set_topic_back_to_in_progress(engineer_id, tid)
        self.assertFalse(result)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, tid),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "active")

    def test_set_topic_back_to_in_progress_does_not_overwrite_in_progress_status(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="IPTopic", tier=1)
        self._insert_progress(engineer_id, tid, status="in_progress")

        result = topic_repository.set_topic_back_to_in_progress(engineer_id, tid)
        self.assertFalse(result)

    def test_set_then_restore_roundtrip(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_catalog_topic(name="RT", tier=1)
        self._insert_progress(engineer_id, tid, status="in_progress")

        topic_repository.set_topic_discussing(engineer_id, tid)
        topic_repository.set_topic_back_to_in_progress(engineer_id, tid)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, tid),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "in_progress")

    # ------------------------------------------------------------------
    # get_in_progress_and_active_topics
    # ------------------------------------------------------------------

    def test_get_in_progress_and_active_topics_includes_both_statuses(self) -> None:
        engineer_id = self._insert_engineer()
        active_id = self._insert_catalog_topic(name="Active", tier=1)
        ip_id = self._insert_catalog_topic(name="InProgress", tier=1)
        self._insert_catalog_topic(name="Inactive", tier=1)  # lazy, no progress row
        dis_id = self._insert_catalog_topic(name="Discussing", tier=1)
        self._insert_progress(engineer_id, active_id, status="active")
        self._insert_progress(engineer_id, ip_id, status="in_progress")
        self._insert_progress(engineer_id, dis_id, status="discussing")

        names = [t["name"] for t in topic_repository.get_in_progress_and_active_topics(engineer_id)]
        self.assertIn("Active", names)
        self.assertIn("InProgress", names)
        self.assertNotIn("Inactive", names)
        self.assertNotIn("Discussing", names)

    def test_get_in_progress_and_active_topics_empty_when_none(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_catalog_topic(name="Inactive", tier=1)
        self.assertEqual(topic_repository.get_in_progress_and_active_topics(engineer_id), [])

    def test_get_in_progress_and_active_topics_ordered_by_tier_then_name(self) -> None:
        engineer_id = self._insert_engineer()
        z_id = self._insert_catalog_topic(name="Z", tier=1)
        a_id = self._insert_catalog_topic(name="A", tier=2)
        m_id = self._insert_catalog_topic(name="M", tier=1)
        self._insert_progress(engineer_id, z_id, status="active")
        self._insert_progress(engineer_id, a_id, status="in_progress")
        self._insert_progress(engineer_id, m_id, status="in_progress")

        names = [t["name"] for t in topic_repository.get_in_progress_and_active_topics(engineer_id)]
        self.assertEqual(names, ["M", "Z", "A"])


class DiscussSessionRepositoryTests(RepositoryDbTestCase):
    """Tests for discuss-mode session repository functions."""

    def _insert_session_raw(self, topic_id: int, **kwargs) -> int:
        """Bypass the repository and insert a session row directly."""
        fields = {"topic_id": topic_id, "mode": None, "teacher_quality": None,
                  "teacher_weak_areas": None, "weak_areas": None,
                  "student_quality": None, "quality_score": None,
                  "studied_at": "2025-01-01 10:00:00"}
        fields.update(kwargs)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """INSERT INTO sessions
                       (topic_id, mode, teacher_quality, teacher_weak_areas,
                        weak_areas, student_quality, quality_score, studied_at)
                   VALUES (:topic_id, :mode, :teacher_quality, :teacher_weak_areas,
                           :weak_areas, :student_quality, :quality_score, :studied_at)""",
                fields,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # insert_discuss_session
    # ------------------------------------------------------------------

    def test_insert_discuss_session_stores_all_fields(self) -> None:
        tid = self._insert_topic(name="T")
        session_repository.insert_discuss_session(tid, 3, '{"gap": "weak"}')

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM sessions WHERE topic_id = ?", (tid,)).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["mode"], "discuss")
        self.assertEqual(row["teacher_quality"], 3)
        self.assertEqual(row["teacher_weak_areas"], '{"gap": "weak"}')
        self.assertIsNotNone(row["studied_at"])

    def test_insert_discuss_session_does_not_set_student_quality(self) -> None:
        tid = self._insert_topic(name="T")
        session_repository.insert_discuss_session(tid, 5, "{}")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT student_quality FROM sessions WHERE topic_id = ?", (tid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["student_quality"])

    # ------------------------------------------------------------------
    # get_discuss_session_count
    # ------------------------------------------------------------------

    def test_get_discuss_session_count_returns_zero_when_no_sessions(self) -> None:
        tid = self._insert_topic(name="T")
        self.assertEqual(session_repository.get_discuss_session_count(tid), 0)

    def test_get_discuss_session_count_returns_zero_for_nonexistent_topic(self) -> None:
        self.assertEqual(session_repository.get_discuss_session_count(99999), 0)

    def test_get_discuss_session_count_counts_only_discuss_mode(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="discuss")
        self._insert_session_raw(tid, mode="discuss")
        self._insert_session_raw(tid, mode="mock")
        self._insert_session_raw(tid, mode=None)

        self.assertEqual(session_repository.get_discuss_session_count(tid), 2)

    def test_get_discuss_session_count_correct_across_topics(self) -> None:
        t1 = self._insert_topic(name="T1")
        t2 = self._insert_topic(name="T2")
        self._insert_session_raw(t1, mode="discuss")
        self._insert_session_raw(t2, mode="discuss")
        self._insert_session_raw(t2, mode="discuss")

        self.assertEqual(session_repository.get_discuss_session_count(t1), 1)
        self.assertEqual(session_repository.get_discuss_session_count(t2), 2)

    # ------------------------------------------------------------------
    # get_discuss_sessions
    # ------------------------------------------------------------------

    def test_get_discuss_sessions_returns_empty_when_none(self) -> None:
        tid = self._insert_topic(name="T")
        self.assertEqual(session_repository.get_discuss_sessions(tid), [])

    def test_get_discuss_sessions_excludes_mock_and_null_mode(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="mock", teacher_quality=5)
        self._insert_session_raw(tid, mode=None, teacher_quality=3)

        self.assertEqual(session_repository.get_discuss_sessions(tid), [])

    def test_get_discuss_sessions_returns_correct_fields(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="discuss", teacher_quality=3,
                                  teacher_weak_areas='{"x": "y"}', studied_at="2025-06-01 10:00:00")

        rows = session_repository.get_discuss_sessions(tid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["teacher_quality"], 3)
        self.assertEqual(rows[0]["teacher_weak_areas"], '{"x": "y"}')
        self.assertEqual(rows[0]["studied_at"], "2025-06-01 10:00:00")

    def test_get_discuss_sessions_ordered_most_recent_first(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="discuss", teacher_quality=2, studied_at="2025-01-01 10:00:00")
        self._insert_session_raw(tid, mode="discuss", teacher_quality=5, studied_at="2025-06-01 10:00:00")

        rows = session_repository.get_discuss_sessions(tid)
        self.assertEqual(rows[0]["teacher_quality"], 5)
        self.assertEqual(rows[1]["teacher_quality"], 2)

    def test_get_discuss_sessions_respects_limit(self) -> None:
        tid = self._insert_topic(name="T")
        for i in range(7):
            self._insert_session_raw(tid, mode="discuss",
                                      studied_at=f"2025-0{i % 9 + 1}-01 10:00:00")

        self.assertEqual(len(session_repository.get_discuss_sessions(tid, limit=3)), 3)

    def test_get_discuss_sessions_default_limit_is_five(self) -> None:
        tid = self._insert_topic(name="T")
        for i in range(7):
            self._insert_session_raw(tid, mode="discuss",
                                      studied_at=f"2025-0{i % 9 + 1}-01 10:00:00")

        self.assertEqual(len(session_repository.get_discuss_sessions(tid)), 5)

    # ------------------------------------------------------------------
    # get_mock_sessions
    # ------------------------------------------------------------------

    def test_get_mock_sessions_returns_empty_when_none(self) -> None:
        tid = self._insert_topic(name="T")
        self.assertEqual(session_repository.get_mock_sessions(tid), [])

    def test_get_mock_sessions_excludes_discuss_mode(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="discuss", teacher_quality=5)
        self.assertEqual(session_repository.get_mock_sessions(tid), [])

    def test_get_mock_sessions_includes_null_mode_legacy_rows(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode=None, student_quality=3,
                                  weak_areas="timing, confidence")
        rows = session_repository.get_mock_sessions(tid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["weak_areas"], "timing, confidence")

    def test_get_mock_sessions_legacy_weak_areas_not_lost_when_no_teacher_weak_areas(self) -> None:
        """Legacy weak_areas column must surface when teacher_weak_areas is NULL."""
        tid = self._insert_topic(name="T2")
        self._insert_session_raw(tid, mode=None, student_quality=3,
                                  weak_areas="legacy gap", teacher_weak_areas=None)
        rows = session_repository.get_mock_sessions(tid)
        self.assertEqual(rows[0]["weak_areas"], "legacy gap")

    def test_get_mock_sessions_teacher_weak_areas_takes_priority_over_legacy(self) -> None:
        """teacher_weak_areas wins when both columns are populated."""
        tid = self._insert_topic(name="T3")
        self._insert_session_raw(tid, mode="mock", student_quality=3,
                                  weak_areas="old", teacher_weak_areas='{"new": "data"}')
        rows = session_repository.get_mock_sessions(tid)
        self.assertEqual(rows[0]["weak_areas"], '{"new": "data"}')

    def test_get_mock_sessions_coalesces_teacher_quality_first(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="mock", teacher_quality=5,
                                  student_quality=2, quality_score=3)
        rows = session_repository.get_mock_sessions(tid)
        self.assertEqual(rows[0]["quality"], 5)

    def test_get_mock_sessions_coalesces_student_quality_when_no_teacher(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="mock", teacher_quality=None,
                                  student_quality=2, quality_score=3)
        rows = session_repository.get_mock_sessions(tid)
        self.assertEqual(rows[0]["quality"], 2)

    def test_get_mock_sessions_coalesces_quality_score_as_last_resort(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="mock", teacher_quality=None,
                                  student_quality=None, quality_score=3)
        rows = session_repository.get_mock_sessions(tid)
        self.assertEqual(rows[0]["quality"], 3)

    def test_get_mock_sessions_quality_is_none_when_all_null(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="mock", teacher_quality=None,
                                  student_quality=None, quality_score=None)
        rows = session_repository.get_mock_sessions(tid)
        self.assertIsNone(rows[0]["quality"])

    def test_get_mock_sessions_ordered_most_recent_first(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="mock", teacher_quality=2, studied_at="2025-01-01 10:00:00")
        self._insert_session_raw(tid, mode="mock", teacher_quality=5, studied_at="2025-06-01 10:00:00")

        rows = session_repository.get_mock_sessions(tid)
        # quality is the COALESCE alias — most recent (teacher_quality=5) should be first
        self.assertEqual(rows[0]["quality"], 5)
        self.assertEqual(rows[1]["quality"], 2)

    def test_get_mock_sessions_respects_limit(self) -> None:
        tid = self._insert_topic(name="T")
        for i in range(7):
            self._insert_session_raw(tid, mode="mock",
                                      studied_at=f"2025-0{i % 9 + 1}-01 10:00:00")
        self.assertEqual(len(session_repository.get_mock_sessions(tid, limit=2)), 2)

    # ------------------------------------------------------------------
    # has_mock_history
    # ------------------------------------------------------------------

    def test_has_mock_history_false_when_no_sessions(self) -> None:
        tid = self._insert_topic(name="T")
        self.assertFalse(session_repository.has_mock_history(tid))

    def test_has_mock_history_false_for_nonexistent_topic(self) -> None:
        self.assertFalse(session_repository.has_mock_history(99999))

    def test_has_mock_history_true_for_mock_mode(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="mock")
        self.assertTrue(session_repository.has_mock_history(tid))

    def test_has_mock_history_true_for_null_mode_legacy_row(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode=None)
        self.assertTrue(session_repository.has_mock_history(tid))

    def test_has_mock_history_false_when_only_discuss_sessions(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="discuss")
        self._insert_session_raw(tid, mode="discuss")
        self.assertFalse(session_repository.has_mock_history(tid))

    def test_has_mock_history_true_when_mixed_modes(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session_raw(tid, mode="discuss")
        self._insert_session_raw(tid, mode="mock")
        self.assertTrue(session_repository.has_mock_history(tid))


if __name__ == "__main__":
    unittest.main()

