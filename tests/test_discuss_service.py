"""Unit tests for src/services/discuss_service.py.

All DB calls go through a temp SQLite database (same pattern as
test_repositories.py).  Telegram calls are always mocked out.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src.infrastructure import db as core_db
from src.services import discuss_service


# ---------------------------------------------------------------------------
# Shared test base
# ---------------------------------------------------------------------------

class DiscussServiceTestCase(unittest.TestCase):
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
            conn.executescript("""
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
                    topic_id INTEGER NOT NULL,
                    studied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    duration_min INTEGER,
                    mode TEXT,
                    quality_score INTEGER,
                    weak_areas TEXT,
                    student_quality INTEGER,
                    student_weak_areas TEXT,
                    teacher_quality INTEGER,
                    teacher_weak_areas TEXT,
                    teacher_source TEXT,
                    calibration_gap INTEGER
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def _insert_topic(self, **kwargs) -> int:
        defaults = {
            "name": "Topic",
            "tier": 1,
            "status": "discussing",
            "topic_type": "conceptual",
            "weak_areas": None,
        }
        defaults.update(kwargs)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """INSERT INTO topics (name, tier, status, topic_type, weak_areas)
                   VALUES (:name, :tier, :status, :topic_type, :weak_areas)""",
                defaults,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def _insert_session(self, topic_id: int, mode: str, teacher_quality: int,
                        teacher_weak_areas: str | None = None) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO sessions (topic_id, mode, teacher_quality, teacher_weak_areas, studied_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (topic_id, mode, teacher_quality, teacher_weak_areas),
            )
            conn.commit()
        finally:
            conn.close()

    def _get_topic_status(self, topic_id: int) -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT status FROM topics WHERE id = ?", (topic_id,)).fetchone()
            return row[0]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# _parse_weak_area_keys (internal helper — tested directly)
# ---------------------------------------------------------------------------

class ParseWeakAreaKeysTests(unittest.TestCase):
    def test_none_returns_empty(self) -> None:
        self.assertEqual(discuss_service._parse_weak_area_keys(None), [])

    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(discuss_service._parse_weak_area_keys(""), [])

    def test_whitespace_only_returns_empty(self) -> None:
        self.assertEqual(discuss_service._parse_weak_area_keys("   "), [])

    def test_valid_json_dict_returns_keys(self) -> None:
        raw = json.dumps({"a": "weak", "b": "ok"})
        self.assertEqual(sorted(discuss_service._parse_weak_area_keys(raw)), ["a", "b"])

    def test_json_dict_skips_falsy_values(self) -> None:
        raw = json.dumps({"a": "weak", "b": "", "c": None})
        self.assertEqual(discuss_service._parse_weak_area_keys(raw), ["a"])

    def test_invalid_json_returns_raw_string(self) -> None:
        self.assertEqual(discuss_service._parse_weak_area_keys("{bad json"), ["{bad json"])

    def test_plain_string_returns_single_element(self) -> None:
        self.assertEqual(discuss_service._parse_weak_area_keys("plain area"), ["plain area"])

    def test_json_array_returns_empty(self) -> None:
        # Non-dict JSON should not crash and returns nothing
        self.assertEqual(discuss_service._parse_weak_area_keys('["a", "b"]'), [])


# ---------------------------------------------------------------------------
# _find_repeated_weak_areas (internal helper — tested directly)
# ---------------------------------------------------------------------------

class FindRepeatedWeakAreasTests(unittest.TestCase):
    def test_empty_sessions_returns_empty(self) -> None:
        self.assertEqual(discuss_service._find_repeated_weak_areas([]), [])

    def test_single_session_no_repeats(self) -> None:
        sessions = [{"teacher_weak_areas": '{"a": "weak"}'}]
        self.assertEqual(discuss_service._find_repeated_weak_areas(sessions), [])

    def test_key_in_two_sessions_is_repeated(self) -> None:
        sessions = [
            {"teacher_weak_areas": '{"a": "weak", "b": "ok"}'},
            {"teacher_weak_areas": '{"a": "still weak"}'},
        ]
        self.assertEqual(discuss_service._find_repeated_weak_areas(sessions), ["a"])

    def test_key_in_only_one_session_not_repeated(self) -> None:
        sessions = [
            {"teacher_weak_areas": '{"a": "weak"}'},
            {"teacher_weak_areas": '{"b": "weak"}'},
        ]
        self.assertEqual(discuss_service._find_repeated_weak_areas(sessions), [])

    def test_none_weak_areas_handled(self) -> None:
        sessions = [
            {"teacher_weak_areas": None},
            {"teacher_weak_areas": '{"a": "weak"}'},
        ]
        self.assertEqual(discuss_service._find_repeated_weak_areas(sessions), [])

    def test_result_is_sorted(self) -> None:
        sessions = [
            {"teacher_weak_areas": '{"z": "x", "a": "x"}'},
            {"teacher_weak_areas": '{"z": "x", "a": "x"}'},
        ]
        result = discuss_service._find_repeated_weak_areas(sessions)
        self.assertEqual(result, sorted(result))


