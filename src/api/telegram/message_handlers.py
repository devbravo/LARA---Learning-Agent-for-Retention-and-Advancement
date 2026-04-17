"""
Message handlers — one function per Telegram command / message type.

Each function returns an Intent (for graph dispatch), a JSONResponse
(for direct response with no graph invocation), or None to signal early return.
"""

import asyncio
import logging

from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.integrations.telegram_client import send_inline_buttons, send_message

logger = logging.getLogger(__name__)


def handle_done(chat_id: int):
    """Handle /done command → trigger 'done'."""
    from src.api.telegram.intent_parser import Intent

    return Intent(trigger="done", chat_id=chat_id, message_id=None, extra={})


def handle_study(chat_id: int):
    """Handle /study command → trigger 'on_demand'."""
    from src.api.telegram.intent_parser import Intent

    return Intent(trigger="on_demand", chat_id=chat_id, message_id=None, extra={})


def handle_briefing(chat_id: int):
    """Handle /briefing command → trigger 'daily'."""
    from src.api.telegram.intent_parser import Intent

    return Intent(trigger="daily", chat_id=chat_id, message_id=None, extra={})


def handle_study_topic(chat_id: int):
    """Handle /study_topic command → trigger 'study_topic'."""
    from src.api.telegram.intent_parser import Intent

    return Intent(trigger="study_topic", chat_id=chat_id, message_id=None, extra={})


def handle_studied_command(chat_id: int) -> JSONResponse:
    """
    Handle /studied command — show inline buttons for in-progress topics.
    Returns JSONResponse directly (no graph invocation needed).
    """
    from src.services import topic_service

    topics = topic_service.get_in_progress_topics()
    loop = asyncio.get_event_loop()
    if not topics:
        loop.run_in_executor(
            None,
            lambda: send_message("No topics are currently in progress."),
        )
    else:
        buttons = [(t["name"], f"studied:{t['id']}") for t in topics]
        loop.run_in_executor(
            None,
            lambda: send_inline_buttons("Which topic did you just study?", buttons),
        )
    return JSONResponse({"ok": True})


def handle_weak_areas(message_text: str, chat_id: int):
    """
    Handle free-text reply when awaiting_weak_areas is set → trigger 'weak_areas'.
    Returns None for unrecognized messages.
    """
    from src.api.telegram.intent_parser import Intent

    state = _graph.get_state(chat_id)
    if state.get("awaiting_weak_areas"):
        return Intent(
            trigger="weak_areas",
            chat_id=chat_id,
            message_id=None,
            extra={"messages": [message_text]},
        )
    return None
