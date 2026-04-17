"""
Unit tests for the Telegram webhook handler package.

Pure unit tests — no FastAPI TestClient, no real DB, no Telegram calls.
All external dependencies (graph, DB, Telegram) are mocked.

asyncio.run() drains the default executor before returning (Python 3.9+), so
executor-dispatched callables complete before any assertion runs.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.models.telegram import (
    TelegramCallbackQuery,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
)
from src.api.telegram.dispatcher import (
    _confirmed_message_ids,
    _in_flight_message_ids,
    _processed_updates,
)
from src.api.telegram.handler import handle_update


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(update_id: int, chat_id: int, text: str, message_id: int = 1) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=message_id,
            chat=TelegramChat(id=chat_id),
            text=text,
        ),
    )


def _cb(update_id: int, chat_id: int, data: str, message_id: int = 100) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        callback_query=TelegramCallbackQuery(
            id="cq_id",
            data=data,
            message=TelegramMessage(
                message_id=message_id,
                chat=TelegramChat(id=chat_id),
            ),
        ),
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_state():
    """Clear module-level dedup / in-flight sets before and after each test."""
    _processed_updates.clear()
    _confirmed_message_ids.clear()
    _in_flight_message_ids.clear()
    yield
    _processed_updates.clear()
    _confirmed_message_ids.clear()
    _in_flight_message_ids.clear()


# ---------------------------------------------------------------------------
# 1. Auth rejection
# ---------------------------------------------------------------------------

def test_auth_rejection_returns_403():
    """Webhook route returns 403 when the secret token does not match."""
    from src.api.routes.webhook import webhook

    async def _inner():
        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={})
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "correct"}):
            with pytest.raises(HTTPException) as exc_info:
                await webhook(mock_request, x_telegram_bot_api_secret_token="wrong")
        assert exc_info.value.status_code == 403

    _run(_inner())


# ---------------------------------------------------------------------------
# 2. Deduplication
# ---------------------------------------------------------------------------

def test_deduplication_skips_second_call():
    """Second call with the same update_id returns ok without invoking the graph."""
    update = _msg(update_id=55555, chat_id=111, text="/study")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))
        first_count = mock_invoke.call_count  # 1 (executor ran synchronously via asyncio.run)

        _run(handle_update(update))          # same update_id → deduped
        assert mock_invoke.call_count == first_count  # no additional invocations


# ---------------------------------------------------------------------------
# 3. Unknown callback ignored
# ---------------------------------------------------------------------------

def test_unknown_callback_returns_ok_without_graph():
    """An unrecognised callback_data returns ok and never invokes the graph."""
    update = _cb(update_id=1, chat_id=111, data="totally_unknown_action")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 4. /done → trigger "done"
# ---------------------------------------------------------------------------

def test_done_message_triggers_done():
    """/done text triggers the 'done' trigger."""
    update = _msg(update_id=2, chat_id=111, text="/done")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "done"


# ---------------------------------------------------------------------------
# 5. /study → trigger "on_demand"
# ---------------------------------------------------------------------------

def test_study_message_triggers_on_demand():
    """/study text triggers the 'on_demand' trigger."""
    update = _msg(update_id=3, chat_id=111, text="/study")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "on_demand"


# ---------------------------------------------------------------------------
# 6. /briefing → trigger "daily"
# ---------------------------------------------------------------------------

def test_briefing_message_triggers_daily():
    """/briefing text triggers the 'daily' trigger."""
    update = _msg(update_id=4, chat_id=111, text="/plan")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "daily"


# ---------------------------------------------------------------------------
# 7. "yes, book them" callback → trigger "confirm"
# ---------------------------------------------------------------------------

def test_yes_book_them_callback_triggers_confirm():
    """\"yes, book them" callback triggers the 'confirm' trigger."""
    update = _cb(update_id=5, chat_id=111, data="yes, book them", message_id=200)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "confirm"
    assert mock_invoke.call_args[1].get("message_id") == 200


# ---------------------------------------------------------------------------
# 8. "skip" callback without awaiting_weak_areas → trigger "skip" (no graph)
# ---------------------------------------------------------------------------

def test_skip_callback_sends_skip_message_without_graph():
    """\"skip\" with no awaiting_weak_areas state sends the skip message and returns."""
    update = _cb(update_id=6, chat_id=111, data="skip", message_id=201)

    with patch("src.api.telegram.callback_handlers._graph") as mock_graph, \
         patch("src.api.telegram.callback_handlers.send_message") as mock_send, \
         patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        mock_graph.get_state.return_value = {}

        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_invoke.assert_not_called()
    mock_send.assert_called_once()
    assert "no study blocks booked" in mock_send.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# 9. Rating "😐 OK" callback → trigger "rate" with quality_score=3
# ---------------------------------------------------------------------------

def test_ok_rating_callback_triggers_rate_with_score_3():
    """😐 OK" callback triggers 'rate' with quality_score=3."""
    update = _cb(update_id=7, chat_id=111, data="😐 OK", message_id=202)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "rate"
    assert mock_invoke.call_args[1].get("quality_score") == 3


