"""
pytest configuration — stubs out heavy/unavailable modules so that unit tests
that only need dispatcher/repository logic can import without the full runtime
dependency chain (langgraph, google-auth, anthropic, etc.).
"""

import importlib.util
import sys
from unittest.mock import MagicMock


def _stub(name: str) -> MagicMock:
    """Create a MagicMock and register it as a module in sys.modules."""
    mock = MagicMock()
    sys.modules[name] = mock
    return mock


def _stub_if_missing(name: str) -> None:
    """Register a MagicMock for *name* only when the module is not importable."""
    try:
        installed = importlib.util.find_spec(name) is not None
    except (ValueError, ModuleNotFoundError):
        installed = False
    if not installed:
        sys.modules.setdefault(name, MagicMock())


# Stub only src.agent.graph so dispatcher.py can be imported without pulling
# in langgraph / google-auth / anthropic.  Do NOT stub src.agent itself —
# that would shadow the real package and break tests that import src.agent.nodes
# or other real sub-modules.
_stub("src.agent.graph")

# Stub third-party packages only when they are not installed in the environment.
# Using find_spec avoids shadowing real packages when they are present.
for _name in [
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
    "google.oauth2.service_account",
    "google_auth_oauthlib",
    "google_auth_oauthlib.flow",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "anthropic",
    "telegram",
    "telegram.error",
]:
    _stub_if_missing(_name)
