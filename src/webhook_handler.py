"""
Telegram webhook handler — intent detection, deduplication, and graph invocation.

Extracted from src/server.py as part of API structure refactor.
All business logic lives here; src/api/routes/webhook.py handles only auth + parsing.
"""

import asyncio
import logging
import os
import threading

from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.api.schemas.telegram import TelegramUpdate
from src.core.db import get_connection
from src.integrations.telegram_client import (
    remove_buttons,
    send_buttons,
    send_inline_buttons,
    send_message,
)

logger = logging.getLogger(__name__)

# Deduplication guard — keeps last 1000 processed update_ids in memory
_processed_updates: set[int] = set()
_MAX_PROCESSED = 1000

# Confirmed bookings — tracks message_ids that have already been booked
# Prevents double-booking when the user taps "Yes, book them" more than once
_confirmed_message_ids: set[int] = set()
_in_flight_message_ids: set[int] = set()
_MAX_CONFIRMED = 1000
_confirm_lock = threading.Lock()


def _invoke_safe(trigger: str, chat_id: int, **kwargs) -> None:
    """Invoke the graph, catching all exceptions so executor threads never crash."""
    message_id: int | None = kwargs.get("message_id")
    try:
        logger.info("Invoking graph: trigger=%s, chat_id=%s", trigger, chat_id)
        _graph.invoke(trigger=trigger, chat_id=chat_id, **kwargs)
        logger.info("Graph invoke done, checking state.db size")
        logger.info("state.db size: %s bytes", os.path.getsize("db/state.db"))
        logger.info("Graph invocation complete: trigger=%s", trigger)
        if trigger in ("confirm", "on_demand", "rate", "study_topic_confirm", "studied") and message_id is not None:
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
        if trigger in ("confirm", "on_demand", "rate") and message_id is not None:
            with _confirm_lock:
                _in_flight_message_ids.discard(message_id)


