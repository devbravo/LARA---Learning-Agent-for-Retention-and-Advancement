"""Helper functions that assemble daily/evening planning message sections."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from src.agent.formatting import (
    event_duration_min,
    format_event_time,
    format_time,
    topic_due_label,
)
from src.agent.slot_builders import build_in_progress_study_slots, build_missing_study_events, get_prebooked_topics
from src.core import gap_finder as _gap_finder
from src.repositories import topic_repository

if TYPE_CHECKING:
    from src.agent.state import AgentState


def append_calendar_lines(lines: list[str], timed_events: list[dict], empty_label: str) -> None:
    """Append the day calendar section to a message being assembled.

    Args:
        lines: Mutable list of message lines to append to.
        timed_events: Calendar events with explicit ``dateTime`` boundaries.
        empty_label: Fallback line used when no timed events are present.
    """
    if timed_events:
        lines.append("📅 Your day:")
        for ev in timed_events:
            t = format_event_time(ev["start"])
            dur = event_duration_min(ev)
            dur_str = f"{dur}min" if dur else ""
            summary = ev.get("summary", "(No title)")
            lines.append(f"• {t} {summary}{' (' + dur_str + ')' if dur_str else ''}")
    else:
        lines.append(empty_label)
    lines.append("")


def append_in_progress_lines(
    lines: list[str],
    in_progress_topics: list[str],
    timed_events: list[dict],
    target_date: date,
) -> None:
    """Append the in-progress [Study] section to a message being assembled.

    Args:
        lines: Mutable list of message lines to append to.
        in_progress_topics: Topic names currently marked ``in_progress``.
        timed_events: Timed calendar events for the target date.
        target_date: Date being previewed (used for fallback slot times).
    """
    if not in_progress_topics:
        return
    slots = build_in_progress_study_slots(in_progress_topics, timed_events, target_date)
    if slots:
        lines.append("⏳ In Progress:")
        for slot in slots:
            lines.append(
                f"• {slot['start']}–{slot['end']} [Study] {slot['topic']} ({slot['duration_min']}min)"
            )
        lines.append("")


def append_evening_mock_block_lines(
    lines: list[str],
    target_date: date,
    events: list[dict],
    timed_events: list[dict],
    due_topics: list[dict],
    config: dict,
    in_progress_topics: list[str] | None = None,
) -> None:
    """Append the evening mock-interview block section.

    Synthetic [Study] busy events are added for in-progress topics before
    calling the gap finder so that mock blocks never overlap study time.

    Args:
        lines: Mutable list of message lines to append to.
        target_date: Date being previewed.
        events: Raw calendar events for ``target_date``.
        timed_events: Timed subset of ``events``.
        due_topics: Topics returned by SM-2 for the target date.
        config: Runtime config values from ``config.yaml``.
        in_progress_topics: Topic names currently marked ``in_progress``.
    """
    # Include synthetic [Study] busy blocks so mock slots never overlap them
    study_busy = build_missing_study_events(
        in_progress_topics or [], timed_events, target_date, config
    )
    effective_events = events + study_busy

    free_windows = _gap_finder.find_free_windows(effective_events, target_date, config)
    prebooked = get_prebooked_topics(timed_events, due_topics)
    available_topics = [t for t in due_topics if t["name"] not in prebooked]

    min_window_minutes = config.get("min_window_minutes", 25)
    remaining_topics = list(available_topics)

    lines.append("🎯 Mock interview blocks:")
    found_any = False
    for win in free_windows:
        if not remaining_topics:
            break
        cursor = datetime.combine(target_date, win["start"])
        win_end = datetime.combine(target_date, win["end"])
        while remaining_topics:
            remaining_min = int((win_end - cursor).total_seconds() // 60)
            if remaining_min < min_window_minutes:
                break
            topic = remaining_topics[0]
            default_duration = topic_repository.get_default_duration_by_name(topic["name"])
            duration = min(default_duration, remaining_min)
            end_dt = cursor + timedelta(minutes=duration)
            t_start = format_time(cursor.time())
            t_end = format_time(end_dt.time())
            lines.append(f"• {t_start}–{t_end} [Mock] {topic['name']} ({duration}min)")
            found_any = True
            cursor = end_dt
            remaining_topics.pop(0)
    if not found_any:
        lines.append("• None found for tomorrow")
    lines.append("")


def append_sm2_pick_lines(lines: list[str], due_topics: list[dict]) -> None:
    """Append the SM-2 due-topic list section.

    Args:
        lines: Mutable list of message lines to append to.
        due_topics: Ranked due topics from SM-2.
    """
    if due_topics:
        lines.append("📌 SM-2 picks tomorrow:")
        for i, topic in enumerate(due_topics, 1):
            label = topic_due_label(topic)
            lines.append(f"• {i}. {topic['name']} — {label}")
        lines.append("")


def pack_mock_slots(
    target_date: date,
    free_windows: list[dict],
    available_topics: list[dict],
    min_window_minutes: int,
    lines: list[str],
) -> tuple[str | None, dict | None, list[dict]]:
    """Pack due topics into free windows and append resulting ``[Mock]`` lines.

    Args:
        target_date: Date being planned.
        free_windows: Free windows returned by ``gap_finder``.
        available_topics: Due topics not already prebooked.
        min_window_minutes: Minimum remaining window size to schedule a slot.
        lines: Mutable list of message lines to append to.

    Returns:
        A tuple of ``(proposed_topic, proposed_slot, proposed_slots)`` for state.
    """
    proposed_topic = None
    proposed_slot = None
    proposed_slots: list[dict] = []
    max_slots = 6

    if not free_windows:
        lines.append("🎯 Mock interview blocks: None found today")
        lines.append("")
        return proposed_topic, proposed_slot, proposed_slots

    lines.append("🎯 Today's mock interview(s):")
    remaining_topics = list(available_topics)
    for win in free_windows:
        if not remaining_topics or len(proposed_slots) >= max_slots:
            break
        cursor = datetime.combine(target_date, win["start"])
        win_end = datetime.combine(target_date, win["end"])
        while remaining_topics and len(proposed_slots) < max_slots:
            remaining_min = int((win_end - cursor).total_seconds() // 60)
            if remaining_min < min_window_minutes:
                break
            topic = remaining_topics[0]
            default_duration = topic_repository.get_default_duration_by_name(topic["name"])
            duration = min(default_duration, remaining_min)
            end_dt = cursor + timedelta(minutes=duration)
            t_start = format_time(cursor.time())
            t_end = format_time(end_dt.time())
            lines.append(f"• {t_start}–{t_end} [Mock] {topic['name']} ({duration}min)")
            lines.append("")

            slot = {
                "topic": topic["name"],
                "start": t_start,
                "end": t_end,
                "duration_min": duration,
            }
            proposed_slots.append(slot)
            if proposed_topic is None:
                proposed_topic = topic["name"]
                proposed_slot = slot

            cursor = end_dt
            remaining_topics.pop(0)

    return proposed_topic, proposed_slot, proposed_slots


def build_evening_preview_state(
    target_date: date,
    events: list[dict],
    timed_events: list[dict],
    due_topics: list[dict],
    config: dict,
    in_progress_topics: list[str] | None = None,
) -> "AgentState":
    """Build the read-only evening preview state payload.

    Args:
        target_date: Date being previewed.
        events: Raw calendar events for ``target_date``.
        timed_events: Timed subset of ``events``.
        due_topics: Topics returned by SM-2 for the target date.
        config: Runtime config values from ``config.yaml``.
        in_progress_topics: Topic names currently marked ``in_progress``.

    Returns:
        A partial ``AgentState`` with preview flags and a single composed message.
    """
    day_str = f"{target_date.strftime('%A %B')} {target_date.day}"
    lines = [f"🌙 Good Evening Diego — {day_str}", "", f"📋 Tomorrow's plan:", ""]
    append_calendar_lines(lines, timed_events, "📅 Your day: No meetings tomorrow")
    append_in_progress_lines(lines, in_progress_topics or [], timed_events, target_date)
    append_evening_mock_block_lines(
        lines,
        target_date,
        events,
        timed_events,
        due_topics,
        config,
        in_progress_topics=in_progress_topics or [],
    )
    lines.append("No confirmation needed — this is your preview for tomorrow.")
    return {
        "preview_only": True,
        "has_study_plan": False,
        "messages": ["\n".join(lines)],
    }

