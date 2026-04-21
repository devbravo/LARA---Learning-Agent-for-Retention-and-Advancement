"""
APScheduler setup for the Learning Manager agent.

Jobs:
  - Mon–Fri 07:00  → trigger="daily"   (weekday morning planning)
  - Sat–Sun 10:00  → trigger="weekend" (weekend brief)
  - Sun–Thu 20:00  → trigger="evening" (evening brief - preview of next day)

Timezone: America/Paramaribo
Guard:    never invoke weekday planning during the 15:00–19:00 protected block.
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

_PROJECT_ROOT = Path(__file__).parents[2]

load_dotenv(_PROJECT_ROOT / ".env", override=True)

logger = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    """Load ``config.yaml`` into a dictionary.
    Returns:
        Parsed configuration content.
    """
    with open(_PROJECT_ROOT / "config.yaml") as f:
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

def _run_weekday_planning() -> None:
    """Invoke the graph with ``trigger='daily'`` if outside protected time.
    On failure, logs the exception and sends a user-facing Telegram warning.
    """
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    if _is_protected_block():
        logger.warning("Weekday briefing skipped — inside protected block (15:00–19:00).")
        return
    logger.info("Scheduler: firing Weekday briefing for chat_id=%s", chat_id)
    try:
        _graph.invoke(trigger="daily", chat_id=chat_id)
    except Exception as e:
        logger.error("Weekday briefing graph error: %s", e)

        try:
            send_message(f"⚠️ Weekday briefing failed: {e}")
        except Exception:
            pass


def _run_weekend_brief() -> None:
    """Invoke the graph with ``trigger='weekend'`` on Sat/Sun at 10:00
    On failure, logs the exception and sends a user-facing Telegram warning.
    """
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    logger.info("Scheduler: firing weekend briefing for chat_id=%s", chat_id)
    try:
        _graph.invoke(trigger="weekend", chat_id=chat_id)
    except Exception as e:
        logger.error("Weekend briefing graph error: %s", e)
        try:
            send_message(f"⚠️ Weekend briefing failed: {e}")
        except Exception:
            pass



def _run_evening_brief() -> None:
    """Invoke the graph with ``trigger='evening'`` if outside protected time.
    On failure, logs the exception and sends a user-facing Telegram warning.
    """
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    if _is_protected_block():
        logger.warning("Evening briefing skipped — inside protected block (15:00–19:00).")
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
        ``AsyncIOScheduler`` with weekday planning, weekend brief, and evening brief jobs.
    """
    config = _load_config()
    scheduler = AsyncIOScheduler(timezone=_TZ)

    weekday = config["schedule"]["weekday_planning"]
    weekend = config["schedule"]["weekend_brief"]
    evening = config["schedule"]["evening_brief"]

    # Mon–Fri at 07:00
    scheduler.add_job(
        _run_weekday_planning,
        trigger=CronTrigger(day_of_week="mon-fri", hour=weekday["hour"], minute=weekday["minute"], timezone=_TZ),
        id="weekday_planning",
        name=f"Weekday Briefing (Mon–Fri {weekday['hour']:02d}:{weekday['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=weekday["misfire_grace_time"],
    )

    # Sat-Sun 10:00 (weekend planning variant)
    scheduler.add_job(
        _run_weekend_brief,
        trigger=CronTrigger(day_of_week="sat,sun", hour=weekend["hour"], minute=weekend["minute"], timezone=_TZ),
        id="weekend_brief",
        name=f"Weekend Brief (Sat-Sun {weekend['hour']:02d}:{weekend['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=weekend["misfire_grace_time"],
    )

    # Mon-Fri at 20:00
    scheduler.add_job(
        _run_evening_brief,
        trigger=CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=evening["hour"], minute=evening["minute"], timezone=_TZ),
        id="evening_brief",
        name=f"Evening Briefing — Tomorrow's Preview (Sun–Thu {evening['hour']:02d}:{evening['minute']:02d})",
        replace_existing=True,
        misfire_grace_time=evening["misfire_grace_time"],
    )

    return scheduler