# ---------------------------------------------------------------------------
# 10. "category:DSA" callback → trigger "study_topic_category"
# ---------------------------------------------------------------------------

def test_category_callback_triggers_study_topic_category():
    """category:DSA" triggers 'study_topic_category' with the correct category."""
    update = _cb(update_id=8, chat_id=111, data="category:DSA")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "study_topic_category"
    assert mock_invoke.call_args[1].get("study_topic_category") == "DSA"


# ---------------------------------------------------------------------------
# 11. "subtopic_id:<valid>" → trigger "study_topic_confirm" with topic name
# ---------------------------------------------------------------------------

def test_subtopic_id_valid_triggers_study_topic_confirm():
    """subtopic_id:5" with a matching DB row triggers 'study_topic_confirm'."""
    update = _cb(update_id=9, chat_id=111, data="subtopic_id:5", message_id=203)

    with patch("src.services.topic_service.get_topic_name_by_id", return_value="DSA - Arrays"), \
         patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "study_topic_confirm"
    assert mock_invoke.call_args[1].get("proposed_topic") == "DSA - Arrays"


# ---------------------------------------------------------------------------
# 12. "subtopic_id:<non-numeric>" → early return, no graph
# ---------------------------------------------------------------------------

def test_subtopic_id_invalid_returns_early():
    """subtopic_id:abc" is invalid — returns ok without invoking the graph."""
    update = _cb(update_id=10, chat_id=111, data="subtopic_id:abc")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 13. "studied:<valid id>" → DB updated, confirmation message sent
# ---------------------------------------------------------------------------

def test_studied_valid_id_updates_db_and_sends_confirmation():
    """studied:7" with a valid DB row updates status and sends a success message."""
    update = _cb(update_id=11, chat_id=111, data="studied:7", message_id=204)

    with patch("src.services.topic_service.graduate_topic", return_value="DSA - Arrays"), \
         patch("src.api.telegram.callback_handlers.send_message") as mock_send, \
         patch("src.api.telegram.callback_handlers.remove_buttons"):
        _run(handle_update(update))

    mock_send.assert_called_once()
    msg = mock_send.call_args[0][0]
    assert "DSA - Arrays" in msg
    assert "graduated to active" in msg


# ---------------------------------------------------------------------------
# 14. "studied:<valid id>" but rowcount=0 → error message, in-flight cleared
# ---------------------------------------------------------------------------

def test_studied_invalid_id_sends_error_message():
    """studied:999" where DB rowcount=0 sends an error message."""
    update = _cb(update_id=12, chat_id=111, data="studied:999", message_id=205)

    with patch("src.services.topic_service.graduate_topic", side_effect=ValueError("Topic id=999 not found in DB")), \
         patch("src.api.telegram.callback_handlers.send_message") as mock_send:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_send.assert_called_once()
    assert "⚠️" in mock_send.call_args[0][0]

    # In-flight should be cleared on error
    assert 205 not in _in_flight_message_ids


# ---------------------------------------------------------------------------
# 15. In-flight message_id blocks repeat tap
# ---------------------------------------------------------------------------

def test_in_flight_message_id_blocks_repeat_tap():
    """A tap on a message_id already in _in_flight_message_ids returns early."""
    message_id = 206
    _in_flight_message_ids.add(message_id)

    update = _cb(update_id=13, chat_id=111, data="yes, book them", message_id=message_id)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke, \
         patch("src.api.telegram.callback_handlers.send_message"):
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 16. /plan alias -> trigger "daily"
# ---------------------------------------------------------------------------

def test_plan_message_triggers_daily():
    """/plan text triggers the 'daily' trigger."""
    update = _msg(update_id=14, chat_id=111, text="/plan")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "daily"


# ---------------------------------------------------------------------------
# 17. /pick alias -> trigger "study_topic"
# ---------------------------------------------------------------------------

def test_pick_message_triggers_study_topic():
    """/pick text triggers the 'study_topic' trigger."""
    update = _msg(update_id=15, chat_id=111, text="/pick")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0][0] == "study_topic"


# ---------------------------------------------------------------------------
# 18. /activate alias -> direct command path
# ---------------------------------------------------------------------------

def test_activate_message_returns_direct_response_without_graph():
    """/activate is handled directly and does not invoke the graph."""
    update = _msg(update_id=16, chat_id=111, text="/activate")

    with patch("src.services.topic_service.get_in_progress_topics", return_value=[]), \
         patch("src.api.telegram.message_handlers.send_message") as mock_send, \
         patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_send.assert_called_once()
    mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 19. /help -> direct help message
# ---------------------------------------------------------------------------

def test_help_message_returns_direct_response_without_graph():
    """/help is handled directly, sends help text, and does not invoke graph."""
    update = _msg(update_id=17, chat_id=111, text="/help")

    with patch("src.api.telegram.message_handlers.send_message") as mock_send, \
         patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_send.assert_called_once()
    assert "/study" in mock_send.call_args[0][0]
    assert "/help" in mock_send.call_args[0][0]
    mock_invoke.assert_not_called()

