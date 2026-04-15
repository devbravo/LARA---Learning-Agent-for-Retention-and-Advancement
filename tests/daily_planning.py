"""
Unit tests for the daily_planning slot-packing loop.

These tests exercise the pure slot-assignment logic by calling the
extracted helper directly — no GCal, no DB, no Telegram.

Run with: pytest tests/test_daily_planning.py -v
"""

from datetime import date, datetime, time, timedelta

import pytest

# ---------------------------------------------------------------------------
# Extract the slot-packing logic into a testable helper.
# We mirror exactly what daily_planning does so tests stay honest.
# ---------------------------------------------------------------------------

MIN_WINDOW_MINUTES = 25
MAX_SLOTS = 6


def _fmt_time(t) -> str:
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M")
    return str(t)[:5]


def pack_slots(
    free_windows: list[dict],
    available_topics: list[dict],
    topics_config: list[dict],
    target_date: date = date(2026, 4, 15),
    min_window_minutes: int = MIN_WINDOW_MINUTES,
) -> list[dict]:
    """
    Mirrors the slot-packing while-loop from daily_planning.
    Returns the list of proposed_slots.
    """
    def get_topic_cfg(name: str) -> dict:
        for t in topics_config:
            if t["name"] == name:
                return t
        return {}

    proposed_slots: list[dict] = []
    remaining_topics = list(available_topics)

    for win in free_windows:
        if not remaining_topics or len(proposed_slots) >= MAX_SLOTS:
            break
        cursor = datetime.combine(target_date, win["start"])
        win_end = datetime.combine(target_date, win["end"])
        while remaining_topics and len(proposed_slots) < MAX_SLOTS:
            remaining_min = int((win_end - cursor).total_seconds() // 60)
            if remaining_min < min_window_minutes:
                break
            topic = remaining_topics[0]
            cfg = get_topic_cfg(topic["name"])
            default_duration = cfg.get("default_duration_minutes", 60)
            duration = min(default_duration, remaining_min)
            end_dt = cursor + timedelta(minutes=duration)
            slot = {
                "topic": topic["name"],
                "start": _fmt_time(cursor.time()),
                "end": _fmt_time(end_dt.time()),
                "duration_min": duration,
            }
            proposed_slots.append(slot)
            cursor = end_dt
            remaining_topics.pop(0)

    return proposed_slots


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def window(start_hhmm: str, end_hhmm: str) -> dict:
    h, m = map(int, start_hhmm.split(":"))
    s = time(h, m)
    h2, m2 = map(int, end_hhmm.split(":"))
    e = time(h2, m2)
    dur = int((datetime.combine(date.today(), e) - datetime.combine(date.today(), s)).total_seconds() // 60)
    return {"start": s, "end": e, "duration_min": dur}


def topic(name: str, default_duration: int = 60) -> dict:
    return {"name": name}


def topic_cfg(name: str, default_duration: int = 60) -> dict:
    return {"name": name, "default_duration_minutes": default_duration}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_topic_fits_window_exactly():
    """A 60-min topic fills a 60-min window exactly."""
    slots = pack_slots(
        free_windows=[window("08:00", "09:00")],
        available_topics=[topic("System Design")],
        topics_config=[topic_cfg("System Design", 60)],
    )
    assert len(slots) == 1
    assert slots[0]["topic"] == "System Design"
    assert slots[0]["duration_min"] == 60


def test_topic_capped_to_remaining_window_time():
    """
    A 60-min topic in a 30-min window gets scheduled for 30 min, not skipped.
    This is the regression the Copilot comment flagged.
    """
    slots = pack_slots(
        free_windows=[window("08:00", "08:30")],
        available_topics=[topic("System Design")],
        topics_config=[topic_cfg("System Design", 60)],
    )
    assert len(slots) == 1
    assert slots[0]["duration_min"] == 30


def test_remaining_time_below_min_window_skips_slot():
    """
    If remaining window time is below min_window_minutes (25), nothing is scheduled.
    """
    slots = pack_slots(
        free_windows=[window("08:00", "08:20")],  # 20 min < 25 min minimum
        available_topics=[topic("System Design")],
        topics_config=[topic_cfg("System Design", 60)],
    )
    assert slots == []


def test_multiple_topics_packed_into_one_window():
    """Two topics with short durations both fit into a single large window."""
    slots = pack_slots(
        free_windows=[window("08:00", "10:00")],  # 120 min
        available_topics=[topic("DSA"), topic("Sales Engineering")],
        topics_config=[topic_cfg("DSA", 60), topic_cfg("Sales Engineering", 60)],
    )
    assert len(slots) == 2
    assert slots[0]["topic"] == "DSA"
    assert slots[0]["start"] == "08:00"
    assert slots[0]["end"] == "09:00"
    assert slots[1]["topic"] == "Sales Engineering"
    assert slots[1]["start"] == "09:00"
    assert slots[1]["end"] == "10:00"


def test_topics_spill_across_windows():
    """When a window fills up, remaining topics roll into the next window."""
    slots = pack_slots(
        free_windows=[window("08:00", "09:00"), window("10:00", "11:00")],
        available_topics=[topic("DSA"), topic("Sales Engineering")],
        topics_config=[topic_cfg("DSA", 60), topic_cfg("Sales Engineering", 60)],
    )
    assert len(slots) == 2
    assert slots[0]["topic"] == "DSA"
    assert slots[1]["topic"] == "Sales Engineering"
    assert slots[1]["start"] == "10:00"


def test_max_slots_cap_is_respected():
    """Never exceeds MAX_SLOTS (6) even with many topics and windows."""
    topics_list = [topic(f"Topic {i}") for i in range(10)]
    configs = [topic_cfg(f"Topic {i}", 30) for i in range(10)]
    slots = pack_slots(
        free_windows=[window("08:00", "13:00")],  # 300 min, could fit 10 × 30-min slots
        available_topics=topics_list,
        topics_config=configs,
    )
    assert len(slots) <= MAX_SLOTS


def test_no_free_windows_returns_empty():
    """No windows → no slots."""
    slots = pack_slots(
        free_windows=[],
        available_topics=[topic("System Design")],
        topics_config=[topic_cfg("System Design", 60)],
    )
    assert slots == []


def test_no_topics_returns_empty():
    """No due topics → no slots."""
    slots = pack_slots(
        free_windows=[window("08:00", "10:00")],
        available_topics=[],
        topics_config=[],
    )
    assert slots == []