# ---------------------------------------------------------------------------
# get_discuss_context
# ---------------------------------------------------------------------------

class GetDiscussContextTests(DiscussServiceTestCase):
    def test_topic_not_found_returns_error(self) -> None:
        result = discuss_service.get_discuss_context("Ghost Topic")
        self.assertIn("error", result)

    def test_ambiguous_name_returns_error(self) -> None:
        self._insert_topic(name="RAG - Alpha")
        self._insert_topic(name="RAG - Beta")
        result = discuss_service.get_discuss_context("RAG")
        self.assertIn("error", result)

    def test_happy_path_returns_all_keys(self) -> None:
        self._insert_topic(name="Topic A", weak_areas="some gap")
        result = discuss_service.get_discuss_context("Topic A")
        for key in ("topic_id", "topic_name", "topic_type", "weak_areas",
                    "discuss_history", "mock_history_exists", "mock_history"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_returns_correct_topic_name(self) -> None:
        self._insert_topic(name="Topic A")
        result = discuss_service.get_discuss_context("Topic A")
        self.assertEqual(result["topic_name"], "Topic A")

    def test_no_history_returns_empty_lists(self) -> None:
        self._insert_topic(name="Topic A")
        result = discuss_service.get_discuss_context("Topic A")
        self.assertEqual(result["discuss_history"], [])
        self.assertEqual(result["mock_history"], [])
        self.assertFalse(result["mock_history_exists"])

    def test_discuss_history_populated(self) -> None:
        tid = self._insert_topic(name="Topic A")
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}')
        result = discuss_service.get_discuss_context("Topic A")
        self.assertEqual(len(result["discuss_history"]), 1)

    def test_mock_history_exists_true_when_mock_session_present(self) -> None:
        tid = self._insert_topic(name="Topic A")
        self._insert_session(tid, mode="mock", teacher_quality=5)
        result = discuss_service.get_discuss_context("Topic A")
        self.assertTrue(result["mock_history_exists"])


# ---------------------------------------------------------------------------
# assess_discuss_readiness
# ---------------------------------------------------------------------------