async def handle_update(update: TelegramUpdate) -> JSONResponse:
    """Process a Telegram update: dedup, extract intent, invoke graph."""
    logger.debug("Webhook update: update_id=%s", update.update_id)

    # --- Deduplication ---
    update_id = update.update_id
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
            extra["duration_min"] = int(cb.replace(" min", ""))
            if message_id is not None:
                extra["message_id"] = message_id
        elif cb in ("yes, book them", "confirm"):
            if message_id is not None:
                with _confirm_lock:
                    if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                        logger.info("message_id=%s already confirmed or in-flight — ignoring repeat tap", message_id)
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
            state = _graph.get_state(chat_id)
            if state.get("awaiting_weak_areas"):
                if message_text is not None:
                    with _confirm_lock:
                        if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                            logger.info("message_id=%s already processed for weak_areas skip — ignoring repeat tap",
                                        message_id)
                            return JSONResponse({"ok": True})
                        _in_flight_message_ids.add(message_id)

                trigger = "weak_areas"
                extra["messages"] = []
                if message_id is not None:
                    extra["message_id"] = message_id
            else:
                if message_id is not None:
                    with _confirm_lock:
                        if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                            return JSONResponse({"ok": True})
                        _confirmed_message_ids.add(message_id)
                trigger = "skip"
        elif cb in ("😕 hard", "😐 ok", "😊 easy"):
            if message_id is not None:
                with _confirm_lock:
                    if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                        logger.info("message_id=%s already rated - ignoring repeat tap", message_id)
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: send_message("✅ Already rated! Thanks for your feedback."))
                        return JSONResponse({"ok": True})
                    _in_flight_message_ids.add(message_id)
                trigger = "rate"
                score_map = {"😕 hard": 2, "😐 ok": 3, "😊 easy": 5}
                extra["quality_score"] = score_map[cb]
                if message_id is not None:
                    extra["message_id"] = message_id

        elif cb.startswith("category:"):
            trigger = "study_topic_category"
            extra["study_topic_category"] = callback_data[len("category:"):]  # preserve original case
        elif cb.startswith("subtopic_id:"):
            try:
                topic_id = int(callback_data[len("subtopic_id:"):])
            except ValueError:
                logger.warning("Invalid subtopic callback_data received: %s", callback_data)
                return JSONResponse({"ok": True})
            if message_id is not None:
                with _confirm_lock:
                    if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                        logger.info("message_id=%s already processed for subtopic — ignoring", message_id)
                        return JSONResponse({"ok": True})
                    _in_flight_message_ids.add(message_id)
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT name FROM topics WHERE id = ?",
                    (topic_id,)).fetchone()
            if row is None:
                logger.warning("Unknown subtopic id in callback_data: %s", callback_data)
                if message_id is not None:
                    with _confirm_lock:
                        _in_flight_message_ids.discard(message_id)
                return JSONResponse({"ok": True})
            trigger = "study_topic_confirm"
            extra["proposed_topic"] = row["name"]
            extra["message_id"] = message_id
        elif cb.startswith("studied:"):
            topic_id = int(callback_data[len("studied:"):])  # preserve original case
            if message_id is not None:
                with _confirm_lock:
                    if message_id in _confirmed_message_ids or message_id in _in_flight_message_ids:
                        logger.info("message_id=%s already processed for studied callback — ignoring", message_id)
                        return JSONResponse({"ok": True})
                    _in_flight_message_ids.add(message_id)
            try:
                with get_connection() as conn:
                    cursor = conn.execute(
                        """UPDATE topics
                           SET status = 'active',
                               repetitions = 0,
                               easiness_factor = 2.5,
                               next_review = date('now', '+1 day'),
                               updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (topic_id,),
                    )
                    if cursor.rowcount == 0:
                        raise ValueError(f"Topic id={topic_id} not found in DB")
                    topic_name = conn.execute(
                        "SELECT name FROM topics WHERE id = ?", (topic_id,)
                    ).fetchone()["name"]

                if message_id is not None:
                    with _confirm_lock:
                        _in_flight_message_ids.discard(message_id)
                        _confirmed_message_ids.add(message_id)
                        if len(_confirmed_message_ids) > _MAX_CONFIRMED:
                            _confirmed_message_ids.discard(min(_confirmed_message_ids))

                loop = asyncio.get_event_loop()
                _tn = topic_name
                loop.run_in_executor(
                    None,
                    lambda: send_message(
                        f"✅ {_tn} graduated to active. First SM-2 review scheduled for tomorrow."
                    ),
                )
                if chat_id is not None and message_id is not None:
                    _cid, _mid = chat_id, message_id
                    loop.run_in_executor(None, lambda: remove_buttons(_cid, _mid))

            except Exception as e:
                logger.error("studied: DB update failed for %s: %s", topic_id, e)
                if message_id is not None:
                    with _confirm_lock:
                        _in_flight_message_ids.discard(message_id)
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: send_message(f"⚠️ Failed to graduate topic: {e}"),
                )
                return JSONResponse({"ok": True})
        else:
            # Unknown callback — ignore
            return JSONResponse({"ok": True})

    elif message_text:
        logger.debug("Incoming message_text received (length=%d)", len(message_text))
        if message_text.strip().lower() in ("/done", "done"):
            trigger = "done"
        elif message_text.strip().lower() == "/study":
            trigger = "on_demand"
        elif message_text.strip().lower() == '/briefing':
            trigger = "daily"
        elif message_text.strip().lower() == '/study_topic':
            trigger = "study_topic"
        elif message_text.strip().lower() == '/studied':
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT id, name FROM topics WHERE status = 'in_progress' ORDER BY tier ASC, name ASC"
                ).fetchall()
            if not rows:
                loop = asyncio.get_event_loop()
                loop.run_in_executor(
                    None,
                    lambda: send_message("No topics are currently in progress."),
                )
            else:
                buttons = [(row["name"], f"studied:{row['id']}") for row in rows]
                loop = asyncio.get_event_loop()
                loop.run_in_executor(
                    None,
                    lambda: send_inline_buttons("Which topic did you just study?", buttons),
                )
            return JSONResponse({"ok": True})
        else:
            state = _graph.get_state(chat_id)
            if state.get("awaiting_weak_areas"):
                trigger = "weak_areas"
                extra["messages"] = [message_text]
            else:
                # Unrecognized — ignore silently
                return JSONResponse({"ok": True})

    if trigger is None:
        return JSONResponse({"ok": True})

    if trigger == "skip":
        asyncio.get_event_loop().run_in_executor(
            None,
            lambda: send_message("Okay, no study blocks booked. See you tomorrow! 👋"),
        )
        return JSONResponse({"ok": True})

    logger.info("Trigger detected: %s, chat_id: %s, extra: %s", trigger, chat_id, extra)

    # --- Menu: send duration picker directly, no graph needed ---
    if trigger == "menu":
        loop = asyncio.get_event_loop()
        loop.run_in_executor(
            None,
            lambda: send_buttons("How long do you have?", ["30 min", "45 min", "60 min"]),
        )
        return JSONResponse({"ok": True})

    # --- Invoke graph (fire-and-forget in background) ---
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        lambda: _invoke_safe(trigger, chat_id, **extra),
    )

    return JSONResponse({"ok": True})
