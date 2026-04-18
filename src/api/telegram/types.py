"""Shared type definitions for Telegram intent dispatch.

Kept in a separate module to avoid circular imports between
``intent_parser`` and ``callback_handlers``.
"""

from dataclasses import dataclass, field
from typing import Any, TypeAlias

from fastapi.responses import JSONResponse


@dataclass
class Intent:
    """Dispatch envelope for graph invocations.

    Attributes:
        trigger: Graph trigger name.
        chat_id: Telegram chat id used as LangGraph thread id.
        message_id: Source Telegram message id when available.
        extra: Additional partial state passed to graph invocation.
    """

    trigger: str
    chat_id: int
    message_id: int | None
    extra: dict[str, Any] = field(default_factory=dict)


ParseResult: TypeAlias = Intent | JSONResponse | None

