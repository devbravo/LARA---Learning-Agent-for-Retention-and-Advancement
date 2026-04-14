"""
Entry point for the LARA agent.

Starts FastAPI (via uvicorn) and APScheduler in the same async process.
"""

import os
import sys
import signal
import asyncio
import logging

import uvicorn
from pathlib import Path
from dotenv import load_dotenv

from src.server import app


load_dotenv(Path(__file__).parents[1] / ".env", override=True)

_LOG_DIR = Path(__file__).parents[1] / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/lara.log"),  # add this
        ],
    force=True,
)
logger = logging.getLogger(__name__)

_HOST = os.environ.get("HOST", "0.0.0.0")
_PORT = int(os.environ.get("PORT", "8000"))


async def main() -> None:
    config = uvicorn.Config(
        app=app,
        host=_HOST,
        port=_PORT,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()

    def _handle_exit() -> None:
        logger.info("Shutting down Learning Manager…")
        server.should_exit = True

    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_exit)
        loop.add_signal_handler(signal.SIGINT, _handle_exit)
    except NotImplementedError:
        logger.warning(
            "Signal handlers are not supported by the current event loop/platform; "
            "continuing without custom SIGTERM/SIGINT handlers."
        )

    logger.info("Starting LARA on %s:%s", _HOST, _PORT)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
