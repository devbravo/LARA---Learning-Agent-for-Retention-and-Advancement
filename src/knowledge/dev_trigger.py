"""Dev script: trigger the full KG pipeline without Telegram.

Equivalent to sending /prepare <topic> in Telegram. Runs search → synthesize,
sends the preview with ✅ Keep / ❌ Discard buttons to your Telegram chat, then
parks the graph. Tapping a button triggers the webhook (server must be running).

Usage:
    python -m src.knowledge.dev_trigger "context management"
"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from src.integrations.telegram_client import get_chat_id
from src.knowledge.graph import start


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m src.knowledge.dev_trigger <topic>")
        sys.exit(1)

    topic = " ".join(sys.argv[1:])
    chat_id = get_chat_id()

    print(f"Starting KG pipeline for topic={topic!r} chat_id={chat_id}")
    print("(Server must be running for button taps to be processed.)\n")
    start(chat_id, topic)
    print("\nDone. Tap ✅ Keep or ❌ Discard in Telegram.")


if __name__ == "__main__":
    main()
