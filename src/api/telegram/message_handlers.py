"""Telegram message handlers — /help and /view direct responses.

All other commands are forwarded to the graph via dispatcher.invoke_safe().
/help and /view are the only commands handled outside the graph.
"""

import asyncio
import logging
from datetime import date

from fastapi.responses import JSONResponse

from src.integrations.telegram_client import send_message
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


def handle_help_command(chat_id: int) -> JSONResponse:
    """Handle /help by sending a concise command guide directly."""
    _ = chat_id
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, send_message, HELP_TEXT)
    return JSONResponse({"ok": True})


def handle_view_command(chat_id: int) -> JSONResponse:
    """Handle /view by sending a read-only study snapshot directly."""
    _ = chat_id
    try:
        today = date.today()
        snapshot = view_service.get_study_snapshot()
        msg = _format_snapshot(snapshot, today)
    except Exception as exc:
        logger.exception("Error fetching study snapshot: %s", exc)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, send_message, "⚠️ Could not load study snapshot. Try again later.")
        return JSONResponse({"ok": True})

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, send_message, msg)
    return JSONResponse({"ok": True})


def _format_snapshot(snapshot: dict, today: date) -> str:
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
