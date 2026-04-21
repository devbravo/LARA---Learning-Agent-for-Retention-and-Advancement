"""Telegram webhook HTTP route.

This module defines the transport boundary for incoming Telegram updates:
- validate the webhook secret header,
- parse raw JSON into ``TelegramUpdate``,
- delegate processing to ``handle_update``.
"""

import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.models.telegram import TelegramUpdate
from src.api.telegram.handler import handle_update

router = APIRouter()


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    """Receive and validate a Telegram webhook update.
    Args:
        request: FastAPI request containing the Telegram update payload.
        x_telegram_bot_api_secret_token: Optional Telegram secret token header
            used to authenticate webhook calls.
    Returns:
        JSON response produced by ``handle_update``.
    """
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Invalid secret token")

    raw = await request.json()
    update = TelegramUpdate.model_validate(raw)
    return await handle_update(update)
