"""
Intent parser — pure parsing from Telegram update fields to Intent objects.

Intent is defined here. callback_handlers and message_handlers are imported
lazily inside functions to break the circular dependency
(they import Intent from this module).
"""

from dataclasses import dataclass, field
from src.api.telegram import callback_handlers, message_handlers


@dataclass
class Intent:
    trigger: str
    chat_id: int
    message_id: int | None
    extra: dict = field(default_factory=dict)


def parse_callback(cb: str, callback_data: str, chat_id: int, message_id: int | None):
    """
    Parse callback_data into an Intent or JSONResponse.
    Returns None for unknown callbacks.

    cb          — callback_data.lower() (for comparisons)
    callback_data — original case (for extracting values that may be case-sensitive)
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
        return callback_handlers.handle_category(callback_data, chat_id)
    elif cb.startswith("subtopic_id:"):
        return callback_handlers.handle_subtopic_id(callback_data, chat_id, message_id)
    elif cb.startswith("studied:"):
        return callback_handlers.handle_studied(callback_data, chat_id, message_id)
    else:
        return None


def parse_message(message_text: str, chat_id: int):
    """
    Parse message text into an Intent or JSONResponse.
    Returns None for unrecognized messages.
    """
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
