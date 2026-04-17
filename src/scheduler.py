"""
APScheduler setup for the Learning Manager agent.

Jobs:
  - Daily 08:00 (Mon–Sat)  → trigger="daily"   (morning briefing)
  - Sunday 09:00           → trigger="daily"   (weekly planning variant)
  - Daily 20:00 (Mon–Sat)  → trigger="evening" (tomorrow's preview)

Timezone: America/Paramaribo
Guard:    never invoke during the 15:00–19:30 protected block.
"""

import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Any

import pytz
import yaml
from dotenv import load_dotenv

from src.agent import graph as _graph
from src.integrations.telegram_client import send_message
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv(Path(__file__).parents[1] / ".env", override=True)

logger = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    """Load ``config.yaml`` into a dictionary.
    Returns:
        Parsed configuration content.
    """
    with open(Path(__file__).parents[1] / "config.yaml") as f:
        return yaml.safe_load(f)

_TZ = pytz.timezone(_load_config()["timezone"])
logger.info("Current time in Paramaribo: %s", datetime.now(_TZ))


def _is_protected_block() -> bool:
    """Check whether the current local time is in a protected block.
    Returns:
        ``True`` when now is inside any configured protected interval.
    """
    config = _load_config()
    now = datetime.now(_TZ).time()
    for block in config.get("protected_blocks", []):
        start = datetime.strptime(block["start"], "%H:%M").time()
        end = datetime.strptime(block["end"], "%H:%M").time()
        if start <= now < end:
            return True
    return False

def _run_daily_planning() -> None:
    """Invoke the graph with ``trigger='daily'`` if outside protected time.
    On failure, logs the exception and sends a user-facing Telegram warning.
    """
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    if _is_protected_block():
        logger.warning("Daily briefing skipped — inside protected block (15:00–19:30).")
        return
    logger.info("Scheduler: firing daily briefing for chat_id=%s", chat_id)
    try:
        _graph.invoke(trigger="daily", chat_id=chat_id)
    except Exception as e:
        logger.error("Daily briefing graph error: %s", e)

        try:
            send_message(f"⚠️ Daily briefing failed: {e}")
        except Exception:
            pass


def _run_evening_briefing() -> None:
    """Invoke the graph with ``trigger='evening'`` if outside protected time.
    On failure, logs the exception and sends a user-facing Telegram warning.
    """
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    if _is_protected_block():
        logger.warning("Evening briefing skipped — inside protected block (15:00–19:30).")
        return
    logger.info("Scheduler: firing evening briefing for chat_id=%s", chat_id)
    try:
        _graph.invoke(trigger="evening", chat_id=chat_id)
    except Exception as e:
        logger.error("Evening briefing graph error: %s", e)
        try:
            send_message(f"⚠️ Evening briefing failed: {e}")
        except Exception:
            pass


def build_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance used by the API app.
    Returns:
        ``AsyncIOScheduler`` with weekday/sunday daily jobs and evening preview.
    """
    config = _load_config()
    scheduler = AsyncIOScheduler(timezone=_TZ)

    daily = config["schedule"]["daily_planning"]
    sunday = config["schedule"]["sunday_planning"]
    evening = config["schedule"]["evening_briefing"]

    # Mon–Sat at 08:00
    scheduler.add_job(
        _run_daily_planning,
        trigger=CronTrigger(day_of_week="mon-sat", hour=daily["hour"], minute=daily["minute"], timezone=_TZ),
        id="daily_planning_weekday",
        name=f"Daily Briefing (Mon–Sat {daily['hour']:02d}:{daily['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=daily["misfire_grace_time"],
    )

    # Sunday at 09:00 (weekly planning variant)
    scheduler.add_job(
        _run_daily_planning,
        trigger=CronTrigger(day_of_week="sun", hour=sunday["hour"], minute=sunday["minute"], timezone=_TZ),
        id="daily_planning_sunday",
        name=f"Weekly Planning (Sun {sunday['hour']:02d}:{sunday['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=sunday["misfire_grace_time"],
    )

    scheduler.add_job(
        _run_evening_briefing,
        trigger=CronTrigger(day_of_week="mon-sat", hour=evening["hour"], minute=evening["minute"], timezone=_TZ),
        id="evening_briefing",
        name=f"Evening Briefing — Tomorrow's Preview (Mon–Sat {evening['hour']:02d}:{evening['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=evening["misfire_grace_time"],
    )

    return scheduler

