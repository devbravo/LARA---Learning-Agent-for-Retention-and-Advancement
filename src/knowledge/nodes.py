"""LangGraph nodes for the Knowledge Agent graph.

Node flow:
  prepare_preview_node  →  sends Telegram preview + approval buttons
  prepare_confirm_node  →  interrupt() (pauses until user taps approve/reject)
  write_node            →  only reached on approval; writes to SQLite + Neo4j

HITL rule: interrupt() is the FIRST statement in prepare_confirm_node.
No Telegram sends, no DB writes, no side effects before it.
"""

import logging

from langgraph.types import interrupt

from src.integrations import telegram_client as _telegram
from src.knowledge.clients import KnowledgeClients
from src.knowledge.state import KGState
from src.knowledge.write import write_concept_note

logger = logging.getLogger(__name__)

_NOTE_PREVIEW_CHARS = 600


def prepare_preview_node(state: KGState) -> dict:
    """Format the synthesis result and send it to Telegram with approval buttons.

    Sends the note preview and concept list to the user, then stores the
    returned message_id so prepare_confirm_node can remove the buttons on resume.

    Args:
        state: Graph state containing ``topic``, ``synthesis_result``.

    Returns:
        Dict with ``pending_message_id`` set to the Telegram message id.
    """
    topic: str = state["topic"]
    synthesis_result: dict = state["synthesis_result"]

    note: str = synthesis_result["synthesized_note"]
    concepts: list[str] = synthesis_result["concepts"]
    source_urls: list[str] = synthesis_result.get("source_urls", [])

    preview = note[:_NOTE_PREVIEW_CHARS]
    truncated = len(note) > _NOTE_PREVIEW_CHARS

    concept_line = ", ".join(concepts[:10])
    if len(concepts) > 10:
        concept_line += f" (+{len(concepts) - 10} more)"

    text = (
        f"<b>Knowledge note ready: {topic}</b>\n\n"
        f"{preview}{'…' if truncated else ''}\n\n"
        f"<b>Concepts ({len(concepts)}):</b> {concept_line}\n"
        f"<b>Sources:</b> {len(source_urls)}"
    )

    buttons = [("✅ Keep", "kg_approve"), ("❌ Discard", "kg_reject")]
    msg_id = _telegram.send_inline_buttons(text, buttons)

    logger.info(
        "prepare_preview_node: sent preview for topic=%r, message_id=%d",
        topic,
        msg_id,
    )

    return {"pending_message_id": msg_id}


def prepare_confirm_node(state: KGState) -> dict:
    """Pause until the user approves or rejects the concept note.

    interrupt() is the FIRST statement — no side effects before it.
    On resume, receives the raw callback_data string: "kg_approve" or "kg_reject".

    Args:
        state: Graph state; reads ``pending_message_id`` and ``chat_id``
            BEFORE calling interrupt() (pure reads, no side effects).

    Returns:
        Dict with ``pending_message_id`` cleared to None and ``user_interest``
        set to ``"approved"`` or ``"rejected"``.
    """
    msg_id = state.get("pending_message_id")
    chat_id: int = state["chat_id"]

    user_choice: str = interrupt("waiting for KG approval")

    if msg_id is not None:
        try:
            _telegram.remove_buttons(chat_id, msg_id)
        except Exception as e:
            logger.debug("remove_buttons silently failed: %s", e)

    user_interest = "approved" if user_choice == "kg_approve" else "rejected"

    logger.info(
        "prepare_confirm_node: chat_id=%s choice=%r → user_interest=%r",
        chat_id,
        user_choice,
        user_interest,
    )

    return {
        "pending_message_id": None,
        "user_interest": user_interest,
    }


def write_node(state: KGState) -> dict:
    """Persist the approved concept note to SQLite and Neo4j.

    Only reached when the user tapped "Keep" (user_interest == "approved").
    Creates fresh KnowledgeClients, writes, then closes them.

    Args:
        state: Graph state containing ``topic`` and ``synthesis_result``.

    Returns:
        Dict with ``write_result`` set to the output of ``write_concept_note``,
        or empty dict on failure (error is sent to Telegram).
    """
    topic: str = state["topic"]
    synthesis_result: dict = state["synthesis_result"]

    clients = KnowledgeClients()
    try:
        result = write_concept_note(topic, synthesis_result, clients)
        logger.info(
            "write_node: committed concept note id=%d for topic=%r "
            "(created=%d, matched=%d)",
            result["sqlite_id"],
            topic,
            len(result["concepts_created"]),
            len(result["concepts_matched"]),
        )
        _telegram.send_message(
            f"✅ Concept note saved (id={result['sqlite_id']}, "
            f"topic: {topic})"
        )
        return {"write_result": result}
    except Exception as e:
        logger.error("write_node: write_concept_note failed: %s", e, exc_info=True)
        _telegram.send_message(f"⚠️ Failed to save concept note for '{topic}': {e}")
        return {}
    finally:
        clients.close()
