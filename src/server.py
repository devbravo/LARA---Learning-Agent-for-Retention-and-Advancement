"""
FastAPI webhook server for the Learning Manager agent.

Endpoints:
  GET  /health   — uptime check
  POST /webhook  — Telegram update receiver
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env", override=True)

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.integrations.telegram import send_buttons

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Learning Manager", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # --- Auth ---
    expected = os.environ.get("WEBHOOK_SECRET", "")
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Invalid secret token")

    update = await request.json()
    logger.debug("Webhook update: %s", update)

    # --- Extract chat_id, text, and callback data ---
    chat_id: int | None = None
    message_text: str | None = None
    callback_data: str | None = None

    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        callback_data = cq.get("data", "").strip()

    elif "message" in update:
        msg = update["message"]
        chat_id = msg.get("chat", {}).get("id")
        message_text = (msg.get("text") or "").strip()

    if chat_id is None:
        # Not a message or callback we handle
        return JSONResponse({"ok": True})

    # --- Intent detection ---
    trigger: str | None = None
    extra: dict = {}

    if callback_data is not None:
        cb = callback_data.lower()
        if cb in ("30 min", "60 min", "90 min"):
            trigger = "study_picker"
            extra["duration_min"] = int(callback_data.replace(" min", ""))
        elif cb in ("yes, book them", "confirm"):
            trigger = "confirm"
        elif cb == "skip":
            trigger = "skip"
        else:
            # Unknown callback — ignore
            return JSONResponse({"ok": True})

    elif message_text:
        if message_text.startswith("📋 Session summary"):
            trigger = "done"
            extra["messages"] = [message_text]
        else:
            # Unrecognised text — show duration menu
            trigger = "menu"

    if trigger is None or trigger == "skip":
        # skip is a no-op
        return JSONResponse({"ok": True})

    logger.info("Trigger detected: %s, chat_id: %s, extra: %s", trigger, chat_id, extra)

    # --- Menu: send duration picker directly, no graph needed ---
    if trigger == "menu":
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            None,
            lambda: send_buttons("How long do you have?", ["30 min", "60 min", "90 min"]),
        )
        return JSONResponse({"ok": True})

    # --- Invoke graph (fire-and-forget in background) ---
    import asyncio

    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        lambda: _invoke_safe(trigger, chat_id, **extra),
    )

    return JSONResponse({"ok": True})


def _invoke_safe(trigger: str, chat_id: int, **kwargs) -> None:
    """Invoke the graph, catching all exceptions so executor threads never crash."""
    try:
        logger.info("Invoking graph: trigger=%s, chat_id=%s", trigger, chat_id)
        _graph.invoke(trigger=trigger, chat_id=chat_id, **kwargs)
        logger.info("Graph invocation complete: trigger=%s", trigger)
    except Exception as e:
        logger.error(
            "Graph invocation failed [trigger=%s chat_id=%s]: %s",
            trigger, chat_id, e, exc_info=True,
        )
