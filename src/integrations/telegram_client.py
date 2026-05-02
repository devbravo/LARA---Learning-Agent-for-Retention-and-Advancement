"""Telegram Bot API adapter for sending messages and inline keyboards.

Async helper functions are wrapped by sync convenience functions so callers can
invoke Telegram operations from scheduler threads and executor jobs.

All async work is funnelled through a single dedicated background event loop
(_tg_loop) so the Bot and its underlying httpx session are only ever awaited on
one loop — safe for concurrent calls from multiple executor/scheduler threads.
"""

import atexit
import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Coroutine, TypeVar

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, TimedOut
from telegram.error import BadRequest

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Dedicated Telegram event loop — one loop, one Bot, for the process lifetime.
#
# Why: python-telegram-bot's Bot wraps an httpx AsyncClient whose internals are
# bound to the event loop it was first awaited on.  Calling asyncio.run() per
# send creates a *new* event loop each time, meaning the shared Bot object gets
# used on multiple loops, triggering "bound to a different event loop" errors
# and subtle races under concurrent executor threads.
#
# Solution: spin up a background thread that owns a persistent event loop.
# All public sync helpers submit coroutines to that loop via
# run_coroutine_threadsafe() and block on the resulting Future, so callers
# still get synchronous semantics with a proper return value / exception.
# ---------------------------------------------------------------------------

