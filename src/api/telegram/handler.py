"""Telegram update orchestrator.

This module coordinates update processing without embedding business logic:
1) deduplicate updates,
2) extract normalized payload fields,
3) parse into ``Intent`` or direct response,
4) dispatch to executor-backed graph invocation when required.
"""

import asyncio
import logging
from typing import TypeAlias

from fastapi.responses import JSONResponse

from src.models.telegram import TelegramUpdate
from src.integrations.telegram_client import send_buttons
from src.api.telegram import dispatcher
from src.api.telegram.intent_parser import Intent, parse_callback, parse_message

logger = logging.getLogger(__name__)

ParseResult: TypeAlias = Intent | JSONResponse | None


async def handle_update(update: TelegramUpdate) -> JSONResponse:
    """Process a single Telegram update end-to-end.
    Args:
        update: Parsed Telegram webhook payload.
    Returns:
        ``JSONResponse({'ok': True})`` for handled or ignored updates, or a
        direct response produced by callback/message handlers.
    """
    logger.debug("Webhook update: update_id=%s", update.update_id)

    # --- 1. Deduplication ---
    if dispatcher.is_duplicate(update.update_id):
        logger.info("Duplicate update_id=%s — skipping", update.update_id)
        return JSONResponse({"ok": True})

    # --- 2. Extract fields ---
    chat_id: int | None = None
    message_text: str | None = None
    callback_data: str | None = None
    message_id: int | None = None

    if update.callback_query is not None:
        cq = update.callback_query
        cq_msg = cq.message
        chat_id = cq_msg.chat.id if cq_msg is not None else None
        callback_data = (cq.data or "").strip()
        message_id = cq_msg.message_id if cq_msg is not None else None

    elif update.message is not None:
        msg = update.message
        chat_id = msg.chat.id
        message_text = (msg.text or "").strip()

    if chat_id is None:
        return JSONResponse({"ok": True})

    # --- 3. Parse intent ---
    result: ParseResult = None

    if callback_data is not None:
        cb = callback_data.lower()
        result = parse_callback(cb, callback_data, chat_id, message_id)

    elif message_text:
        logger.debug("Incoming message_text received (length=%d)", len(message_text))
        result = parse_message(message_text, chat_id)

    if result is None:
        return JSONResponse({"ok": True})

    # Direct response (e.g. studied:, /studied) — no graph invocation needed
    if isinstance(result, JSONResponse):
        return result

    intent: Intent = result

    # --- 4. Dispatch ---

    # Skip: message already sent in handle_skip, no graph invocation
    if intent.trigger == "skip":
        return JSONResponse({"ok": True})

    logger.info("Trigger detected: %s, chat_id: %s, extra: %s", intent.trigger, intent.chat_id, intent.extra)

    # Menu: send duration picker directly, no graph needed
    if intent.trigger == "menu":
        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            None,
            lambda: send_buttons("How long do you have?", ["30 min", "45 min", "60 min"]),
        )
        return JSONResponse({"ok": True})

    # Fire-and-forget graph invocation
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        lambda: dispatcher.invoke_safe(intent.trigger, intent.chat_id, **intent.extra),
    )

    return JSONResponse({"ok": True})
