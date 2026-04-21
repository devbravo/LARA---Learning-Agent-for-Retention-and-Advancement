"""
Unit tests for the Telegram webhook handler package (HITL pattern).

In the HITL refactor, handler.py is a thin coordinator:
- /help and /view → direct JSONResponse (no graph)
- Everything else → dispatcher.invoke_safe(chat_id, payload)
- Callbacks go through idempotency guard in callback_handlers

Pure unit tests — no FastAPI TestClient, no real DB, no Telegram calls.
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
    update = _msg(update_id=55555, chat_id=111, text="/study")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))
        first_count = mock_invoke.call_count

        _run(handle_update(update))
        assert mock_invoke.call_count == first_count


# ---------------------------------------------------------------------------
# 3. /done → invoke_safe(chat_id, "/done")
# ---------------------------------------------------------------------------

def test_done_message_calls_invoke_safe_with_slash_done():
    update = _msg(update_id=2, chat_id=111, text="/done")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "/done")


# ---------------------------------------------------------------------------
# 4. /study → invoke_safe(chat_id, "/study")
# ---------------------------------------------------------------------------

def test_study_message_calls_invoke_safe_with_slash_study():
    """/study is forwarded to the graph (send_duration_picker node handles it)."""
    update = _msg(update_id=3, chat_id=111, text="/study")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "/study")


# ---------------------------------------------------------------------------
# 5. /plan → invoke_safe(chat_id, "/plan")
# ---------------------------------------------------------------------------

def test_plan_message_calls_invoke_safe_with_slash_plan():
    update = _msg(update_id=4, chat_id=111, text="/plan")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "/plan")


# ---------------------------------------------------------------------------
# 6. "yes, book them" callback → invoke_safe as resume value
# ---------------------------------------------------------------------------

def test_yes_book_them_callback_calls_invoke_safe():
    update = _cb(update_id=5, chat_id=111, data="yes, book them", message_id=200)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "yes, book them")


# ---------------------------------------------------------------------------
# 7. "skip" callback → invoke_safe as resume value
# ---------------------------------------------------------------------------

def test_skip_callback_calls_invoke_safe():
    """In HITL, 'skip' is a resume value forwarded to the graph."""
    update = _cb(update_id=6, chat_id=111, data="skip", message_id=201)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "skip")


# ---------------------------------------------------------------------------
# 8. Rating "😐 OK" callback → invoke_safe as resume value
# ---------------------------------------------------------------------------

def test_ok_rating_callback_calls_invoke_safe():
    update = _cb(update_id=7, chat_id=111, data="😐 OK", message_id=202)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "😐 OK")


# ---------------------------------------------------------------------------
# 9. "category:DSA" callback → invoke_safe as resume value
# ---------------------------------------------------------------------------

def test_category_callback_calls_invoke_safe():
    update = _cb(update_id=8, chat_id=111, data="category:DSA")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "category:DSA")


# ---------------------------------------------------------------------------
# 10. "subtopic_id:5" callback → invoke_safe as resume value
# ---------------------------------------------------------------------------

def test_subtopic_id_callback_calls_invoke_safe():
    update = _cb(update_id=9, chat_id=111, data="subtopic_id:5", message_id=203)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "subtopic_id:5")


# ---------------------------------------------------------------------------
# 11. "studied:7" callback → invoke_safe as resume value
# ---------------------------------------------------------------------------

def test_studied_callback_calls_invoke_safe():
    """studied: is now a resume value for the activate_topic interrupt."""
    update = _cb(update_id=11, chat_id=111, data="studied:7", message_id=204)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "studied:7")


# ---------------------------------------------------------------------------
# 12. In-flight message_id blocks repeat tap
# ---------------------------------------------------------------------------

def test_in_flight_message_id_blocks_repeat_tap():
    """A tap on a message_id already in _in_flight_message_ids is suppressed."""
    message_id = 206
    _in_flight_message_ids.add(message_id)

    update = _cb(update_id=13, chat_id=111, data="yes, book them", message_id=message_id)

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 13. /pick → invoke_safe(chat_id, "/pick")
# ---------------------------------------------------------------------------

def test_pick_message_calls_invoke_safe():
    update = _msg(update_id=15, chat_id=111, text="/pick")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "/pick")


# ---------------------------------------------------------------------------
# 14. /activate → invoke_safe(chat_id, "/activate") (now goes through graph)
# ---------------------------------------------------------------------------

def test_activate_message_calls_invoke_safe():
    """/activate is forwarded to the graph (activate_topic node handles it)."""
    update = _msg(update_id=16, chat_id=111, text="/activate")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "/activate")


# ---------------------------------------------------------------------------
# 15. /help → direct help message (no graph)
# ---------------------------------------------------------------------------

def test_help_message_returns_direct_response_without_graph():
    update = _msg(update_id=17, chat_id=111, text="/help")

    with patch("src.api.telegram.message_handlers.send_message") as mock_send, \
         patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_send.assert_called_once()
    assert "/study" in mock_send.call_args[0][0]
    assert "/help" in mock_send.call_args[0][0]
    mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 16. /view → direct snapshot (no graph)
# ---------------------------------------------------------------------------

def test_view_message_returns_direct_response_without_graph():
    update = _msg(update_id=18, chat_id=111, text="/view")

    snapshot = {"overdue": [], "due_today": [], "in_progress": []}
    with patch("src.services.view_service.get_study_snapshot", return_value=snapshot), \
         patch("src.api.telegram.message_handlers.send_message") as mock_send, \
         patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        result = _run(handle_update(update))

    assert result.body == b'{"ok":true}'
    mock_send.assert_called_once()
    mock_invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 17. Free text → invoke_safe (forwarded as weak-areas resume value)
# ---------------------------------------------------------------------------

def test_free_text_calls_invoke_safe():
    """Free text is forwarded to the graph as a potential resume value."""
    update = _msg(update_id=19, chat_id=111, text="struggled with dynamic programming")

    with patch("src.api.telegram.dispatcher.invoke_safe") as mock_invoke:
        _run(handle_update(update))

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args[0] == (111, "struggled with dynamic programming")
