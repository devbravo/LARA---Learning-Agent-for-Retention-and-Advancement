"""
APScheduler setup for the Learning Manager agent.

Jobs:
  - Daily 08:00 (Mon–Sat) → trigger="daily"
  - Sunday 09:00          → trigger="daily"  (weekly planning variant)

Timezone: America/Paramaribo
Guard:    never invoke during the 15:00–19:30 protected block.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env", override=True)

from src.agent import graph as _graph

logger = logging.getLogger(__name__)

_TZ = "America/Paramaribo"
_PROTECTED_START = (15, 0)   # HH, MM
_PROTECTED_END   = (19, 30)


def _is_protected_block() -> bool:
    """Return True if current local time falls inside the protected block."""
    now = datetime.now()
    start_min = _PROTECTED_START[0] * 60 + _PROTECTED_START[1]
    end_min   = _PROTECTED_END[0]   * 60 + _PROTECTED_END[1]
    now_min   = now.hour * 60 + now.minute
    return start_min <= now_min < end_min


def _run_daily_briefing() -> None:
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    if _is_protected_block():
        logger.warning("Daily briefing skipped — inside protected block (15:00–19:30).")
        return
    logger.info("Scheduler: firing daily briefing for chat_id=%s", chat_id)
    try:
        _graph.invoke(trigger="daily", chat_id=chat_id)
    except Exception as e:
        logger.error("Daily briefing graph error: %s", e)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=_TZ)

    # Mon–Sat at 08:00
    scheduler.add_job(
        _run_daily_briefing,
        trigger=CronTrigger(day_of_week="mon-sat", hour=8, minute=0, timezone=_TZ),
        id="daily_briefing_weekday",
        name="Daily Briefing (Mon–Sat 08:45)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Sunday at 09:00 (weekly planning variant)
    scheduler.add_job(
        _run_daily_briefing,
        trigger=CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=_TZ),
        id="daily_briefing_sunday",
        name="Weekly Planning (Sun 09:00)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    return scheduler
