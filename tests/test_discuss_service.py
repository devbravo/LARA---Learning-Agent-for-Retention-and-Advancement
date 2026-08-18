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
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    engineer_id INTEGER,
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
            """)
            conn.commit()
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

    def _insert_topic(self, **kwargs) -> int:
        """Insert a catalog topic plus this test's default engineer's progress row.

        Returns the topic id (not the progress row id) to match the previous
        single-user helper's contract, since most tests only need the topic id.
        """
        engineer_id = kwargs.pop("engineer_id", None) or self._default_engineer_id()
        topic_defaults = {"name": "Topic", "tier": 1, "topic_type": "conceptual"}
        progress_defaults = {"status": "discussing", "weak_areas": None}
        for key in ("status", "weak_areas"):
            if key in kwargs:
                progress_defaults[key] = kwargs.pop(key)
        topic_defaults.update(kwargs)

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                """INSERT INTO topic_catalog (name, tier, topic_type)
                   VALUES (:name, :tier, :topic_type)""",
                topic_defaults,
            )
            topic_id = cursor.lastrowid
            progress_defaults["engineer_id"] = engineer_id
            progress_defaults["topic_id"] = topic_id
            conn.execute(
                """INSERT INTO engineer_topic_progress (engineer_id, topic_id, status, weak_areas)
                   VALUES (:engineer_id, :topic_id, :status, :weak_areas)""",
                progress_defaults,
            )
            conn.commit()
            return topic_id
        finally:
            conn.close()

    def _default_engineer_id(self) -> int:
        if not hasattr(self, "_default_engineer"):
            self._default_engineer = self._insert_engineer()
        return self._default_engineer

    def _insert_session(self, topic_id: int, mode: str, teacher_quality: int,
                        teacher_weak_areas: str | None = None, engineer_id: int | None = None) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO sessions (engineer_id, topic_id, mode, teacher_quality, teacher_weak_areas, studied_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (engineer_id or self._default_engineer_id(), topic_id, mode, teacher_quality, teacher_weak_areas),
            )
            conn.commit()
        finally:
            conn.close()

    def _get_topic_status(self, engineer_id: int, topic_id: int) -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
                (engineer_id, topic_id),
            ).fetchone()
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
        engineer_id = self._insert_engineer()
        result = discuss_service.get_discuss_context(engineer_id, "Ghost Topic")
        self.assertIn("error", result)

    def test_ambiguous_name_returns_error(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="RAG - Alpha", engineer_id=engineer_id)
        self._insert_topic(name="RAG - Beta", engineer_id=engineer_id)
        result = discuss_service.get_discuss_context(engineer_id, "RAG")
        self.assertIn("error", result)

    def test_happy_path_returns_all_keys(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="Topic A", weak_areas="some gap", engineer_id=engineer_id)
        result = discuss_service.get_discuss_context(engineer_id, "Topic A")
        for key in ("topic_id", "topic_name", "topic_type", "weak_areas",
                    "discuss_history", "mock_history_exists", "mock_history"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_returns_correct_topic_name(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="Topic A", engineer_id=engineer_id)
        result = discuss_service.get_discuss_context(engineer_id, "Topic A")
        self.assertEqual(result["topic_name"], "Topic A")

    def test_no_history_returns_empty_lists(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="Topic A", engineer_id=engineer_id)
        result = discuss_service.get_discuss_context(engineer_id, "Topic A")
        self.assertEqual(result["discuss_history"], [])
        self.assertEqual(result["mock_history"], [])
        self.assertFalse(result["mock_history_exists"])

    def test_discuss_history_populated(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="Topic A", engineer_id=engineer_id)
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}', engineer_id=engineer_id)
        result = discuss_service.get_discuss_context(engineer_id, "Topic A")
        self.assertEqual(len(result["discuss_history"]), 1)

    def test_mock_history_exists_true_when_mock_session_present(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="Topic A", engineer_id=engineer_id)
        self._insert_session(tid, mode="mock", teacher_quality=5, engineer_id=engineer_id)
        result = discuss_service.get_discuss_context(engineer_id, "Topic A")
        self.assertTrue(result["mock_history_exists"])

    def test_does_not_leak_other_engineers_history(self) -> None:
        engineer_a = self._insert_engineer(external_id="a")
        engineer_b = self._insert_engineer(external_id="b")
        tid = self._insert_topic(name="Shared", engineer_id=engineer_a)
        # Same-named catalog topic, but progress row only exists for engineer_a here;
        # engineer_b has their own history on the same catalog topic id.
        self._insert_session(tid, mode="mock", teacher_quality=5, engineer_id=engineer_b)

        result = discuss_service.get_discuss_context(engineer_a, "Shared")
        self.assertFalse(result["mock_history_exists"])
        self.assertEqual(result["mock_history"], [])


# ---------------------------------------------------------------------------
# assess_discuss_readiness
# ---------------------------------------------------------------------------

class AssessDiscussReadinessTests(DiscussServiceTestCase):

    def _call(self, engineer_id: int, topic_name: str, quality: int, weak_areas: str,
              mock_send_message=None, mock_send_buttons=None,
              mock_safe_invoke=None):
        """Helper: call the service with Telegram + graph invocation patched out.

        ``safe_chat_invoke`` is always patched so the fresh-ready path doesn't
        accidentally invoke the real LangGraph singleton during tests.  Pass
        ``mock_safe_invoke`` to inspect the invocation or override its return
        value (e.g. ``MagicMock(return_value=False)`` to simulate a busy chat).
        """
        invoke_mock = mock_safe_invoke or MagicMock(return_value=True)
        with patch("src.integrations.telegram_client.send_message",
                   mock_send_message or MagicMock()) as msg, \
             patch("src.integrations.telegram_client.send_buttons",
                   mock_send_buttons or MagicMock()) as btn, \
             patch("src.services.discuss_service._dispatcher.safe_chat_invoke",
                   invoke_mock), \
             patch("src.services.discuss_service._telegram.get_chat_id",
                   return_value=9999):
            result = discuss_service.assess_discuss_readiness(engineer_id, topic_name, quality, weak_areas)
            return result, msg, btn

    # --- Input validation ---

    def test_invalid_quality_returns_error_without_db_write(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", engineer_id=engineer_id)
        result, _, _ = self._call(engineer_id, "T", 4, "{}")
        self.assertIn("error", result)
        # No session should have been inserted
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM sessions WHERE topic_id = ?", (tid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_topic_not_found_returns_error(self) -> None:
        engineer_id = self._insert_engineer()
        result, _, _ = self._call(engineer_id, "Ghost", 3, "{}")
        self.assertIn("error", result)

    # --- not_ready ---

    def test_not_ready_when_quality_below_threshold(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="T", engineer_id=engineer_id)
        result, msg, btn = self._call(engineer_id, "T", 3, '{"gap": "weak"}')
        self.assertEqual(result["recommendation"], "not_ready")
        self.assertEqual(result["repeated_weak_areas"], [])
        msg.assert_called_once()
        btn.assert_not_called()

    def test_not_ready_inserts_session(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", engineer_id=engineer_id)
        self._call(engineer_id, "T", 2, '{"gap": "weak"}')
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE topic_id = ? AND mode = 'discuss'", (tid,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_not_ready_does_not_change_topic_status(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", status="discussing", engineer_id=engineer_id)
        self._call(engineer_id, "T", 2, "{}")
        self.assertEqual(self._get_topic_status(engineer_id, tid), "discussing")

    # --- ready (fresh) ---

    def test_ready_fresh_when_quality_5_no_repeats(self) -> None:
        # Non-reentry ready path: routes through dispatcher.safe_chat_invoke
        # (not send_message) with the discuss_ready_confirm trigger.
        engineer_id = self._insert_engineer()
        self._insert_topic(name="T", engineer_id=engineer_id)
        mock_safe_invoke = MagicMock(return_value=True)
        result, msg, btn = self._call(engineer_id, "T", 5, '{"gap": "strong"}',
                                      mock_safe_invoke=mock_safe_invoke)
        self.assertEqual(result["recommendation"], "ready")
        self.assertIn("first", result["reason"].lower())
        mock_safe_invoke.assert_called_once()
        call_chat_id, call_state = mock_safe_invoke.call_args[0]
        self.assertEqual(call_chat_id, 9999)
        self.assertEqual(call_state["trigger"], "discuss_ready_confirm")
        self.assertEqual(call_state["current_topic_name"], "T")
        msg.assert_not_called()  # send_message not used when invocation succeeds
        btn.assert_not_called()


    def test_ready_fresh_falls_back_to_plain_message_when_chat_busy(self) -> None:
        """When safe_chat_invoke returns False (chat paused on another flow),
        the service must fall back to a plain readiness message instead of
        clobbering the paused checkpoint with activation buttons.
        """
        engineer_id = self._insert_engineer()
        self._insert_topic(name="T", engineer_id=engineer_id)
        mock_safe_invoke = MagicMock(return_value=False)
        result, msg, btn = self._call(engineer_id, "T", 5, '{"gap": "strong"}',
                                      mock_safe_invoke=mock_safe_invoke)
        self.assertEqual(result["recommendation"], "ready")
        # Invocation was attempted but skipped.
        mock_safe_invoke.assert_called_once()
        # Plain message sent as fallback — no buttons.
        msg.assert_called_once()
        btn.assert_not_called()

    def test_ready_fresh_reason_mentions_first_discuss(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="T", engineer_id=engineer_id)
        result, _, _ = self._call(engineer_id, "T", 5, "{}")
        self.assertIn("First", result["reason"])

    # --- ready (reentry) ---

    def test_ready_reentry_when_mock_history_exists(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", engineer_id=engineer_id)
        self._insert_session(tid, mode="mock", teacher_quality=3, engineer_id=engineer_id)
        mock_safe_invoke = MagicMock(return_value=True)
        result, msg, btn = self._call(engineer_id, "T", 5, "{}", mock_safe_invoke=mock_safe_invoke)
        self.assertEqual(result["recommendation"], "ready")
        self.assertIn("another", result["reason"].lower())
        # Re-entry now routes through safe_chat_invoke, not a plain send_message.
        mock_safe_invoke.assert_called_once()
        _, call_state = mock_safe_invoke.call_args[0]
        self.assertTrue(call_state["current_topic_is_reentry"])
        msg.assert_not_called()
        btn.assert_not_called()

    def test_ready_reentry_falls_back_to_plain_message_when_chat_busy(self) -> None:
        """When safe_chat_invoke returns False on a re-entry topic, fall back to
        a plain readiness message — same behaviour as the fresh-topic path."""
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", engineer_id=engineer_id)
        self._insert_session(tid, mode="mock", teacher_quality=3, engineer_id=engineer_id)
        mock_safe_invoke = MagicMock(return_value=False)
        result, msg, btn = self._call(engineer_id, "T", 5, "{}", mock_safe_invoke=mock_safe_invoke)
        self.assertEqual(result["recommendation"], "ready")
        mock_safe_invoke.assert_called_once()
        msg.assert_called_once()
        btn.assert_not_called()

    def test_ready_multi_discuss_no_repeats_uses_count_in_reason(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", engineer_id=engineer_id)
        # Two earlier sessions with no overlapping keys
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"x": "ok"}', engineer_id=engineer_id)
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"y": "ok"}', engineer_id=engineer_id)
        # Third call → ready
        result, _, _ = self._call(engineer_id, "T", 5, '{"z": "strong"}')
        self.assertEqual(result["recommendation"], "ready")
        # Should mention session count (3 sessions inserted total)
        self.assertIn("3", result["reason"])

    # --- go_back_to_study (discussing → in_progress) ---

    def test_go_back_to_study_when_repeated_gap(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", status="discussing", engineer_id=engineer_id)
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}', engineer_id=engineer_id)
        result, msg, btn = self._call(engineer_id, "T", 3, '{"gap": "still weak"}')
        self.assertEqual(result["recommendation"], "go_back_to_study")
        self.assertIn("gap", result["repeated_weak_areas"])
        msg.assert_called_once()
        btn.assert_not_called()

    def test_go_back_to_study_moves_discussing_topic_to_in_progress(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", status="discussing", engineer_id=engineer_id)
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}', engineer_id=engineer_id)
        self._call(engineer_id, "T", 3, '{"gap": "still weak"}')
        self.assertEqual(self._get_topic_status(engineer_id, tid), "in_progress")

    def test_go_back_to_study_active_topic_status_unchanged(self) -> None:
        """Active topics are valid discuss targets but set_topic_back_to_in_progress
        guards on 'discussing'. The service must NOT lie and say the topic was moved."""
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", status="active", engineer_id=engineer_id)
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}', engineer_id=engineer_id)
        result, msg, _ = self._call(engineer_id, "T", 3, '{"gap": "still weak"}')
        self.assertEqual(result["recommendation"], "go_back_to_study")
        # Status should remain active — no transition happened
        self.assertEqual(self._get_topic_status(engineer_id, tid), "active")
        # Message should NOT claim the topic was moved
        sent_text = msg.call_args[0][0]
        self.assertNotIn("moved back", sent_text.lower())
        self.assertNotIn("In Progress", sent_text)

    def test_go_back_to_study_discussing_topic_message_says_moved(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", status="discussing", engineer_id=engineer_id)
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}', engineer_id=engineer_id)
        _, msg, _ = self._call(engineer_id, "T", 3, '{"gap": "still weak"}')
        sent_text = msg.call_args[0][0]
        self.assertIn("moved back", sent_text.lower())

    # --- DB failure isolation ---

    def test_status_rollback_failure_returns_error(self) -> None:
        engineer_id = self._insert_engineer()
        tid = self._insert_topic(name="T", status="discussing", engineer_id=engineer_id)
        self._insert_session(tid, mode="discuss", teacher_quality=3,
                             teacher_weak_areas='{"gap": "weak"}', engineer_id=engineer_id)
        with patch("src.repositories.topic_repository.set_topic_back_to_in_progress",
                   side_effect=Exception("DB exploded")):
            result, _, _ = self._call(engineer_id, "T", 3, '{"gap": "still weak"}')
        self.assertIn("error", result)

    def test_telegram_failure_does_not_affect_return_value(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="T", engineer_id=engineer_id)
        with patch("src.integrations.telegram_client.send_message",
                   side_effect=RuntimeError("timeout")):
            result = discuss_service.assess_discuss_readiness(engineer_id, "T", 5, "{}")
        # Should still return a result despite Telegram being down
        self.assertEqual(result["recommendation"], "ready")

    def test_session_insert_failure_returns_error(self) -> None:
        engineer_id = self._insert_engineer()
        self._insert_topic(name="T", engineer_id=engineer_id)
        with patch("src.repositories.session_repository.insert_discuss_session",
                   side_effect=Exception("disk full")):
            result, _, _ = self._call(engineer_id, "T", 3, "{}")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
