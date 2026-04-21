"""Integration-style tests for the ``daily_planning`` node.

These tests patch external integrations but exercise the real planning node logic
end-to-end, including synthetic study busy blocks that are passed into the gap
finder inside ``daily_planning``.
"""

from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

from src.agent.nodes import daily_planning


class _MorningDateTime(datetime):
    """Frozen datetime shim so morning planning tests run before 08:00."""

    @classmethod
    def now(cls, tz=None):
        return cls.combine(date.today(), time(7, 0), tzinfo=tz)



def test_daily_planning_moves_mock_slots_after_synthesized_study_blocks():
    """Mock slots should not overlap missing in-progress study blocks."""
    config = {
        "timezone": "UTC",
        "focus_windows": [{"start": "08:00", "end": "12:00"}],
        "protected_blocks": [],
        "min_window_minutes": 25,
    }
    topics_config = {
        "topics": [
            {"name": "System Design", "default_duration_minutes": 60},
        ]
    }
    due_topics = [
        {"name": "System Design", "easiness_factor": 2.3, "next_review": date.today().isoformat()},
    ]

    mock_send_buttons = MagicMock(return_value=None)
    with patch("src.agent.nodes._load_config", return_value=config), \
         patch("src.agent.nodes._load_topics", return_value=topics_config), \
         patch("src.agent.nodes._gcal.get_events", return_value=[]), \
         patch("src.agent.nodes._sm2.get_due_topics", return_value=due_topics), \
         patch("src.agent.nodes.datetime", _MorningDateTime), \
         patch("src.agent.nodes.topic_repository.get_in_progress_topic_names", return_value=["DSA - Arrays"]), \
         patch("src.agent.nodes.rebook_study_events"), \
         patch("src.agent.nodes._telegram.send_buttons", mock_send_buttons), \
         patch("src.agent.nodes.interrupt", return_value="yes, book them"):
        result = daily_planning({"trigger": "daily"})

    assert result["proposed_slots"] is not None
    assert result["proposed_slots"][0]["topic"] == "System Design"
    assert result["proposed_slots"][0]["start"] == "09:00"
    # The morning plan message is sent as button text — verify its content
    plan_text = mock_send_buttons.call_args[0][0]
    assert "08:00–09:00 [STUDY] DSA - Arrays (60min)" in plan_text



def test_daily_planning_with_many_in_progress_topics_does_not_crash():
    """Large in-progress lists should be capped safely instead of generating invalid times."""
    config = {
        "timezone": "UTC",
        "focus_windows": [{"start": "08:00", "end": "23:00"}],
        "protected_blocks": [],
        "min_window_minutes": 25,
    }
    topics_config = {"topics": []}

    with patch("src.agent.nodes._load_config", return_value=config), \
         patch("src.agent.nodes._load_topics", return_value=topics_config), \
         patch("src.agent.nodes._gcal.get_events", return_value=[]), \
         patch("src.agent.nodes._sm2.get_due_topics", return_value=[]), \
         patch(
             "src.agent.nodes.topic_repository.get_in_progress_topic_names",
             return_value=[f"Topic {i}" for i in range(20)],
         ), \
         patch("src.agent.nodes.rebook_study_events"):
        result = daily_planning({"trigger": "daily"})

    assert result["messages"]
    assert "⚠️" not in result["messages"][0]
    assert "T24:" not in result["messages"][0]


