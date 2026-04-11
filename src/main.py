"""
Entry point for the LARA agent.

Starts FastAPI (via uvicorn) and APScheduler in the same async process.
"""

import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env", override=True)
from src.server import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/lara.log"),  # add this
        ]
)
logger = logging.getLogger(__name__)

_HOST = os.environ.get("HOST", "0.0.0.0")
_PORT = int(os.environ.get("PORT", "8000"))


async def main() -> None:
    # --- uvicorn (async, non-blocking) ---
    config = uvicorn.Config(
        app=app,
        host=_HOST,
        port=_PORT,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    logger.info("Starting LARA on %s:%s", _HOST, _PORT)
    await server.serve()



if __name__ == "__main__":
    asyncio.run(main())
