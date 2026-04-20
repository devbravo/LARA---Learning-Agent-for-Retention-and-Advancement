"""Telegram message handlers for command and free-text updates.

This module translates message text into either:
- an ``Intent`` that should be dispatched to the graph,
- a direct ``JSONResponse`` for command flows handled without graph invocation,
- or ``None`` for unrecognized input.
"""

import asyncio
import logging
from datetime import date

from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.integrations.telegram_client import send_inline_buttons, send_message
from src.api.telegram.types import Intent
from src.services import topic_service
from src.services import view_service

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 Here is what I can do:\n\n"
    "/study - Generate a study brief for the highest-priority due topic\n"
    "/done - Log completed study sessions and rate how they went\n"
    "/plan - Generate today's study plan\n"
    "/pick - Choose a specific topic to start studying\n"
    "/activate - Show in-progress topics and move one into active review\n"
    "/view - Show overdue, due today, and in-progress topics at a glance\n"
    "/help - Show this command guide\n\n"
    "Notes:\n"
    "- After /done, your next text reply is treated as weak areas notes\n"
    "- Booking mock sessions always requires confirmation"
)


def handle_done(chat_id: int) -> Intent:
    """Build an intent for the ``/done`` command.
    Args:
        chat_id: Telegram chat identifier used as LangGraph thread id.
    Returns:
        Intent with ``trigger='done'``.
    """
    return Intent(trigger="done", chat_id=chat_id, message_id=None, extra={})


def handle_study(chat_id: int) -> Intent:
    """Build an intent for the ``/study`` command.
    Args:
        chat_id: Telegram chat identifier used as LangGraph thread id.
    Returns:
        Intent with ``trigger='on_demand'``.
    """
    return Intent(trigger="on_demand", chat_id=chat_id, message_id=None, extra={})


def handle_daily(chat_id: int) -> Intent:
    """Build an intent for the ``/plan`` command.
    Args:
        chat_id: Telegram chat identifier used as LangGraph thread id.
    Returns:
        Intent with ``trigger='daily'``.
    """
    return Intent(trigger="daily", chat_id=chat_id, message_id=None, extra={})


def handle_study_topic(chat_id: int) -> Intent:
    """Build an intent for the ``/pick`` command.
    Args:
        chat_id: Telegram chat identifier used as LangGraph thread id.
    Returns:
        Intent with ``trigger='study_topic'``.
    """
    return Intent(trigger="study_topic", chat_id=chat_id, message_id=None, extra={})


def handle_studied_command(chat_id: int) -> JSONResponse:
    """Handle ``/activate`` by listing in-progress topics as inline buttons.
    This command is handled directly from the webhook path without graph
    invocation.
    Args:
        chat_id: Telegram chat identifier (currently unused by this handler,
            included for interface consistency).
    Returns:
        ``JSONResponse({'ok': True})`` after scheduling the outbound Telegram
        message/button send.
    """
    topics = topic_service.get_in_progress_topics()
    loop = asyncio.get_event_loop()
    if not topics:
        loop.run_in_executor(
            None,
            send_message,
            "No topics are currently in progress.",
        )
    else:
        buttons = [(t["name"], f"studied:{t['id']}") for t in topics]
        loop.run_in_executor(
            None,
            send_inline_buttons,
            "Which topic did you just study?",
            buttons,
        )
    return JSONResponse({"ok": True})


def handle_help_command(chat_id: int) -> JSONResponse:
    """Handle ``/help`` by sending a concise command guide.

    This command is handled directly from the webhook path without graph
    invocation.

    Args:
        chat_id: Telegram chat identifier (unused, kept for handler consistency).

    Returns:
        ``JSONResponse({'ok': True})`` after scheduling the help message send.
    """
    _ = chat_id
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, send_message, HELP_TEXT)
    return JSONResponse({"ok": True})


def handle_view_command(chat_id: int) -> JSONResponse:
    """Handle ``/view`` by sending a read-only study snapshot.

    This command is handled directly from the webhook path without graph
    invocation. No DB writes or calendar calls are made.

    Args:
        chat_id: Telegram chat identifier (unused, kept for handler consistency).

    Returns:
        ``JSONResponse({'ok': True})`` after scheduling the snapshot message.
    """
    _ = chat_id
    try:
        snapshot = view_service.get_study_snapshot()
        msg = _format_snapshot(snapshot, date.today())
    except Exception as exc:
        logger.exception("Error fetching study snapshot: %s", exc)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, send_message, "⚠️ Could not load study snapshot. Try again later.")
        return JSONResponse({"ok": True})

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, send_message, msg)
    return JSONResponse({"ok": True})


def _format_snapshot(snapshot: dict, today: date) -> str:
    """Format a study snapshot dict as a Telegram message string."""
    day_str = today.strftime("%A %B ") + str(today.day)
    lines = [f"📊 Your study snapshot — {day_str}"]

    overdue = snapshot["overdue"]
    due_today = snapshot["due_today"]
    in_progress = snapshot["in_progress"]

    if not overdue and not due_today and not in_progress:
        return "🎉 Nothing due and no topics in progress."

    if overdue:
        lines.append("\n⚠️ Overdue:")
        for t in overdue:
            line = f"• {t['name']} ({t['days_overdue']}d)"
            if t["weak_areas"]:
                line += f" — focus: {t['weak_areas']}"
            lines.append(line)

    if due_today:
        lines.append("\n🎯 Due today:")
        for t in due_today:
            line = f"• {t['name']}"
            if t["weak_areas"]:
                line += f" — focus: {t['weak_areas']}"
            lines.append(line)

    if in_progress:
        lines.append("\n⏳ In Progress:")
        for t in in_progress:
            line = f"• {t['name']}"
            if t["weak_areas"]:
                line += f" — focus: {t['weak_areas']}"
            lines.append(line)

    return "\n".join(lines)


def handle_weak_areas(message_text: str, chat_id: int) -> Intent | None:
    """Convert free text into a ``weak_areas`` intent when expected.
    Args:
        message_text: Raw user text from Telegram.
        chat_id: Telegram chat identifier used to inspect checkpointed state.
    Returns:
        Intent with ``trigger='weak_areas'`` when the current state expects a
        weak-areas reply; otherwise ``None``.
    """
    state = _graph.get_state(chat_id)
    if state.get("awaiting_weak_areas"):
        return Intent(
            trigger="weak_areas",
            chat_id=chat_id,
            message_id=None,
            extra={"messages": [message_text]},
        )
    return None
