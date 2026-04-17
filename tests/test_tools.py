"""Unit tests for LangGraph tool wrappers in ``src/agent/tools.py``."""

from datetime import date
from unittest.mock import patch

import pytest

from src.agent import tools


def _call_tool(tool_obj, *args, **kwargs):
	"""Call a @tool-decorated object in tests without depending on runtime wrappers."""
	fn = getattr(tool_obj, "func", tool_obj)
	return fn(*args, **kwargs)


def test_get_calendar_events_parses_date_and_calls_gcal():
	expected = [{"id": "1", "summary": "Meeting"}]
	with patch("src.agent.tools._gcal.get_events", return_value=expected) as mock_get:
		result = _call_tool(tools.get_calendar_events, "2026-04-03")

	mock_get.assert_called_once_with(date(2026, 4, 3))
	assert result == expected


def test_find_free_windows_loads_config_and_calls_gap_finder():
	fake_config = {"focus_windows": [{"start": "08:00", "end": "09:00"}]}
	fake_events = [{"summary": "Standup"}]
	fake_windows = [{"start": "08:00", "end": "09:00", "duration_min": 60}]

	with patch("src.agent.tools._load_config", return_value=fake_config) as mock_cfg, \
		 patch("src.agent.tools._gcal.get_events", return_value=fake_events) as mock_events, \
		 patch("src.agent.tools._gap_finder.find_free_windows", return_value=fake_windows) as mock_windows:
		result = _call_tool(tools.find_free_windows, "2026-04-03")

	target = date(2026, 4, 3)
	mock_cfg.assert_called_once()
	mock_events.assert_called_once_with(target)
	mock_windows.assert_called_once_with(fake_events, target, fake_config)
	assert result == fake_windows


def test_get_due_topics_uses_repo_db_path():
	due = [{"name": "System Design"}]
	with patch("src.agent.tools._sm2.get_due_topics", return_value=due) as mock_due:
		result = _call_tool(tools.get_due_topics)

	mock_due.assert_called_once_with(db_path=tools._DB_PATH)
	assert result == due


def test_write_calendar_event_returns_created_event_when_self_owned():
	created = {"id": "evt_1", "creator": {"self": True}}
	with patch("src.agent.tools._gcal.write_event", return_value=created) as mock_write:
		result = _call_tool(
			tools.write_calendar_event,
			topic="System Design",
			start="2026-04-03T09:00:00+00:00",
			end="2026-04-03T10:00:00+00:00",
		)

	mock_write.assert_called_once()
	assert result == created


def test_write_calendar_event_raises_when_event_not_self_owned():
	created = {"id": "evt_2", "creator": {"self": False}}
	with patch("src.agent.tools._gcal.write_event", return_value=created):
		with pytest.raises(PermissionError):
			_call_tool(
				tools.write_calendar_event,
				topic="System Design",
				start="2026-04-03T09:00:00+00:00",
				end="2026-04-03T10:00:00+00:00",
			)


def test_log_study_session_rejects_invalid_quality_score():
	with patch("src.agent.tools.session_repository.insert_session") as mock_insert, \
		 patch("src.agent.tools._sm2.update_topic_after_session") as mock_update:
		with pytest.raises(ValueError):
			_call_tool(tools.log_study_session, topic_id=1, duration_min=30, quality_score=4)

	mock_insert.assert_not_called()
	mock_update.assert_not_called()


def test_log_study_session_with_weak_areas_updates_topic_and_sm2():
	with patch("src.agent.tools.session_repository.insert_session") as mock_insert, \
		 patch("src.agent.tools.topic_repository.update_topic_weak_areas") as mock_topic_weak, \
		 patch("src.agent.tools._sm2.update_topic_after_session") as mock_update:
		_call_tool(
			tools.log_study_session,
			topic_id=7,
			duration_min=45,
			quality_score=3,
			weak_areas="trade-offs",
		)

	mock_insert.assert_called_once_with(
		topic_id=7,
		duration_min=45,
		quality_score=3,
		weak_areas="trade-offs",
	)
	mock_topic_weak.assert_called_once_with(7, "trade-offs")
	mock_update.assert_called_once_with(db_path=tools._DB_PATH, topic_id=7, quality=3)


def test_log_study_session_without_weak_areas_skips_topic_update():
	with patch("src.agent.tools.session_repository.insert_session") as mock_insert, \
		 patch("src.agent.tools.topic_repository.update_topic_weak_areas") as mock_topic_weak, \
		 patch("src.agent.tools._sm2.update_topic_after_session") as mock_update:
		_call_tool(
			tools.log_study_session,
			topic_id=9,
			duration_min=60,
			quality_score=5,
			weak_areas="",
		)

	mock_insert.assert_called_once_with(
		topic_id=9,
		duration_min=60,
		quality_score=5,
		weak_areas=None,
	)
	mock_topic_weak.assert_not_called()
	mock_update.assert_called_once_with(db_path=tools._DB_PATH, topic_id=9, quality=5)