_tg_loop: asyncio.AbstractEventLoop | None = None
_tg_loop_lock = threading.Lock()
_tg_bot: Bot | None = None
_tg_chat_id: int | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return the shared background event loop, starting it on first call."""
    global _tg_loop
    if _tg_loop is None:
        with _tg_loop_lock:
            if _tg_loop is None:
                loop = asyncio.new_event_loop()

                def _run(lp: asyncio.AbstractEventLoop) -> None:
                    lp.run_forever()

                t = threading.Thread(target=_run, args=(loop,), daemon=True, name="tg-loop")
                t.start()
                _tg_loop = loop

                async def _init_bot() -> None:
                    global _tg_bot, _tg_chat_id
                    token = os.environ.get("TELEGRAM_BOT_TOKEN")
                    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
                    if not token:
                        raise EnvironmentError("Missing required env var: TELEGRAM_BOT_TOKEN")
                    if not chat_id:
                        raise EnvironmentError("Missing required env var: TELEGRAM_CHAT_ID")
                    _tg_bot = Bot(token=token)
                    _tg_chat_id = int(chat_id)

                asyncio.run_coroutine_threadsafe(_init_bot(), loop).result(timeout=10)

                def _shutdown() -> None:
                    async def _close() -> None:
                        if _tg_bot is not None:
                            try:
                                await _tg_bot.shutdown()
                            except Exception:
                                pass

                    try:
                        asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=5)
                    except Exception:
                        pass
                    loop.call_soon_threadsafe(loop.stop)

                atexit.register(_shutdown)
    return _tg_loop  # type: ignore[return-value]


def _run(coro: "Coroutine[Any, Any, T]") -> T:
    """Submit *coro* to the shared Telegram loop and block until done."""
    return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result()


# ---------------------------------------------------------------------------
# Async implementation helpers (run on the dedicated loop)
# ---------------------------------------------------------------------------

async def _send_message(text: str) -> None:
    """Send a plain HTML-enabled Telegram message to the default chat."""
    assert _tg_bot is not None and _tg_chat_id is not None
    for attempt in range(3):
        try:
            await _tg_bot.send_message(chat_id=_tg_chat_id, text=text, parse_mode="HTML")
            return
        except TimedOut:
            if attempt == 2:
                raise RuntimeError("Telegram send_message failed: timed out after 3 attempts")
            await asyncio.sleep(1)
        except TelegramError as e:
            raise RuntimeError(f"Telegram send_message failed: {e}") from e


async def _send_buttons(text: str, buttons: list[str]) -> int:
    """Send a message with a single-row inline keyboard.

    Args:
        text: Message text.
        buttons: Callback labels (label equals callback data).

    Returns:
        The Telegram message_id of the sent message.
    """
    assert _tg_bot is not None and _tg_chat_id is not None
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=label) for label in buttons]]
    )
    for attempt in range(3):
        try:
            msg = await _tg_bot.send_message(
                chat_id=_tg_chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return msg.message_id
        except TimedOut:
            if attempt == 2:
                raise RuntimeError("Telegram send_buttons failed: timed out after 3 attempts")
            await asyncio.sleep(1)
        except TelegramError as e:
            raise RuntimeError(f"Telegram send_buttons failed: {e}") from e
    raise RuntimeError("Telegram send_buttons failed: unreachable")


async def _send_inline_buttons(text: str, buttons: list[tuple[str, str]]) -> int:
    """Send a message with an inline keyboard where label and data can differ.

    Args:
        text: Message text.
        buttons: Sequence of ``(label, callback_data)`` tuples, one button per
            row.

    Returns:
        The Telegram message_id of the sent message.
    """
    assert _tg_bot is not None and _tg_chat_id is not None
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
    )
    for attempt in range(3):
        try:
            msg = await _tg_bot.send_message(
                chat_id=_tg_chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return msg.message_id
        except TimedOut:
            if attempt == 2:
                raise RuntimeError("Telegram send_inline_buttons failed: timed out after 3 attempts")
            await asyncio.sleep(1)
        except TelegramError as e:
            raise RuntimeError(f"Telegram send_inline_buttons failed: {e}") from e
    raise RuntimeError("Telegram send_inline_buttons failed: unreachable")


async def _remove_buttons(chat_id: int, message_id: int) -> None:
    """Remove inline keyboard buttons from an existing Telegram message."""
    assert _tg_bot is not None
    try:
        await _tg_bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except TelegramError as e:
        if isinstance(e, BadRequest) and "not modified" in str(e).lower():
            return  # already removed, ignore
        raise RuntimeError(f"Telegram remove_buttons failed: {e}") from e


# ---------------------------------------------------------------------------
# Public synchronous API
# ---------------------------------------------------------------------------

def send_message(text: str) -> None:
    """Synchronous wrapper for ``_send_message``."""
    _run(_send_message(text))


def send_buttons(text: str, buttons: list[str]) -> int:
    """Synchronous wrapper for ``_send_buttons``.

    Returns:
        The Telegram message_id of the sent message.
    """
    return _run(_send_buttons(text, buttons))


def send_inline_buttons(text: str, buttons: list[tuple[str, str]]) -> int:
    """Synchronous wrapper for ``_send_inline_buttons``.

    Returns:
        The Telegram message_id of the sent message.
    """
    return _run(_send_inline_buttons(text, buttons))


def remove_buttons(chat_id: int, message_id: int) -> None:
    """Synchronous wrapper for ``_remove_buttons``."""
    _run(_remove_buttons(chat_id, message_id))


def get_chat_id() -> int:
    """Return the configured Telegram chat id.

    Reads directly from the environment so it can be called before the
    background Telegram loop is initialised — safe to use from service-layer
    code that needs the chat id before sending the first message.

    Returns:
        Integer chat id from ``TELEGRAM_CHAT_ID`` environment variable.

    Raises:
        EnvironmentError: If the environment variable is not set, or is set
            to a value that cannot be parsed as an integer.
    """
    chat_id_str = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id_str:
        raise EnvironmentError("Missing required env var: TELEGRAM_CHAT_ID")
    try:
        return int(chat_id_str)
    except ValueError as exc:
        raise EnvironmentError(
            f"Invalid TELEGRAM_CHAT_ID: must be an integer, got {chat_id_str!r}."
        ) from exc


if __name__ == "__main__":
    send_message("Learning Manager online ✅")
    print("Message sent.")
