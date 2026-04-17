"""Telegram Bot API adapter for sending messages and inline keyboards.

Async helper functions are wrapped by sync convenience functions so callers can
invoke Telegram operations from scheduler threads and executor jobs.
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.error import BadRequest

load_dotenv(Path(__file__).parents[2] / ".env", override=True)


def _get_bot() -> tuple[Bot, int]:
    """Create a configured bot client and resolve default chat id.
    Returns:
        Tuple of ``(Bot, chat_id)``.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        raise EnvironmentError("Missing required env var: TELEGRAM_BOT_TOKEN")
    if not chat_id:
        raise EnvironmentError("Missing required env var: TELEGRAM_CHAT_ID")
    return Bot(token=token), int(chat_id)


async def _send_message(text: str) -> None:
    """Send a plain HTML-enabled Telegram message to the default chat."""
    bot, chat_id = _get_bot()
    try:
        async with bot:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except TelegramError as e:
        raise RuntimeError(f"Telegram send_message failed: {e}") from e


async def _send_buttons(text: str, buttons: list[str]) -> None:
    """Send a message with a single-row inline keyboard.

    Args:
        text: Message text.
        buttons: Callback labels (label equals callback data).
    """
    bot, chat_id = _get_bot()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=label) for label in buttons]]
    )
    try:
        async with bot:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    except TelegramError as e:
        raise RuntimeError(f"Telegram send_buttons failed: {e}") from e


async def _send_inline_buttons(text: str, buttons: list[tuple[str, str]]) -> None:
    """Send a message with an inline keyboard where label and data can differ.

    Args:
        text: Message text.
        buttons: Sequence of ``(label, callback_data)`` tuples, one button per
            row.
    """
    bot, chat_id = _get_bot()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
    )
    try:
        async with bot:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    except TelegramError as e:
        raise RuntimeError(f"Telegram send_inline_buttons failed: {e}") from e


async def _remove_buttons(chat_id: int, message_id: int) -> None:
    """Remove inline keyboard buttons from an existing Telegram message."""
    bot, _ = _get_bot()
    try:
        async with bot:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
    except TelegramError as e:
        if isinstance(e, BadRequest) and "not modified" in str(e).lower():
            return  # already removed, ignore
        raise RuntimeError(f"Telegram remove_buttons failed: {e}") from e


def send_message(text: str) -> None:
    """Synchronous wrapper for ``_send_message``."""
    asyncio.run(_send_message(text))


def send_buttons(text: str, buttons: list[str]) -> None:
    """Synchronous wrapper for ``_send_buttons``."""
    asyncio.run(_send_buttons(text, buttons))


def send_inline_buttons(text: str, buttons: list[tuple[str, str]]) -> None:
    """Synchronous wrapper for ``_send_inline_buttons``."""
    asyncio.run(_send_inline_buttons(text, buttons))


def remove_buttons(chat_id: int, message_id: int) -> None:
    """Synchronous wrapper for ``_remove_buttons``."""
    asyncio.run(_remove_buttons(chat_id, message_id))


if __name__ == "__main__":
    send_message("Learning Manager online ✅")
    print("Message sent.")
