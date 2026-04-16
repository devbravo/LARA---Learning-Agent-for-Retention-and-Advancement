import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.schemas.telegram import TelegramUpdate
from src.webhook_handler import handle_update

router = APIRouter()


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Invalid secret token")

    raw = await request.json()
    update = TelegramUpdate.model_validate(raw)
    return await handle_update(update)
