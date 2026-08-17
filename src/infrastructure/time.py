"""Local-time helpers for DB inserts and date comparisons.

Reads timezone from config.yaml so nothing is hardcoded.
"""

from datetime import datetime
from functools import lru_cache
from src.settings import lara_config

import pytz


@lru_cache(maxsize=1)
def _tz() -> pytz.BaseTzInfo:
    return pytz.timezone(lara_config["timezone"])


def local_now() -> str:
    """Current local datetime as 'YYYY-MM-DD HH:MM:SS' for DB inserts."""
    return datetime.now(_tz()).strftime("%Y-%m-%d %H:%M:%S")


def local_today() -> str:
    """Current local date as 'YYYY-MM-DD' for DB date comparisons."""
    return datetime.now(_tz()).strftime("%Y-%m-%d")
