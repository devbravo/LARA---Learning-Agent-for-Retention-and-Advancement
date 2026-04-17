"""Formatting and datetime utilities for agent planning and messaging.

These helpers are intentionally pure and reusable across nodes, planning
helpers, and debug tooling.
"""

from datetime import date, datetime


def format_time(t) -> str:
    """Return a value formatted as ``HH:MM``.

    Args:
        t: A ``datetime.time``-like object or a string containing an ``HH:MM``
            prefix.

    Returns:
        A zero-padded ``HH:MM`` string.
    """
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M")
    return str(t)[:5]



def format_event_time(dt_dict: dict) -> str:
    """Return a readable clock time for a Google Calendar time dictionary.

    Args:
        dt_dict: Event ``start`` or ``end`` dictionary from Google Calendar.

    Returns:
        ``HH:MM`` for timed events, or ``"all-day"`` when no ``dateTime`` is
        present.
    """
    if "dateTime" in dt_dict:
        dt = datetime.fromisoformat(dt_dict["dateTime"]).replace(tzinfo=None)
        return dt.strftime("%H:%M")
    return "all-day"



def event_duration_min(event: dict) -> int:
    """Compute an event duration in minutes.

    Args:
        event: Normalized calendar event dictionary containing timed ``start``
            and ``end`` entries.

    Returns:
        Duration in whole minutes, or ``0`` when the event cannot be parsed.
    """
    try:
        s = datetime.fromisoformat(event["start"]["dateTime"]).replace(tzinfo=None)
        e = datetime.fromisoformat(event["end"]["dateTime"]).replace(tzinfo=None)
        return int((e - s).total_seconds() / 60)
    except Exception:
        return 0



def topic_due_label(topic: dict) -> str:
    """Return a human-friendly due label for a topic.

    Args:
        topic: Topic dictionary with a ``next_review`` ISO date.

    Returns:
        A label such as ``"due"``, ``"due tomorrow"``, or ``"due in Nd"``.
        Falls back to ``"due"`` when parsing fails.
    """
    try:
        nr = date.fromisoformat(topic["next_review"])
        delta = (nr - date.today()).days
        if delta <= 0:
            return "due"
        if delta == 1:
            return "due tomorrow"
        return f"due in {delta}d"
    except Exception:
        return "due"



def timezone_offset_str(tz) -> str:
    """Return a timezone offset formatted as ``±HH:MM``.

    Args:
        tz: A timezone object compatible with ``datetime.now(tz)``.

    Returns:
        Offset text suitable for appending to ISO datetime strings.
    """
    offset = datetime.now(tz).strftime("%z")
    return f"{offset[:3]}:{offset[3:]}"



def local_datetime_str(target_date: date, hour: int, minute: int, tz) -> str:
    """Build a local ISO datetime string for a date/time pair.

    Args:
        target_date: Date portion of the resulting timestamp.
        hour: Hour in 24-hour time.
        minute: Minute value.
        tz: Timezone object used to derive the numeric offset.

    Returns:
        An ISO-like datetime string including the timezone offset, such as
        ``2026-04-17T08:00:00+01:00``.
    """
    return f"{target_date.isoformat()}T{hour:02d}:{minute:02d}:00{timezone_offset_str(tz)}"


