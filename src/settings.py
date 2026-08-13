import yaml
from pathlib import Path

_CONFIG_PATH = Path(__file__).parents[1] / "config.yaml"


def _load_config() -> dict:
    """Load ``config.yaml`` into a dictionary.
    Returns:
        Parsed configuration content.
    """
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)