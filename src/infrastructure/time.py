"""Local-time helpers for DB inserts and date comparisons.

Reads timezone from config.yaml so nothing is hardcoded.
"""

from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pytz
import yaml

_CONFIG_PATH = Path(__file__).parents[2] / "config.yaml"


@lru_cache(maxsize=1)
def _tz() -> pytz.BaseTzInfo:
    with open(_CONFIG_PATH) as f:
        return pytz.timezone(yaml.safe_load(f)["timezone"])


def local_now() -> str:
    """Current local datetime as 'YYYY-MM-DD HH:MM:SS' for DB inserts."""
    return datetime.now(_tz()).strftime("%Y-%m-%d %H:%M:%S")


def local_today() -> str:
    """Current local date as 'YYYY-MM-DD' for DB date comparisons."""
    return datetime.now(_tz()).strftime("%Y-%m-%d")
