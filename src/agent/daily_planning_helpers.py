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
from src.agent.planning_helpers import get_prebooked_topics, get_topic_config
from src.core import gap_finder as _gap_finder

if TYPE_CHECKING:
    from src.agent.nodes import AgentState


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


def append_evening_study_window_lines(
    lines: list[str],
    target_date: date,
    events: list[dict],
    timed_events: list[dict],
    due_topics: list[dict],
    config: dict,
    topics_config: dict,
) -> None:
    """Append the evening study-window section.

    Args:
        lines: Mutable list of message lines to append to.
        target_date: Date being previewed.
        events: Raw calendar events for ``target_date``.
        timed_events: Timed subset of ``events``.
        due_topics: Topics returned by SM-2 for the target date.
        config: Runtime config values from ``config.yaml``.
        topics_config: Topic metadata loaded from ``topics.yaml``.
    """
    free_windows = _gap_finder.find_free_windows(events, target_date, config)
    prebooked = get_prebooked_topics(timed_events, due_topics)
    available_topics = [t for t in due_topics if t["name"] not in prebooked]

    if free_windows:
        lines.append("🧠 Study windows:")
        for i, win in enumerate(free_windows):
            topic = available_topics[i] if i < len(available_topics) else None
            if topic is None:
                break
            topic_cfg = get_topic_config(topic["name"], topics_config)
            default_duration = topic_cfg.get("default_duration_minutes", 60)
            duration = min(default_duration, win["duration_min"])
            t_start = format_time(win["start"])
            start_dt = datetime.combine(target_date, win["start"])
            end_dt = start_dt + timedelta(minutes=duration)
            t_end = format_time(end_dt.time())
            lines.append(f"• {t_start}–{t_end} → {topic['name']} ({duration}min)")
    else:
        lines.append("🧠 Study windows: None found for tomorrow")
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
            ef = topic["easiness_factor"]
            lines.append(f"• {i}. {topic['name']} — {label} (EF: {ef})")
        lines.append("")


def pack_mock_slots(
    target_date: date,
    free_windows: list[dict],
    available_topics: list[dict],
    topics_config: dict,
    min_window_minutes: int,
    lines: list[str],
) -> tuple[str | None, dict | None, list[dict]]:
    """Pack due topics into free windows and append resulting ``[Mock]`` lines.

    Args:
        target_date: Date being planned.
        free_windows: Free windows returned by ``gap_finder``.
        available_topics: Due topics not already prebooked.
        topics_config: Topic metadata loaded from ``topics.yaml``.
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
            topic_cfg = get_topic_config(topic["name"], topics_config)
            default_duration = topic_cfg.get("default_duration_minutes", 60)
            duration = min(default_duration, remaining_min)
            end_dt = cursor + timedelta(minutes=duration)
            t_start = format_time(cursor.time())
            t_end = format_time(end_dt.time())
            lines.append(f"• {t_start}–{t_end} [Mock] {topic['name']} ({duration}min)")
            if topic.get("weak_areas"):
                lines.append(f" ⚠️ Focus on: {topic['weak_areas']}")
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
    topics_config: dict,
) -> "AgentState":
    """Build the read-only evening preview state payload.

    Args:
        target_date: Date being previewed.
        events: Raw calendar events for ``target_date``.
        timed_events: Timed subset of ``events``.
        due_topics: Topics returned by SM-2 for the target date.
        config: Runtime config values from ``config.yaml``.
        topics_config: Topic metadata loaded from ``topics.yaml``.

    Returns:
        A partial ``AgentState`` with preview flags and a single composed message.
    """
    day_str = f"{target_date.strftime('%A %B')} {target_date.day}"
    lines = [f"🌙 Tomorrow's plan — {day_str}", ""]
    append_calendar_lines(lines, timed_events, "📅 Your day: No meetings tomorrow")
    append_evening_study_window_lines(
        lines,
        target_date,
        events,
        timed_events,
        due_topics,
        config,
        topics_config,
    )
    append_sm2_pick_lines(lines, due_topics)
    lines.append("No confirmation needed — this is your preview for tomorrow.")
    return {
        "preview_only": True,
        "has_study_plan": False,
        "messages": ["\n".join(lines)],
    }

