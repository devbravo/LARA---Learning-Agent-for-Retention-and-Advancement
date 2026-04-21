"""Payload extraction for Telegram webhook updates.

Converts raw callback data or message text into one of:
- ``str``          — payload to forward to dispatcher.invoke_safe()
- ``JSONResponse`` — direct response (/help, /view)
- ``None``         — unknown or suppressed input

All routing decisions happen in the graph, not here.
"""

from fastapi.responses import JSONResponse

from src.api.telegram import callback_handlers, message_handlers
from src.api.telegram.types import ParseResult

__all__ = ["extract_payload", "ParseResult"]


def extract_payload(
    raw_payload: str,
    chat_id: int,
    message_id: int | None = None,
) -> str | JSONResponse | None:
    """Normalise a Telegram payload for graph dispatch.

    Args:
        raw_payload: Raw text from a message or callback button.
        chat_id: Telegram chat identifier.
        message_id: Source message id (only provided for callback buttons).

    Returns:
        Payload string when the graph should be invoked, ``JSONResponse`` for
        direct responses, or ``None`` to ignore the update.
    """
    text_lower = raw_payload.strip().lower()

    # Callback button — apply idempotency guard
    if message_id is not None:
        return callback_handlers.handle_callback(raw_payload, chat_id, message_id)

    # Direct-response commands (never touch the graph)
    if text_lower == "/help":
        return message_handlers.handle_help_command(chat_id)
    if text_lower == "/view":
        return message_handlers.handle_view_command(chat_id)

    # Everything else is forwarded to the graph
    if raw_payload.strip():
        return raw_payload.strip()

    return None


# ---------------------------------------------------------------------------
# Legacy shims — kept so existing imports don't break during transition
# ---------------------------------------------------------------------------

def parse_callback(
    cb: str,
    callback_data: str,
    chat_id: int,
    message_id: int | None,
) -> ParseResult:
    """Deprecated shim — use extract_payload instead."""
    return extract_payload(callback_data, chat_id, message_id=message_id)


def parse_message(message_text: str, chat_id: int) -> ParseResult:
    """Deprecated shim — use extract_payload instead."""
    return extract_payload(message_text, chat_id, message_id=None)
