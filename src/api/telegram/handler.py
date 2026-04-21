"""Telegram update orchestrator.

Thin coordinator — no routing decisions, no graph state reads:
1. Deduplicate updates
2. Extract chat_id + payload from callback or message text
3. Handle /help and /view directly (no graph)
4. Forward everything else to dispatcher.invoke_safe(chat_id, payload)
"""

import asyncio
import logging

from fastapi.responses import JSONResponse

from src.models.telegram import TelegramUpdate
from src.api.telegram import dispatcher
from src.api.telegram.intent_parser import extract_payload

logger = logging.getLogger(__name__)


async def handle_update(update: TelegramUpdate) -> JSONResponse:
    """Process a single Telegram update end-to-end."""
    logger.debug("Webhook update: update_id=%s", update.update_id)

    # --- 1. Deduplication ---
    if dispatcher.is_duplicate(update.update_id):
        logger.info("Duplicate update_id=%s — skipping", update.update_id)
        return JSONResponse({"ok": True})

    # --- 2. Extract fields ---
    chat_id: int | None = None
    raw_payload: str | None = None
    message_id: int | None = None

    if update.callback_query is not None:
        cq = update.callback_query
        cq_msg = cq.message
        chat_id = cq_msg.chat.id if cq_msg is not None else None
        raw_payload = (cq.data or "").strip() or None
        message_id = cq_msg.message_id if cq_msg is not None else None

    elif update.message is not None:
        msg = update.message
        chat_id = msg.chat.id
        raw_payload = (msg.text or "").strip() or None

    if chat_id is None or not raw_payload:
        return JSONResponse({"ok": True})

    # --- 3. Extract payload — direct responses for /help and /view ---
    result = extract_payload(raw_payload, chat_id, message_id=message_id)

    if result is None:
        return JSONResponse({"ok": True})

    if isinstance(result, JSONResponse):
        return result

    payload: str = result

    logger.info("Dispatching: chat_id=%s payload=%r message_id=%s", chat_id, payload, message_id)

    # --- 4. Fire-and-forget graph invocation ---
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        lambda: dispatcher.invoke_safe(chat_id, payload, message_id=message_id),
    )

    return JSONResponse({"ok": True})
