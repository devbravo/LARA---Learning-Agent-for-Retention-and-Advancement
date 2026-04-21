"""Planning helpers for study-event matching, synthetic busy blocks, and rebooking."""

from datetime import date, datetime, time, timedelta
from typing import Any

import logging
import pytz

from src.integrations import gcal as _gcal
from src.agent.formatting import event_duration_min, format_event_time, local_datetime_str

logger = logging.getLogger(__name__)


def _default_study_slot_datetimes(target_date: date, slot_index: int) -> tuple[datetime, datetime] | None:
    """Return the default study-slot datetimes for a given slot index.

    Slot ``0`` starts at 08:00 on ``target_date`` and each subsequent slot is one
    hour later. Returns ``None`` once the next slot would start on a later day.
    """
    start_dt = datetime.combine(target_date, time(hour=8)) + timedelta(hours=slot_index)
    if start_dt.date() != target_date:
        return None
    return start_dt, start_dt + timedelta(hours=1)



def is_topic_in_summary(topic_name: str, summary: str) -> bool:
    """Return whether a topic name is represented by a calendar summary.

    The match is intentionally fuzzy around ``and``/``&`` to better align topic
    names with manually or previously generated event titles.
    """
    norm_topic = topic_name.lower().replace(" and ", " & ")
    norm_summary = summary.lower().replace(" and ", " & ")
    return norm_topic in norm_summary or norm_summary in norm_topic



def get_topic_config(topic_name: str, config: dict) -> dict:
    """Return the configuration entry for a topic name.

    Args:
        topic_name: Topic display name.
        config: Parsed ``topics.yaml`` content.

    Returns:
        The matching topic configuration dictionary, or an empty dict when the
        topic is not configured.
    """
    for t in config.get("topics", []):
        if t["name"] == topic_name:
            return t
    return {}



def get_prebooked_topics(events: list, due_topics: list) -> set:
    """Return due topic names that already appear on the calendar.

    Args:
        events: Calendar events for the target day.
        due_topics: Due-topic rows returned by the SM-2 layer.

    Returns:
        A set of topic names already represented in the day's events.
    """
    prebooked = set()
    for topic in due_topics:
        for ev in events:
            raw_summary = ev.get("summary") or ""
            if not raw_summary:
                continue
            if is_topic_in_summary(topic["name"], raw_summary):
                prebooked.add(topic["name"])
                break
    return prebooked



def rebook_study_events(
    in_progress_topics: list[str], timed_events: list[dict[str, Any]], target_date: date, config: dict
) -> None:
    """Create missing default ``[Study]`` events for in-progress topics.

    Args:
        in_progress_topics: Topic names currently marked ``in_progress``.
        timed_events: Existing timed calendar events on the target date.
        target_date: Date being planned.
        config: Parsed application configuration including timezone.
    """
    tz = pytz.timezone(config["timezone"])

    for slot_index, topic_name in enumerate(in_progress_topics):
        slot_range = _default_study_slot_datetimes(target_date, slot_index)
        if slot_range is None:
            logger.warning("Skipping [Study] rebooking for %s — no valid slot remains on %s", topic_name, target_date)
            break

        already_booked = any(
            is_topic_in_summary(topic_name, ev.get("summary", ""))
            for ev in timed_events
        )
        if not already_booked:
            try:
                start_dt, end_dt = slot_range
                start = local_datetime_str(start_dt.date(), start_dt.hour, start_dt.minute, tz)
                end = local_datetime_str(end_dt.date(), end_dt.hour, end_dt.minute, tz)
                _gcal.write_study_event(
                    topic=topic_name,
                    start=start,
                    end=end,
                )
            except Exception as e:
                logger.warning("Failed to rebook [Study] for %s: %s", topic_name, e)



def build_missing_study_events(
    in_progress_topics: list[str], timed_events: list[dict[str, Any]], target_date: date, config: dict
) -> list[dict[str, Any]]:
    """Return synthetic [Study] events only for in-progress topics not already booked.

    Mirrors ``rebook_study_events()`` so planning uses the same default 08:00+
    schedule that would be written to calendar later, without double-blocking
    topics that already have a real event today.

    Args:
        in_progress_topics: Topic names currently marked ``in_progress``.
        timed_events: Existing timed calendar events on the target date.
        target_date: Date being planned.
        config: Parsed application configuration including timezone.

    Returns:
        Synthetic normalized calendar events representing only the missing
        default study slots.
    """
    tz = pytz.timezone(config["timezone"])

    synthetic_events: list[dict[str, Any]] = []

    for slot_index, topic_name in enumerate(in_progress_topics):
        slot_range = _default_study_slot_datetimes(target_date, slot_index)
        if slot_range is None:
            logger.warning("Skipping synthetic [Study] busy event for %s — no valid slot remains on %s", topic_name, target_date)
            break

        already_booked = any(
            is_topic_in_summary(topic_name, ev.get("summary", ""))
            for ev in timed_events
        )
        if not already_booked:
            start_dt, end_dt = slot_range
            synthetic_events.append(
                {
                    "summary": f"[Study] {topic_name}",
                    "start": {"dateTime": local_datetime_str(start_dt.date(), start_dt.hour, start_dt.minute, tz)},
                    "end": {"dateTime": local_datetime_str(end_dt.date(), end_dt.hour, end_dt.minute, tz)},
                }
            )

    return synthetic_events



def build_in_progress_study_slots(
    in_progress_topics: list[str], timed_events: list[dict[str, Any]], target_date: date
) -> list[dict[str, Any]]:
    """Return display-ready study slots using real booked times when available.

    For topics without a real timed ``[Study]`` event, fall back to the default
    08:00+ one-hour sequence used by ``rebook_study_events()``.

    Args:
        in_progress_topics: Topic names currently marked ``in_progress``.
        timed_events: Existing timed calendar events on the target date.
        target_date: Date being planned, used to cap fallback default slots.

    Returns:
        A chronologically sorted list of display-ready study-slot dictionaries.
    """
    slots: list[dict[str, Any]] = []
    for slot_index, topic_name in enumerate(in_progress_topics):
        booked_event = next(
            (
                ev for ev in timed_events
                if (ev.get("summary") or "").lower().startswith("[study]")
                and is_topic_in_summary(topic_name, ev.get("summary", ""))
            ),
            None,
        )

        if booked_event is not None:
            start = format_event_time(booked_event["start"])
            end = format_event_time(booked_event["end"])
            duration_min = event_duration_min(booked_event) or 60
        else:
            slot_range = _default_study_slot_datetimes(target_date, slot_index)
            if slot_range is None:
                logger.warning("Skipping in-progress display slot for %s — no valid slot remains on %s", topic_name, target_date)
                break
            start_dt, end_dt = slot_range
            start = format_event_time({"dateTime": start_dt.isoformat()})
            end = format_event_time({"dateTime": end_dt.isoformat()})
            duration_min = 60

        slots.append(
            {
                "topic": topic_name,
                "start": start,
                "end": end,
                "duration_min": duration_min,
            }
        )

    return sorted(slots, key=lambda slot: slot["start"])




