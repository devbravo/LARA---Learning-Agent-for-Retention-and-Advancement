"""
pytest configuration — stubs out heavy/unavailable modules so that unit tests
that only need dispatcher/repository logic can import without the full runtime
dependency chain (langgraph, google-auth, anthropic, etc.).
"""

import sys
import types
from unittest.mock import MagicMock


def _stub(name: str) -> MagicMock:
    """Create a MagicMock and register it as a module in sys.modules."""
    mock = MagicMock()
    sys.modules[name] = mock
    return mock


# Stub only src.agent.graph so dispatcher.py can be imported without pulling
# in langgraph / google-auth / anthropic.  Do NOT stub src.agent itself —
# that would shadow the real package and break tests that import src.agent.nodes
# or other real sub-modules.
_stub("src.agent.graph")


