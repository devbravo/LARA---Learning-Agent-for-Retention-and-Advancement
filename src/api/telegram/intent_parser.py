"""Intent parsing utilities for Telegram webhook updates.

This module converts normalized callback/message input into one of:
- ``Intent`` (graph dispatch),
- ``JSONResponse`` (direct response path),
- ``None`` (unknown/ignored input).
"""

from fastapi.responses import JSONResponse

from src.api.telegram import callback_handlers, message_handlers
from src.api.telegram.types import Intent, ParseResult

__all__ = ["Intent", "ParseResult", "parse_callback", "parse_message"]


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
    if cb in ("30 min", "45 min", "60 min"):
        return callback_handlers.handle_duration(cb, chat_id, message_id)
    elif cb in ("yes, book them", "confirm"):
        return callback_handlers.handle_confirm(cb, chat_id, message_id)
    elif cb == "skip":
        return callback_handlers.handle_skip(chat_id, message_id)
    elif cb in ("😕 hard", "😐 ok", "😊 easy"):
        return callback_handlers.handle_rating(cb, chat_id, message_id)
    elif cb.startswith("category:"):
        return callback_handlers.handle_category(callback_data, chat_id, message_id)
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
    text_lower = message_text.strip().lower()

    if text_lower == "/done":
        return message_handlers.handle_done(chat_id)
    elif text_lower == "/study":
        return message_handlers.handle_study(chat_id)
    elif text_lower == "/plan":
        return message_handlers.handle_daily(chat_id)
    elif text_lower == "/pick":
        return message_handlers.handle_study_topic(chat_id)
    elif text_lower == "/activate":
        return message_handlers.handle_studied_command(chat_id)
    elif text_lower == "/help":
        return message_handlers.handle_help_command(chat_id)
    else:
        return message_handlers.handle_weak_areas(message_text, chat_id)
