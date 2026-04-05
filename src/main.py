"""
Entry point for the Kairos agent.

Starts FastAPI (via uvicorn) and APScheduler in the same async process.
"""

import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env", override=True)

from src.scheduler import build_scheduler
from src.server import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_HOST = os.environ.get("HOST", "0.0.0.0")
_PORT = int(os.environ.get("PORT", "8000"))


async def main() -> None:
    # --- Scheduler ---
    scheduler = build_scheduler()
    scheduler.start()

    for job in scheduler.get_jobs():
        logger.info("Scheduled: %s  next_run=%s", job.name, job.next_run_time)

    # --- uvicorn (async, non-blocking) ---
    config = uvicorn.Config(
        app=app,
        host=_HOST,
        port=_PORT,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    logger.info("Starting Kairos on %s:%s", _HOST, _PORT)

    try:
        await server.serve()
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down.")


if __name__ == "__main__":
    asyncio.run(main())
