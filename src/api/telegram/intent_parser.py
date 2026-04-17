"""Intent parsing utilities for Telegram webhook updates.

This module converts normalized callback/message input into one of:
- ``Intent`` (graph dispatch),
- ``JSONResponse`` (direct response path),
- ``None`` (unknown/ignored input).
"""

from dataclasses import dataclass, field
from typing import Any, TypeAlias

from fastapi.responses import JSONResponse


@dataclass
class Intent:
    """Dispatch envelope for graph invocations.
    Attributes:
        trigger: Graph trigger name.
        chat_id: Telegram chat id used as LangGraph thread id.
        message_id: Source Telegram message id when available.
        extra: Additional partial state passed to graph invocation.
    """
    trigger: str
    chat_id: int
    message_id: int | None
    extra: dict[str, Any] = field(default_factory=dict)


ParseResult: TypeAlias = Intent | JSONResponse | None


def parse_callback(
    cb: str,
    callback_data: str,
    chat_id: int,
    message_id: int | None,
) -> ParseResult:
    """Parse callback data into a dispatchable result.
    Args:
        cb: Lowercased callback text used for branch comparisons.
        callback_data: Original callback payload preserving case.
        chat_id: Telegram chat identifier.
        message_id: Telegram message id associated with callback buttons.
    Returns:
        ``Intent`` or ``JSONResponse`` for recognized callbacks, else ``None``.
    """
    from src.api.telegram import callback_handlers

    if cb in ("30 min", "45 min", "60 min"):
        return callback_handlers.handle_duration(cb, chat_id, message_id)
    elif cb in ("yes, book them", "confirm"):
        return callback_handlers.handle_confirm(cb, chat_id, message_id)
    elif cb == "skip":
        return callback_handlers.handle_skip(chat_id, message_id)
    elif cb in ("😕 hard", "😐 ok", "😊 easy"):
        return callback_handlers.handle_rating(cb, chat_id, message_id)
    elif cb.startswith("category:"):
        return callback_handlers.handle_category(callback_data, chat_id)
    elif cb.startswith("subtopic_id:"):
        return callback_handlers.handle_subtopic_id(callback_data, chat_id, message_id)
    elif cb.startswith("studied:"):
        return callback_handlers.handle_studied(callback_data, chat_id, message_id)
    else:
        return None


def parse_message(message_text: str, chat_id: int) -> ParseResult:
    """Parse incoming text message into a dispatchable result.
    Args:
        message_text: Raw Telegram text payload.
        chat_id: Telegram chat identifier.
    Returns:
        ``Intent`` or ``JSONResponse`` for recognized commands/replies,
        otherwise ``None``.
    """
    from src.api.telegram import message_handlers

    text_lower = message_text.strip().lower()

    if text_lower in ("/done", "done"):
        return message_handlers.handle_done(chat_id)
    elif text_lower == "/study":
        return message_handlers.handle_study(chat_id)
    elif text_lower == "/briefing":
        return message_handlers.handle_briefing(chat_id)
    elif text_lower == "/study_topic":
        return message_handlers.handle_study_topic(chat_id)
    elif text_lower == "/studied":
        return message_handlers.handle_studied_command(chat_id)
    else:
        return message_handlers.handle_weak_areas(message_text, chat_id)
