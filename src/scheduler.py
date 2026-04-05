"""
APScheduler setup for the Learning Manager agent.

Jobs:
  - Daily 08:00 (Mon–Sat) → trigger="daily"
  - Sunday 09:00          → trigger="daily"  (weekly planning variant)

Timezone: America/Paramaribo
Guard:    never invoke during the 15:00–19:30 protected block.
"""
import os
import yaml
import pytz
import logging

from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from src.agent import graph as _graph
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv(Path(__file__).parents[1] / ".env", override=True)

logger = logging.getLogger(__name__)

def _load_config() -> dict:
    with open(Path(__file__).parents[1] / "config.yaml") as f:
        return yaml.safe_load(f)

_TZ = pytz.timezone(_load_config()["timezone"])
logger.info("Current time in Paramaribo: %s", datetime.now(_TZ))


def _is_protected_block() -> bool:
    """Return True if current local time falls inside any protected block."""
    config = _load_config()
    now = datetime.now(_TZ).time()
    for block in config.get("protected_blocks", []):
        start = datetime.strptime(block["start"], "%H:%M").time()
        end = datetime.strptime(block["end"], "%H:%M").time()
        if start <= now < end:
            return True
    return False

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
    config = _load_config()
    scheduler = AsyncIOScheduler(timezone=_TZ)

    daily = config["schedule"]["daily_briefing"]
    sunday = config["schedule"]["sunday_planning"]

    # Mon–Sat at 08:00
    scheduler.add_job(
        _run_daily_briefing,
        trigger=CronTrigger(day_of_week="mon-sat", hour=daily["hour"], minute=daily["minute"], timezone=_TZ),
        id="daily_briefing_weekday",
        name=f"Daily Briefing (Mon–Sat {daily['hour']:02d}:{daily['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=daily["misfire_grace_time"],
    )

    # Sunday at 09:00 (weekly planning variant)
    scheduler.add_job(
        _run_daily_briefing,
        trigger=CronTrigger(day_of_week="sun", hour=sunday["hour"], minute=sunday["minute"], timezone=_TZ),
        id="daily_briefing_sunday",
        name=f"Weekly Planning (Sun {sunday['hour']:02d}:{sunday['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=sunday["misfire_grace_time"],
    )

    return scheduler
