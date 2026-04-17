"""
Callback handlers — one function per Telegram callback type.

Each function returns an Intent (for graph dispatch) or a JSONResponse
(for direct response with no graph invocation), or None to signal early return.
"""

import asyncio
import logging

from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.core.db import get_connection
from src.integrations.telegram_client import remove_buttons, send_message
from src.api.telegram import dispatcher

logger = logging.getLogger(__name__)


def handle_duration(cb: str, chat_id: int, message_id: int | None):
    """Handle 30/45/60 min duration taps → trigger 'on_demand'."""
    from src.api.telegram.intent_parser import Intent

    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info("message_id=%s already in-flight or processed — ignoring repeat tap", message_id)
            return None
    duration = int(cb.replace(" min", ""))
    extra: dict = {"duration_min": duration}
    if message_id is not None:
        extra["message_id"] = message_id
    return Intent(trigger="on_demand", chat_id=chat_id, message_id=message_id, extra=extra)


def handle_confirm(cb: str, chat_id: int, message_id: int | None):
    """Handle 'yes, book them' / 'confirm' taps → trigger 'confirm'."""
    from src.api.telegram.intent_parser import Intent

    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info("message_id=%s already confirmed or in-flight — ignoring repeat tap", message_id)
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: send_message("✅ Already booked! Check your Google Calendar."),
            )
            return None
    extra: dict = {}
    if message_id is not None:
        extra["message_id"] = message_id
    return Intent(trigger="confirm", chat_id=chat_id, message_id=message_id, extra=extra)


def handle_skip(chat_id: int, message_id: int | None):
    """
    Handle 'skip' taps.

    If awaiting_weak_areas → trigger 'weak_areas' (skip logging weak areas).
    Otherwise → send skip message and return Intent(trigger='skip') with no graph invocation.
    """
    from src.api.telegram.intent_parser import Intent

    state = _graph.get_state(chat_id)
    if state.get("awaiting_weak_areas"):
        if message_id is not None:
            if not dispatcher.try_mark_in_flight(message_id):
                logger.info(
                    "message_id=%s already processed for weak_areas skip — ignoring repeat tap",
                    message_id,
                )
                return None
        extra: dict = {"messages": []}
        if message_id is not None:
            extra["message_id"] = message_id
        return Intent(trigger="weak_areas", chat_id=chat_id, message_id=message_id, extra=extra)
    else:
        if message_id is not None:
            if not dispatcher.try_mark_in_flight(message_id):
                return None
            dispatcher.mark_confirmed(message_id)
        asyncio.get_event_loop().run_in_executor(
            None,
            lambda: send_message("Okay, no study blocks booked. See you tomorrow! 👋"),
        )
        return Intent(trigger="skip", chat_id=chat_id, message_id=message_id, extra={})


def handle_rating(cb: str, chat_id: int, message_id: int | None):
    """Handle 😕/😐/😊 rating taps → trigger 'rate'."""
    from src.api.telegram.intent_parser import Intent

    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info("message_id=%s already rated - ignoring repeat tap", message_id)
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: send_message("✅ Already rated! Thanks for your feedback."),
            )
            return None
    score_map = {"😕 hard": 2, "😐 ok": 3, "😊 easy": 5}
    extra: dict = {"quality_score": score_map[cb]}
    if message_id is not None:
        extra["message_id"] = message_id
    return Intent(trigger="rate", chat_id=chat_id, message_id=message_id, extra=extra)


def handle_category(callback_data: str, chat_id: int):
    """Handle 'category:<name>' taps → trigger 'study_topic_category'."""
    from src.api.telegram.intent_parser import Intent

    category = callback_data[len("category:"):]  # preserve original case
    return Intent(
        trigger="study_topic_category",
        chat_id=chat_id,
        message_id=None,
        extra={"study_topic_category": category},
    )


def handle_subtopic_id(callback_data: str, chat_id: int, message_id: int | None):
    """Handle 'subtopic_id:<id>' taps → trigger 'study_topic_confirm'."""
    from src.api.telegram.intent_parser import Intent

    try:
        topic_id = int(callback_data[len("subtopic_id:"):])
    except ValueError:
        logger.warning("Invalid subtopic callback_data received: %s", callback_data)
        return None

    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info("message_id=%s already processed for subtopic — ignoring", message_id)
            return None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT name FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()

    if row is None:
        logger.warning("Unknown subtopic id in callback_data: %s", callback_data)
        if message_id is not None:
            dispatcher.clear_in_flight(message_id)
        return None

    return Intent(
        trigger="study_topic_confirm",
        chat_id=chat_id,
        message_id=message_id,
        extra={"proposed_topic": row["name"], "message_id": message_id},
    )


def handle_studied(callback_data: str, chat_id: int, message_id: int | None) -> JSONResponse | None:
    """
    Handle 'studied:<id>' taps — graduate topic to active.
    Returns JSONResponse directly (no graph invocation needed).
    Returns None if callback_data is malformed.
    """
    from src.services import topic_service

    try:
        topic_id = int(callback_data[len("studied:"):])
    except ValueError:
        logger.warning("Invalid studied callback_data received: %s", callback_data)
        return None

    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info(
                "message_id=%s already processed for studied callback — ignoring", message_id
            )
            return JSONResponse({"ok": True})

    try:
        topic_name = topic_service.graduate_topic(topic_id)

        if message_id is not None:
            dispatcher.mark_confirmed(message_id)

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
            dispatcher.clear_in_flight(message_id)
        asyncio.get_event_loop().run_in_executor(
            None,
            lambda: send_message(f"⚠️ Failed to graduate topic: {e}"),
        )

    return JSONResponse({"ok": True})
