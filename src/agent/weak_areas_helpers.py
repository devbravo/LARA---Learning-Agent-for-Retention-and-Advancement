from pathlib import Path
import yaml

_CONFIG_PATH = Path(__file__).parents[2] / "config.yaml"

# WEAK AREAS HELPERS
def null_if_skip(t: str) -> str | None:
    return None if not t or t.lower() == "skip" else t

def to_key(s: str) -> str:
    return s.lower().strip().replace(" ", "_")

def breakdown(text: str, all_values: list[str]) -> str | list[str]:
    return all_values if text.lower().strip() == "all of the above" else to_key(text)


def load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)