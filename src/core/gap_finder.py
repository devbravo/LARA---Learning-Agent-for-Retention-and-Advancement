from datetime import date, datetime, time

MIN_WINDOW_MIN = 25


def _time_to_dt(t_str: str, target_date: date) -> datetime:
    """Convert 'HH:MM' string to a naive datetime on target_date."""
    h, m = map(int, t_str.split(":"))
    return datetime(target_date.year, target_date.month, target_date.day, h, m)


def _parse_event_dt(dt_str: str) -> datetime:
    """Parse ISO-8601 datetime string into a naive datetime (timezone stripped)."""
    dt = datetime.fromisoformat(dt_str)
    return dt.replace(tzinfo=None)


def _subtract_busy(window_start: datetime, window_end: datetime, busy: list[tuple]) -> list[dict]:
    """
    Return free sub-intervals within [window_start, window_end] after subtracting busy intervals.
    Results are filtered to >= MIN_WINDOW_MIN minutes.
    """
    clipped = []
    for s, e in busy:
        s = max(s, window_start)
        e = min(e, window_end)
        if s < e:
            clipped.append((s, e))

    clipped.sort()

    gaps = []
    cursor = window_start
    for s, e in clipped:
        if cursor < s:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < window_end:
        gaps.append((cursor, window_end))

    result = []
    for s, e in gaps:
        dur = int((e - s).total_seconds() / 60)
        if dur >= MIN_WINDOW_MIN:
            result.append({"start": s.time(), "end": e.time(), "duration_min": dur})
    return result


def find_free_windows(
    events: list[dict],
    target_date: date,
    config: dict,
) -> list[dict]:
    """
    Returns free study windows on target_date.

    Rules:
    - Only consider focus_windows from config
    - Protected blocks are treated as busy (safety net if focus windows ever shift)
    - Calendar events overlapping focus windows are subtracted
    - All-day events (no 'start.dateTime') are skipped
    - Minimum gap returned: 25 minutes
    """
    focus_windows = config.get("focus_windows", [])
    protected_blocks = config.get("protected_blocks", [])

    # Build busy intervals from calendar events (skip all-day)
    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        start = event.get("start", {})
        end = event.get("end", {})
        if "dateTime" not in start:
            continue  # all-day event — ignore
        busy.append((_parse_event_dt(start["dateTime"]), _parse_event_dt(end["dateTime"])))

    # Add protected blocks as busy intervals
    for pb in protected_blocks:
        busy.append((_time_to_dt(pb["start"], target_date), _time_to_dt(pb["end"], target_date)))

    result = []
    for fw in focus_windows:
        fw_start = _time_to_dt(fw["start"], target_date)
        fw_end = _time_to_dt(fw["end"], target_date)
        result.extend(_subtract_busy(fw_start, fw_end, busy))
    return result


def find_slot_for_duration(free_windows: list[dict], duration_min: int) -> dict | None:
    """Return the first free window large enough for duration_min. None if no fit."""
    for window in free_windows:
        if window["duration_min"] >= duration_min:
            return window
    return None
