import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

load_dotenv(Path(__file__).parents[2] / ".env", override=True)


def _get_bot() -> tuple[Bot, int]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        raise EnvironmentError("Missing required env var: TELEGRAM_BOT_TOKEN")
    if not chat_id:
        raise EnvironmentError("Missing required env var: TELEGRAM_CHAT_ID")
    return Bot(token=token), int(chat_id)


async def _send_message(text: str) -> None:
    bot, chat_id = _get_bot()
    try:
        async with bot:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except TelegramError as e:
        raise RuntimeError(f"Telegram send_message failed: {e}") from e


async def _send_buttons(text: str, buttons: list[str]) -> None:
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


def send_message(text: str) -> None:
    asyncio.run(_send_message(text))


def send_buttons(text: str, buttons: list[str]) -> None:
    asyncio.run(_send_buttons(text, buttons))


if __name__ == "__main__":
    send_message("Learning Manager online ✅")
    print("Message sent.")
