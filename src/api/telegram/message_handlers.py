"""Telegram message handlers for command and free-text updates.

This module translates message text into either:
- an ``Intent`` that should be dispatched to the graph,
- a direct ``JSONResponse`` for command flows handled without graph invocation,
- or ``None`` for unrecognized input.
"""

import asyncio
import logging

from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.integrations.telegram_client import send_inline_buttons, send_message
from src.api.telegram.intent_parser import Intent
from src.services import topic_service

logger = logging.getLogger(__name__)


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


def handle_briefing(chat_id: int) -> Intent:
    """Build an intent for the ``/briefing`` command.
    Args:
        chat_id: Telegram chat identifier used as LangGraph thread id.
    Returns:
        Intent with ``trigger='daily'``.
    """
    return Intent(trigger="daily", chat_id=chat_id, message_id=None, extra={})


def handle_study_topic(chat_id: int) -> Intent:
    """Build an intent for the ``/study_topic`` command.
    Args:
        chat_id: Telegram chat identifier used as LangGraph thread id.
    Returns:
        Intent with ``trigger='study_topic'``.
    """
    return Intent(trigger="study_topic", chat_id=chat_id, message_id=None, extra={})


def handle_studied_command(chat_id: int) -> JSONResponse:
    """Handle ``/studied`` by listing in-progress topics as inline buttons.
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
            lambda: send_message("No topics are currently in progress."),
        )
    else:
        buttons = [(t["name"], f"studied:{t['id']}") for t in topics]
        loop.run_in_executor(
            None,
            lambda: send_inline_buttons("Which topic did you just study?", buttons),
        )
    return JSONResponse({"ok": True})


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
