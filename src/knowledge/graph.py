"""LangGraph knowledge agent graph: synthesize → preview → confirm → write.

Entry points:
  - start(chat_id, topic): kick off from a /prepare command (synthesis included).
  - invoke_safe(chat_id, payload): resume a paused flow from a Telegram kg_* callback.

Thread ids are namespaced as ``kg_{chat_id}`` to avoid colliding with the
session graph which uses bare ``str(chat_id)``.
"""

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import sqlite3
from src.knowledge.nodes import (
    prepare_confirm_node,
    prepare_preview_node,
    synthesize_node,
    write_node,
)
from src.knowledge.state import KGState
from src.settings import DB_DIR
from langgraph.checkpoint.sqlite import SqliteSaver

DB_DIR.mkdir(parents=True, exist_ok=True)

_conn = sqlite3.connect(str(DB_DIR / "state.db"), check_same_thread=False)
checkpointer = SqliteSaver(_conn)

logger = logging.getLogger(__name__)


def _route_after_synthesize(state: KGState) -> str:
    """Skip to END if synthesis produced nothing (no articles / API error)."""
    if state.get("synthesis_result") is None:
        logger.info("_route_after_synthesize: no result — ending flow")
        return END
    return "prepare_preview_node"


def _route_after_confirm(state: KGState) -> str:
    """Route to write_node on approval, END on rejection."""
    if state.get("user_interest") == "approved":
        return "write_node"
    logger.info("_route_after_confirm: rejected — skipping write")
    return END


_builder = StateGraph(KGState)
_builder.add_node("synthesize_node", synthesize_node)
_builder.add_node("prepare_preview_node", prepare_preview_node)
_builder.add_node("prepare_confirm_node", prepare_confirm_node)
_builder.add_node("write_node", write_node)

_builder.add_edge(START, "synthesize_node")
_builder.add_conditional_edges(
    "synthesize_node",
    _route_after_synthesize,
    ["prepare_preview_node", END],
)
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


def start(chat_id: int, topic: str) -> None:
    """Start a fresh KG pipeline from a /prepare command.

    Runs synthesize_node (search + synthesize), then pauses at
    prepare_confirm_node waiting for user approval. The server's webhook
    handler resumes the flow when the user taps ✅ Keep or ❌ Discard.

    If the thread is already paused at an interrupt (a previous /prepare is
    still waiting for a button tap), skips synthesis entirely and reminds
    the user to respond to the existing preview.

    Args:
        chat_id: Telegram chat id (used as thread_id namespace and state field).
        topic: The search topic keyword entered after /prepare.
    """
    from src.integrations import telegram_client as _telegram

    config = {"configurable": {"thread_id": f"kg_{chat_id}"}}

    snapshot = graph.get_state(config)
    if _has_pending_interrupt(snapshot):
        existing_topic = snapshot.values.get("topic", "unknown")
        synthesis_result = snapshot.values.get("synthesis_result")
        if synthesis_result is not None:
            logger.info(
                "KG graph: /prepare %r — re-sending buttons for already-paused topic=%r chat_id=%s",
                topic, existing_topic, chat_id,
            )
            from src.knowledge.nodes import send_preview_buttons
            new_msg_id = send_preview_buttons(existing_topic, synthesis_result)
            graph.update_state(config, {"pending_message_id": new_msg_id})
            return
        logger.warning(
            "KG graph: stale checkpoint (synthesis_result=None) for chat_id=%s — restarting",
            chat_id,
        )

    initial_state: KGState = {"topic": topic, "chat_id": chat_id}
    logger.info("KG graph: /prepare %r for chat_id=%s", topic, chat_id)
    try:
        graph.invoke(initial_state, config=config)
    except Exception as e:
        logger.error("KG graph start failed [topic=%r chat_id=%s]: %s", topic, chat_id, e, exc_info=True)
        _telegram.send_message(f"⚠️ /prepare failed for <i>{topic}</i>: {e}")


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
