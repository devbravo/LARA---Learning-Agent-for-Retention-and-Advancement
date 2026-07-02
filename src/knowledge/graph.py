"""LangGraph knowledge agent graph: preview → confirm → write.

Entry points:
  - run(topic, chat_id, synthesis_result): start a fresh flow from the synthesis pipeline.
  - invoke_safe(chat_id, payload): resume a paused flow from a Telegram kg_* callback.

Thread ids are namespaced as ``kg_{chat_id}`` to avoid colliding with the
session graph which uses bare ``str(chat_id)``.
"""

import logging
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import sqlite3
from src.knowledge.nodes import prepare_confirm_node, prepare_preview_node, write_node
from src.knowledge.state import KGState
from langgraph.checkpoint.sqlite import SqliteSaver



_DB_DIR = Path(__file__).parents[3] / "db"
_DB_DIR.mkdir(parents=True, exist_ok=True)

_conn = sqlite3.connect(str(_DB_DIR / "state.db"), check_same_thread=False)
checkpointer = SqliteSaver(_conn)

logger = logging.getLogger(__name__)


def _route_after_confirm(state: KGState) -> str:
    """Route to write_node on approval, END on rejection."""
    if state.get("user_interest") == "approved":
        return "write_node"
    logger.info("_route_after_confirm: rejected — skipping write")
    return END


_builder = StateGraph(KGState)
_builder.add_node("prepare_preview_node", prepare_preview_node)
_builder.add_node("prepare_confirm_node", prepare_confirm_node)
_builder.add_node("write_node", write_node)

_builder.add_edge(START, "prepare_preview_node")
_builder.add_edge("prepare_preview_node", "prepare_confirm_node")
_builder.add_conditional_edges(
    "prepare_confirm_node",
    _route_after_confirm,
    ["write_node", END],
)
_builder.add_edge("write_node", END)

graph = _builder.compile(checkpointer=checkpointer)


def _has_pending_interrupt(state) -> bool:
    """Return True when the graph is paused at an interrupt() call."""
    tasks = getattr(state, "tasks", [])
    if any(getattr(t, "interrupts", None) for t in tasks):
        return True
    return bool(getattr(state, "next", ()))


def run(topic: str, chat_id: int, synthesis_result: dict) -> None:
    """Start a fresh KG confirm flow from the synthesis pipeline.

    Sends the preview to Telegram and pauses at the interrupt in
    prepare_confirm_node. Control returns to the caller immediately after
    the GraphInterrupt is raised; the flow resumes when the user taps a button
    and invoke_safe() is called.

    Args:
        topic: The search topic keyword.
        chat_id: Telegram chat id (used as thread_id namespace and state field).
        synthesis_result: Output of ``synthesize_concept_note``.
    """
    config = {"configurable": {"thread_id": f"kg_{chat_id}"}}
    initial_state: KGState = {
        "topic": topic,
        "chat_id": chat_id,
        "synthesis_result": synthesis_result,
    }
    logger.info("KG graph: starting fresh flow for topic=%r chat_id=%s", topic, chat_id)
    try:
        graph.invoke(initial_state, config=config)
    except Exception as e:
        # GraphInterrupt is expected — graph paused at prepare_confirm_node.
        # Any other exception is a real error.
        from langgraph.errors import GraphInterrupt  # type: ignore[import]
        if not isinstance(e, GraphInterrupt):
            logger.error("KG graph fresh invocation failed: %s", e, exc_info=True)
            raise


def invoke_safe(chat_id: int, payload: str) -> None:
    """Resume a paused KG graph from a kg_approve / kg_reject callback.

    Args:
        chat_id: Telegram chat id; must match the thread started by ``run()``.
        payload: Raw callback_data string (``"kg_approve"`` or ``"kg_reject"``).
    """
    config = {"configurable": {"thread_id": f"kg_{chat_id}"}}
    try:
        state_snapshot = graph.get_state(config)
        if not _has_pending_interrupt(state_snapshot):
            logger.warning(
                "invoke_safe: no pending interrupt for kg chat_id=%s payload=%r — ignoring",
                chat_id,
                payload,
            )
            return
        logger.info("KG graph: resuming chat_id=%s payload=%r", chat_id, payload)
        graph.invoke(Command(resume=payload), config=config)
        logger.info("KG graph: flow complete for chat_id=%s", chat_id)
    except Exception as e:
        logger.error(
            "KG graph invocation failed [chat_id=%s payload=%r]: %s",
            chat_id,
            payload,
            e,
            exc_info=True,
        )
