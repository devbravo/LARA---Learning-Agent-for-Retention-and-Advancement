"""Telegram callback handlers for webhook intent dispatch.

This module maps callback payloads (button taps) into one of three outcomes:
1) an ``Intent`` for graph invocation,
2) a direct ``JSONResponse`` when no graph call is needed,
3) ``None`` to indicate an ignored/duplicate/malformed callback.

Handlers also apply idempotency guards for repeat taps and trigger user-facing
Telegram side effects (confirmation, duplicate notices, button cleanup) when
appropriate.
"""

import asyncio
import logging

from fastapi.responses import JSONResponse

from src.agent import graph as _graph
from src.integrations.telegram_client import remove_buttons, send_message
from src.api.telegram import dispatcher
from src.services import topic_service
from src.api.telegram.intent_parser import Intent

logger = logging.getLogger(__name__)


def handle_duration(cb: str, chat_id: int, message_id: int | None) -> Intent | None:
    """Convert a duration callback into an ``on_demand`` intent.
    Args:
        cb: Normalized callback text, expected in ``{"30 min", "45 min", "60 min"}``.
        chat_id: Telegram chat identifier used as LangGraph thread id.
        message_id: Source Telegram message id used for repeat-tap protection.
    Returns:
        Intent for ``trigger="on_demand"`` with ``duration_min`` in ``extra``.
        Returns ``None`` when the message has already been processed.
    """
    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info("message_id=%s already in-flight or processed — ignoring repeat tap", message_id)
            return None
    duration = int(cb.replace(" min", ""))
    extra: dict = {"duration_min": duration}
    if message_id is not None:
        extra["message_id"] = message_id
    return Intent(trigger="on_demand", chat_id=chat_id, message_id=message_id, extra=extra)


def handle_confirm(cb: str, chat_id: int, message_id: int | None) -> Intent | None:
    """Convert a confirmation callback into a ``confirm`` intent.
    On duplicate taps, this handler sends an "Already booked" notice and
    returns ``None`` so the callback is ignored safely.
    Args:
        cb: Normalized callback text (``"yes, book them"`` or ``"confirm"``).
        chat_id: Telegram chat identifier used as LangGraph thread id.
        message_id: Source Telegram message id used for idempotency.
    Returns:
        Intent for ``trigger="confirm"`` when accepted, otherwise ``None``.
    """
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


def handle_skip(chat_id: int, message_id: int | None) -> Intent | None:
    """Handle ``skip`` callbacks for both booking and weak-area flows.
    Behavior depends on checkpointed state:
    - If ``awaiting_weak_areas`` is true, returns an Intent for
      ``trigger="weak_areas"`` with empty messages (explicit skip).
    - Otherwise, sends a "no study blocks booked" message and returns
      ``trigger="skip"``.
    Args:
        chat_id: Telegram chat identifier used to read graph state.
        message_id: Source Telegram message id used for idempotency.
    Returns:
        Intent for ``weak_areas`` or ``skip``; returns ``None`` when rejected as
        a duplicate callback.
    """

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


def handle_rating(cb: str, chat_id: int, message_id: int | None) -> Intent | None:
    """Convert a rating callback into a ``rate`` intent.
    Args:
        cb: Lowercased rating label (``"😕 hard"``, ``"😐 ok"``, ``"😊 easy"``).
        chat_id: Telegram chat identifier used as LangGraph thread id.
        message_id: Source Telegram message id used for idempotency.

    Returns:
        Intent for ``trigger="rate"`` with ``quality_score`` in ``extra``.
        Returns ``None`` when the rating tap is a duplicate.
    """

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


def handle_category(callback_data: str, chat_id: int) -> Intent:
    """Convert ``category:<name>`` callback data into a category intent.
    Args:
        callback_data: Raw callback payload with ``category:`` prefix.
        chat_id: Telegram chat identifier used as LangGraph thread id.
    Returns:
        Intent for ``trigger="study_topic_category"`` preserving original
        category text casing.
    """

    category = callback_data[len("category:"):]  # preserve original case
    return Intent(
        trigger="study_topic_category",
        chat_id=chat_id,
        message_id=None,
        extra={"study_topic_category": category},
    )


def handle_subtopic_id(callback_data: str, chat_id: int, message_id: int | None) -> Intent | None:
    """Resolve ``subtopic_id:<id>`` callback data into a topic-confirm intent.
    The handler validates id format, applies idempotency protection, and
    resolves the topic name from the database before returning an intent.
    Args:
        callback_data: Raw callback payload with ``subtopic_id:`` prefix.
        chat_id: Telegram chat identifier used as LangGraph thread id.
        message_id: Source Telegram message id used for idempotency.
    Returns:
        Intent for ``trigger="study_topic_confirm"`` with ``proposed_topic``.
        Returns ``None`` for malformed ids, unknown topics, or duplicate taps.
    """

    try:
        topic_id = int(callback_data[len("subtopic_id:"):])
    except ValueError:
        logger.warning("Invalid subtopic callback_data received: %s", callback_data)
        return None

    if message_id is not None:
        if not dispatcher.try_mark_in_flight(message_id):
            logger.info("message_id=%s already processed for subtopic — ignoring", message_id)
            return None

    topic_name = topic_service.get_topic_name_by_id(topic_id)
    if topic_name is None:
        logger.warning("Unknown subtopic id in callback_data: %s", callback_data)
        if message_id is not None:
            dispatcher.clear_in_flight(message_id)
        return None

    return Intent(
        trigger="study_topic_confirm",
        chat_id=chat_id,
        message_id=message_id,
        extra={"proposed_topic": topic_name, "message_id": message_id},
    )


def handle_studied(callback_data: str, chat_id: int, message_id: int | None) -> JSONResponse | None:
    """Handle ``studied:<id>`` callback by graduating a topic to active.
    This path performs a direct service call (no graph invocation), sends user
    feedback, and removes inline buttons when possible.
    Args:
        callback_data: Raw callback payload with ``studied:`` prefix.
        chat_id: Telegram chat id used for optional button removal.
        message_id: Source message id used for idempotency and cleanup.
    Returns:
        ``JSONResponse({"ok": True})`` for all handled callbacks, including
        success and service-level failures. Returns ``None`` only for malformed
        callback payloads.
    """

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
