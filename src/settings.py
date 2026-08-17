import yaml
from pathlib import Path
import os

# PATHS
_PROJECT_ROOT = Path(__file__).parents[1]
ENV_FILE = _PROJECT_ROOT / ".env"
TOPICS_PATH = _PROJECT_ROOT / "topics.yaml"
CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
DB_DIR = _PROJECT_ROOT / "db"
DB_PATH = DB_DIR / "learning.db"
LOG_DIR = _PROJECT_ROOT / "logs"
CREDENTIALS_PATH = Path(
    os.environ.get("GOOGLECREDENTIALS_PATH", "credentials/gcal_credentials.json")
)
TOKEN_PATH = Path("credentials/token.json")

# VOYAGE CONFIGS
VOYAGE_MODEL = "voyage-4-lite"
VOYAGE_RERANK_MODEL = "rerank-2"

# GOOGLE SCOPES
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def load_config() -> dict:
    """Load ``config.yaml`` into a dictionary.
    Returns:
        Parsed configuration content.
    """
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)