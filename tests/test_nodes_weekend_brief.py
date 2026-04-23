"""Tests for the ``weekend_brief`` node.

Covers:
- Due topics branch: formatting, overdue indicator, weak areas focus line
- In-progress fallback: shown when no SM-2 topics are due
- All-caught-up branch: nothing due, nothing in progress
- Planning fields (proposed_slots, has_study_plan, preview_only) are always safe defaults
"""

from datetime import date
from unittest.mock import patch

from src.agent.nodes import weekend_brief


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(due_topics=None, in_progress=None):
    """Invoke weekend_brief with patched integrations."""
    due_topics = due_topics or []
    in_progress = in_progress or []
    with patch("src.agent.nodes._sm2.get_due_topics", return_value=due_topics), \
         patch("src.agent.nodes.topic_repository.get_in_progress_topic_names", return_value=in_progress):
        return weekend_brief({"trigger": "weekend"})


# ---------------------------------------------------------------------------
# Due-topics branch
# ---------------------------------------------------------------------------

def test_weekend_brief_due_topics_lists_topic_names():
    due = [
        {"name": "System Design", "next_review": date.today().isoformat(), "weak_areas": None},
    ]
    result = _run(due_topics=due)
    msg = result["messages"][0]
    assert "System Design" in msg
    assert "topic(s) due" in msg


def test_weekend_brief_due_topics_does_not_show_weak_areas():
    """Weak areas are collected after /done but not displayed in the weekend brief."""
    due = [
        {"name": "DSA - Trees", "next_review": date.today().isoformat(), "weak_areas": "BFS vs DFS"},
    ]
    result = _run(due_topics=due)
    assert "focus:" not in result["messages"][0]
    assert "BFS vs DFS" not in result["messages"][0]


def test_weekend_brief_due_topics_no_focus_when_no_weak_areas():
    due = [
        {"name": "Behavioural", "next_review": date.today().isoformat(), "weak_areas": None},
    ]
    result = _run(due_topics=due)
    assert "focus:" not in result["messages"][0]


def test_weekend_brief_due_topics_shows_overdue_indicator():
    overdue_date = "2026-04-01"  # well in the past relative to April 19 2026
    due = [
        {"name": "System Design", "next_review": overdue_date, "weak_areas": None},
    ]
    result = _run(due_topics=due)
    assert "overdue" in result["messages"][0]


def test_weekend_brief_due_topics_no_overdue_when_due_today():
    due = [
        {"name": "System Design", "next_review": date.today().isoformat(), "weak_areas": None},
    ]
    result = _run(due_topics=due)
    assert "overdue" not in result["messages"][0]


def test_weekend_brief_due_topics_asks_for_time_block():
    due = [
        {"name": "System Design", "next_review": date.today().isoformat(), "weak_areas": None},
    ]
    result = _run(due_topics=due)
    assert "time block" in result["messages"][0]


# ---------------------------------------------------------------------------
# In-progress fallback branch
# ---------------------------------------------------------------------------

def test_weekend_brief_in_progress_fallback_lists_topics():
    result = _run(due_topics=[], in_progress=["DSA - Arrays", "System Design"])
    msg = result["messages"][0]
    assert "DSA - Arrays" in msg
    assert "System Design" in msg


def test_weekend_brief_in_progress_fallback_mentions_nothing_due():
    result = _run(due_topics=[], in_progress=["DSA - Arrays"])
    assert "Nothing due" in result["messages"][0]


def test_weekend_brief_in_progress_fallback_offers_booking():
    result = _run(due_topics=[], in_progress=["DSA - Arrays"])
    assert "book" in result["messages"][0].lower()


# ---------------------------------------------------------------------------
# All-caught-up branch
# ---------------------------------------------------------------------------

def test_weekend_brief_all_caught_up_message():
    result = _run(due_topics=[], in_progress=[])
    assert "caught up" in result["messages"][0]


def test_weekend_brief_all_caught_up_suggests_study_command():
    result = _run(due_topics=[], in_progress=[])
    assert "/study" in result["messages"][0]


# ---------------------------------------------------------------------------
# Planning-field invariants
# ---------------------------------------------------------------------------

def test_weekend_brief_always_sets_preview_only():
    for scenario in [
        {"due_topics": [{"name": "X", "next_review": date.today().isoformat(), "weak_areas": None}]},
        {"in_progress": ["Y"]},
        {},
    ]:
        result = _run(**scenario)
        assert result.get("preview_only") is True, f"preview_only not set for {scenario}"


def test_weekend_brief_has_study_plan_is_false():
    due = [{"name": "System Design", "next_review": date.today().isoformat(), "weak_areas": None}]
    result = _run(due_topics=due)
    assert result.get("has_study_plan") is False


def test_weekend_brief_proposed_slots_not_set():
    """weekend_brief must never populate proposed_slots — it does no slot packing."""
    due = [{"name": "System Design", "next_review": date.today().isoformat(), "weak_areas": None}]
    result = _run(due_topics=due)
    assert result.get("proposed_slots") is None

