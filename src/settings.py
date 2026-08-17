import yaml
from pathlib import Path
import os
from dotenv import load_dotenv

# STATIC PATHS
_PROJECT_ROOT = Path(__file__).parents[1]
ENV_FILE = _PROJECT_ROOT / ".env"
TOPICS_PATH = _PROJECT_ROOT / "topics.yaml"
CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
DB_DIR = _PROJECT_ROOT / "db"
DB_PATH = DB_DIR / "learning.db"
LOG_DIR = _PROJECT_ROOT / "logs"
TOKEN_PATH = Path("credentials/token.json")

# VOYAGE CONFIGS
VOYAGE_MODEL = "voyage-4-lite"
VOYAGE_RERANK_MODEL = "rerank-2"

# GOOGLE SCOPES
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ENV LOADING
load_dotenv(ENV_FILE, override=True)
CREDENTIALS_PATH = Path( # Get GOOGLE_CREDENTIALS_PATH after loading the .env file
    os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials/gcal_credentials.json")
)

# CONFIG LOADING
def load_config() -> dict:
    """Load ``config.yaml`` into a dictionary.
    Returns:
        Parsed configuration content.
    """
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

try:
    lara_config = load_config()
except FileNotFoundError as exc:
    raise RuntimeError(f"Configuration file not found at '{CONFIG_PATH}'. The system cannot start without it.") from exc
except yaml.YAMLError as exc:
    raise RuntimeError(f"Failed to parse the configuration file '{CONFIG_PATH}'. Ensure it is valid YAML.\nDetails: {exc}") from exc
except Exception as exc:
    raise RuntimeError(f"An unexpected error occurred while loading the configuration: {exc}") from exc