class AssessDiscussReadinessTests(DiscussServiceTestCase):

    def _call(self, topic_name: str, quality: int, weak_areas: str,
              mock_send_message=None, mock_send_buttons=None):
        """Helper: call the service with Telegram patched out."""
        with patch("src.integrations.telegram_client.send_message",
                   mock_send_message or MagicMock()) as msg, \
             patch("src.integrations.telegram_client.send_buttons",
                   mock_send_buttons or MagicMock()) as btn:
            result = discuss_service.assess_discuss_readiness(topic_name, quality, weak_areas)
            return result, msg, btn

    # --- Input validation ---

    def test_invalid_quality_returns_error_without_db_write(self) -> None:
        tid = self._insert_topic(name="T")
        result, _, _ = self._call("T", 4, "{}")
        self.assertIn("error", result)
        # No session should have been inserted
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM sessions WHERE topic_id = ?", (tid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_topic_not_found_returns_error(self) -> None:
        result, _, _ = self._call("Ghost", 3, "{}")
        self.assertIn("error", result)

    # --- not_ready ---

    def test_not_ready_when_quality_below_threshold(self) -> None:
        self._insert_topic(name="T")
        result, msg, btn = self._call("T", 3, '{"gap": "weak"}')
        self.assertEqual(result["recommendation"], "not_ready")
        self.assertEqual(result["repeated_weak_areas"], [])
        msg.assert_called_once()
        btn.assert_not_called()

    def test_not_ready_inserts_session(self) -> None:
        tid = self._insert_topic(name="T")
        self._call("T", 2, '{"gap": "weak"}')
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE topic_id = ? AND mode = 'discuss'", (tid,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_not_ready_does_not_change_topic_status(self) -> None:
        tid = self._insert_topic(name="T", status="discussing")
        self._call("T", 2, "{}")
        self.assertEqual(self._get_topic_status(tid), "discussing")

    # --- ready (fresh) ---

    def test_ready_fresh_when_quality_5_no_repeats(self) -> None:
        # Non-reentry ready path: graph.invoke is called (not send_message).
        self._insert_topic(name="T")
        mock_invoke = MagicMock()
        with patch("src.services.discuss_service._graph_module.graph.invoke", mock_invoke), \
             patch("src.services.discuss_service._telegram.get_chat_id", return_value=9999):
            result, msg, btn = self._call("T", 5, '{"gap": "strong"}')
        self.assertEqual(result["recommendation"], "ready")
        self.assertIn("first", result["reason"].lower())
        mock_invoke.assert_called_once()
        call_state = mock_invoke.call_args[0][0]
        self.assertEqual(call_state["trigger"], "discuss_ready_confirm")
        msg.assert_not_called()  # send_message not used in non-reentry ready path
        btn.assert_not_called()

    def test_ready_fresh_reason_mentions_first_discuss(self) -> None:
        self._insert_topic(name="T")
        result, _, _ = self._call("T", 5, "{}")
        self.assertIn("First", result["reason"])

    # --- ready (reentry) ---

    def test_ready_reentry_when_mock_history_exists(self) -> None:
        tid = self._insert_topic(name="T")
        self._insert_session(tid, mode="mock", teacher_quality=3)
        result, msg, _ = self._call("T", 5, "{}")
        self.assertEqual(result["recommendation"], "ready")
        self.assertIn("another", result["reason"].lower())
        msg.assert_called_once()

    def test_ready_multi_discuss_no_repeats_uses_count_in_reason(self) -> None:
        tid = self._insert_topic(name="T")
        # Two earlier sessions with no overlapping keys
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"x": "ok"}')
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"y": "ok"}')
        # Third call → ready
        result, _, _ = self._call("T", 5, '{"z": "strong"}')
        self.assertEqual(result["recommendation"], "ready")
        # Should mention session count (3 sessions inserted total)
        self.assertIn("3", result["reason"])

    # --- go_back_to_study (discussing → in_progress) ---

    def test_go_back_to_study_when_repeated_gap(self) -> None:
        tid = self._insert_topic(name="T", status="discussing")
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}')
        result, msg, btn = self._call("T", 3, '{"gap": "still weak"}')
        self.assertEqual(result["recommendation"], "go_back_to_study")
        self.assertIn("gap", result["repeated_weak_areas"])
        msg.assert_called_once()
        btn.assert_not_called()

    def test_go_back_to_study_moves_discussing_topic_to_in_progress(self) -> None:
        tid = self._insert_topic(name="T", status="discussing")
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}')
        self._call("T", 3, '{"gap": "still weak"}')
        self.assertEqual(self._get_topic_status(tid), "in_progress")

    def test_go_back_to_study_active_topic_status_unchanged(self) -> None:
        """Active topics are valid discuss targets but set_topic_back_to_in_progress
        guards on 'discussing'. The service must NOT lie and say the topic was moved."""
        tid = self._insert_topic(name="T", status="active")
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}')
        result, msg, _ = self._call("T", 3, '{"gap": "still weak"}')
        self.assertEqual(result["recommendation"], "go_back_to_study")
        # Status should remain active — no transition happened
        self.assertEqual(self._get_topic_status(tid), "active")
        # Message should NOT claim the topic was moved
        sent_text = msg.call_args[0][0]
        self.assertNotIn("moved back", sent_text.lower())
        self.assertNotIn("In Progress", sent_text)

    def test_go_back_to_study_discussing_topic_message_says_moved(self) -> None:
        tid = self._insert_topic(name="T", status="discussing")
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}')
        _, msg, _ = self._call("T", 3, '{"gap": "still weak"}')
        sent_text = msg.call_args[0][0]
        self.assertIn("moved back", sent_text.lower())

    # --- DB failure isolation ---

    def test_status_rollback_failure_returns_error(self) -> None:
        tid = self._insert_topic(name="T", status="discussing")
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}')
        with patch("src.repositories.topic_repository.set_topic_back_to_in_progress",
                   side_effect=Exception("DB exploded")):
            result, _, _ = self._call("T", 3, '{"gap": "still weak"}')
        self.assertIn("error", result)

    def test_telegram_failure_does_not_affect_return_value(self) -> None:
        self._insert_topic(name="T")
        with patch("src.integrations.telegram_client.send_message",
                   side_effect=RuntimeError("timeout")):
            result = discuss_service.assess_discuss_readiness("T", 5, "{}")
        # Should still return a result despite Telegram being down
        self.assertEqual(result["recommendation"], "ready")

    def test_session_insert_failure_returns_error(self) -> None:
        self._insert_topic(name="T")
        with patch("src.repositories.session_repository.insert_discuss_session",
                   side_effect=Exception("disk full")):
            result, _, _ = self._call("T", 3, "{}")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
