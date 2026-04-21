"""Telegram callback handlers — idempotency guards only.

In the HITL pattern, callback payloads are resume values forwarded directly
to the graph via dispatcher.invoke_safe(). This module is responsible only for:
- Rejecting duplicate/repeat taps via message_id deduplication
- Returning the raw callback payload string for forwarding

All routing decisions live in the graph (route_from_router, interrupt()).
"""

import logging

from src.api.telegram import dispatcher

logger = logging.getLogger(__name__)


def handle_callback(callback_data: str, chat_id: int, message_id: int | None) -> str | None:
    """Apply idempotency guard and return callback payload for forwarding.

    Marks message_id as in-flight to block repeat taps, but does NOT call
    mark_confirmed here. Confirmation (or release on failure) happens in
    dispatcher.invoke_safe() after the graph invocation completes, so a
    transient error leaves the message_id retryable.

    Args:
        callback_data: Raw Telegram callback payload.
        chat_id: Telegram chat identifier.
        message_id: Source message id used for repeat-tap deduplication.

    Returns:
        ``callback_data`` when the tap is accepted; ``None`` to suppress duplicates.
    """
    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info(
                "message_id=%s already processed — ignoring repeat tap (chat_id=%s)",
                message_id, chat_id,
            )
            return None

    return callback_data
