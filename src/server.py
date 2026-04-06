"""
FastAPI webhook server for the Learning Manager agent.

Endpoints:
  GET  /health   — uptime check
  POST /webhook  — Telegram update receiver
"""

import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env", override=True)

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.integrations.telegram_client import send_buttons, send_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Learning Manager", docs_url=None, redoc_url=None)

# Deduplication guard — keeps last 1000 processed update_ids in memory
_processed_updates: set[int] = set()
_MAX_PROCESSED = 1000

# Confirmed bookings — tracks message_ids that have already been booked
# Prevents double-booking when the user taps "Yes, book them" more than once
_confirmed_message_ids: set[int] = set()
_in_flight_message_ids: set[int] = set()
_MAX_CONFIRMED = 1000
_confirm_lock = threading.Lock()


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

    # --- Deduplication ---
    update_id: int | None = update.get("update_id")
    if update_id is not None:
        if update_id in _processed_updates:
            logger.info("Duplicate update_id=%s — skipping", update_id)
            return JSONResponse({"ok": True})
        _processed_updates.add(update_id)
        if len(_processed_updates) > _MAX_PROCESSED:
            _processed_updates.discard(min(_processed_updates))

    # --- Extract chat_id, text, callback data, and message_id ---
    chat_id: int | None = None
    message_text: str | None = None
    callback_data: str | None = None
    message_id: int | None = None

    if "callback_query" in update:
        cq = update["callback_query"]
        cq_msg = cq.get("message", {})
        chat_id = cq_msg.get("chat", {}).get("id")
        callback_data = cq.get("data", "").strip()
        message_id = cq_msg.get("message_id")

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
        if cb in ("30 min", "45 min", "60 min"):
            if message_id is not None:
                with _confirm_lock:
                    if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                        logger.info("message_id=%s already in-flight or processed — ignoring repeat tap", message_id)
                        return JSONResponse({"ok": True})
                    _in_flight_message_ids.add(message_id)
            trigger = "on_demand"
            extra["duration_min"] = int(callback_data.replace(" min", ""))
            if message_id is not None:
                extra["message_id"] = message_id
        elif cb in ("yes, book them", "confirm"):
            if message_id is not None:
                with _confirm_lock:
                    if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                        logger.info("message_id=%s already confirmed or in-flight — ignoring repeat tap", message_id)
                        import asyncio
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: send_message("✅ Already booked! Check your Google Calendar."),
                        )
                        return JSONResponse({"ok": True})
                    _in_flight_message_ids.add(message_id)
            trigger = "confirm"
            if message_id is not None:
                extra["message_id"] = message_id
        elif cb == "skip":
            if message_id is not None:
                with _confirm_lock:
                    if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                        return JSONResponse({"ok": True})
                    _confirmed_message_ids.add(message_id)
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

    if trigger is None:
        return JSONResponse({"ok": True})

    if trigger == "skip":
        import asyncio
        asyncio.get_event_loop().run_in_executor(
            None,
            lambda: send_message("Okay, no study blocks booked. See you tomorrow! 👋"),
        )
        return JSONResponse({"ok": True})

    logger.info("Trigger detected: %s, chat_id: %s, extra: %s", trigger, chat_id, extra)

    # --- Menu: send duration picker directly, no graph needed ---
    if trigger == "menu":
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            None,
            lambda: send_buttons("How long do you have?", ["30 min", "45 min", "60 min"]),
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
    message_id: int | None = kwargs.get("message_id")
    try:
        logger.info("Invoking graph: trigger=%s, chat_id=%s", trigger, chat_id)
        _graph.invoke(trigger=trigger, chat_id=chat_id, **kwargs)
        logger.info("Graph invoke done, checking state.db size")
        import os
        logger.info("state.db size: %s bytes", os.path.getsize("db/state.db"))
        logger.info("Graph invocation complete: trigger=%s", trigger)
        if trigger in ("confirm", "on_demand") and message_id is not None:
            with _confirm_lock:
                _in_flight_message_ids.discard(message_id)
                _confirmed_message_ids.add(message_id)
                if len(_confirmed_message_ids) > _MAX_CONFIRMED:
                    _confirmed_message_ids.discard(min(_confirmed_message_ids))
    except Exception as e:
        logger.error(
            "Graph invocation failed [trigger=%s chat_id=%s]: %s",
            trigger, chat_id, e, exc_info=True,
        )
        if trigger in ("confirm", "on_demand") and message_id is not None:
            with _confirm_lock:
                _in_flight_message_ids.discard(message_id)
