import pytest
from datetime import date
from src.core.gap_finder import find_free_windows, find_slot_for_duration

TARGET_DATE = date(2026, 4, 3)

CONFIG = {
    "focus_windows": [
        {"start": "08:00", "end": "11:00"},
        {"start": "11:00", "end": "13:00"},
    ],
    "protected_blocks": [
        {"start": "15:00", "end": "19:30"},
    ],
}


def timed_event(start_hhmm: str, end_hhmm: str) -> dict:
    """Build a calendar event dict with dateTime fields."""
    d = TARGET_DATE.isoformat()
    return {
        "summary": "Test event",
        "start": {"dateTime": f"{d}T{start_hhmm}:00"},
        "end": {"dateTime": f"{d}T{end_hhmm}:00"},
    }


def all_day_event() -> dict:
    """Build an all-day calendar event (no dateTime field)."""
    return {
        "summary": "All-day event",
        "start": {"date": TARGET_DATE.isoformat()},
        "end": {"date": TARGET_DATE.isoformat()},
    }


# ---------------------------------------------------------------------------
# find_free_windows
# ---------------------------------------------------------------------------

def test_no_events_returns_both_full_focus_windows():
    """With no events, both focus windows are returned at full size."""
    windows = find_free_windows([], TARGET_DATE, CONFIG)
    assert len(windows) == 2
    durations = sorted(w["duration_min"] for w in windows)
    assert durations == [120, 180]  # 08-11 = 180 min, 11-13 = 120 min


def test_event_blocking_middle_of_focus_window_splits_it():
    """An event in the middle of a focus window creates two smaller gaps."""
    events = [timed_event("09:00", "10:00")]
    windows = find_free_windows(events, TARGET_DATE, CONFIG)
    # 08:00-09:00 (60 min), 10:00-11:00 (60 min), 11:00-13:00 (120 min)
    assert len(windows) == 3
    durations = sorted(w["duration_min"] for w in windows)
    assert durations == [60, 60, 120]


def test_event_at_start_of_focus_window_trims_it():
    """An event at the start of a window trims it from the front."""
    events = [timed_event("08:00", "09:30")]
    windows = find_free_windows(events, TARGET_DATE, CONFIG)
    # 09:30-11:00 (90 min) + 11:00-13:00 (120 min)
    assert len(windows) == 2
    durations = sorted(w["duration_min"] for w in windows)
    assert durations == [90, 120]


def test_event_during_protected_block_does_not_affect_focus_windows():
    """An event inside the protected block (15:00-19:30) leaves focus windows untouched."""
    events = [timed_event("15:30", "17:00")]
    windows = find_free_windows(events, TARGET_DATE, CONFIG)
    assert len(windows) == 2
    durations = sorted(w["duration_min"] for w in windows)
    assert durations == [120, 180]


def test_all_day_event_is_handled_without_crash():
    """All-day events (no dateTime) are silently skipped; full windows returned."""
    events = [all_day_event()]
    windows = find_free_windows(events, TARGET_DATE, CONFIG)
    assert len(windows) == 2
    durations = sorted(w["duration_min"] for w in windows)
    assert durations == [120, 180]


def test_event_filling_entire_focus_window_removes_it():
    """An event that covers an entire focus window removes that window entirely."""
    events = [timed_event("08:00", "11:00")]
    windows = find_free_windows(events, TARGET_DATE, CONFIG)
    # Only the 11:00-13:00 window survives
    assert len(windows) == 1
    assert windows[0]["duration_min"] == 120


def test_small_remaining_gap_below_minimum_is_excluded():
    """Gaps smaller than 25 minutes are not returned."""
    # Leave only a 20-minute gap in the first window
    events = [timed_event("08:00", "10:40"), timed_event("11:00", "13:00")]
    windows = find_free_windows(events, TARGET_DATE, CONFIG)
    # 10:40-11:00 = 20 min → excluded; second window fully blocked
    assert all(w["duration_min"] >= 25 for w in windows)


# ---------------------------------------------------------------------------
# find_slot_for_duration
# ---------------------------------------------------------------------------

def test_find_slot_returns_none_when_no_window_fits():
    """Returns None when every window is smaller than requested duration."""
    windows = [{"start": None, "end": None, "duration_min": 20}]
    assert find_slot_for_duration(windows, 30) is None


def test_find_slot_returns_first_fitting_window():
    """Returns the first window that fits, not the largest."""
    windows = [
        {"start": None, "end": None, "duration_min": 60},
        {"start": None, "end": None, "duration_min": 90},
    ]
    result = find_slot_for_duration(windows, 60)
    assert result is not None
    assert result["duration_min"] == 60


def test_find_slot_returns_none_on_empty_list():
    """Empty window list → None."""
    assert find_slot_for_duration([], 30) is None


def test_find_slot_exact_fit():
    """A window exactly equal to duration_min is accepted."""
    windows = [{"start": None, "end": None, "duration_min": 45}]
    result = find_slot_for_duration(windows, 45)
    assert result is not None
    assert result["duration_min"] == 45
