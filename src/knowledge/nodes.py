"""LangGraph nodes for the Knowledge Agent graph.

Node flow:
  synthesize_node       →  search + synthesize (creates synthesis_result in state)
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
from src.knowledge.extract import is_extraction_valid
from src.knowledge.lookup import find_prior_concept_notes
from src.knowledge.search import fetch_article_contents, search_blogs_for_topic, select_top_results
from src.knowledge.state import KGState
from src.knowledge.synthesize import synthesize_concept_note
from src.knowledge.write import write_concept_note

logger = logging.getLogger(__name__)

_NOTE_PREVIEW_CHARS = 600


def send_preview_buttons(topic: str, synthesis_result: dict) -> int:
    """Format a synthesis result and send it to Telegram with approval buttons.

    Extracted so both ``prepare_preview_node`` and the re-send path in
    ``graph.start()`` (when buttons are lost after a server restart) can call
    the same formatting logic.

    Args:
        topic: The search topic keyword.
        synthesis_result: Output of ``synthesize_concept_note``.

    Returns:
        Telegram message_id of the sent button message.
    """
    note: str = synthesis_result["synthesized_note"]
    concepts: list[str] = synthesis_result["concepts"]
    source_urls: list[str] = synthesis_result.get("source_urls", [])

    preview = note[:_NOTE_PREVIEW_CHARS]
    truncated = len(note) > _NOTE_PREVIEW_CHARS

    concept_line = ", ".join(concepts[:10])
    if len(concepts) > 10:
        concept_line += f" (+{len(concepts) - 10} more)"

    source_lines = "\n".join(
        f'  {i}. <a href="{url}">{url}</a>' for i, url in enumerate(source_urls, 1)
    )

    text = (
        f"<b>Knowledge note ready: {topic}</b>\n\n"
        f"{preview}{'…' if truncated else ''}\n\n"
        f"<b>Concepts ({len(concepts)}):</b> {concept_line}\n\n"
        f"<b>Sources ({len(source_urls)}):</b>\n{source_lines}"
    )

    buttons = [("✅ Keep", "kg_approve"), ("❌ Discard", "kg_reject")]
    return _telegram.send_inline_buttons(text, buttons)


def synthesize_node(state: KGState) -> dict:
    """Run the full search → synthesize pipeline for the topic in state.

    Sends progress messages to Telegram so the user knows work is happening.
    On success stores ``synthesis_result`` in state. On failure (no articles
    found, API error) sends an error message and stores ``None`` so the
    conditional edge after this node can route to END instead of the preview.

    Args:
        state: Graph state containing ``topic`` and ``chat_id``.

    Returns:
        Dict with ``synthesis_result`` set to the output of
        ``synthesize_concept_note``, or ``None`` on failure.
    """
    topic: str = state["topic"]

    clients = KnowledgeClients()
    try:
        _telegram.send_message(f"🔍 Searching for <b>{topic}</b>…")

        all_results = search_blogs_for_topic(topic, clients)
        top = select_top_results(topic, all_results, clients)

        if not top:
            _telegram.send_message(f"⚠️ No relevant articles found for <i>{topic}</i>.")
            return {"synthesis_result": None}

        top_with_content = fetch_article_contents(top, clients.jina_api_key)
        extracted = [
            item for item in top_with_content
            if is_extraction_valid(item.get("content", ""))[0]
        ]

        if not extracted:
            _telegram.send_message(f"⚠️ No usable articles found for <i>{topic}</i>.")
            return {"synthesis_result": None}

        _telegram.send_message(
            f"✍️ Synthesizing from {len(extracted)} article(s)…"
        )
        prior_notes = find_prior_concept_notes(topic, clients)
        result = synthesize_concept_note(topic, extracted, clients, prior_notes=prior_notes)

        logger.info(
            "synthesize_node: done for topic=%r "
            "(%d concepts, %d in / %d out tokens)",
            topic,
            len(result["concepts"]),
            result["input_tokens"],
            result["output_tokens"],
        )
        return {"synthesis_result": result}

    except Exception as e:
        logger.error("synthesize_node failed for topic=%r: %s", topic, e, exc_info=True)
        _telegram.send_message(f"⚠️ Synthesis failed for <i>{topic}</i>: {e}")
        return {"synthesis_result": None}
    finally:
        clients.close()


def prepare_preview_node(state: KGState) -> dict:
    """Send the synthesis result to Telegram with approval buttons.

    Args:
        state: Graph state containing ``topic`` and ``synthesis_result``.

    Returns:
        Dict with ``pending_message_id`` set to the Telegram message id.
    """
    topic: str = state["topic"]
    synthesis_result: dict = state["synthesis_result"]

    msg_id = send_preview_buttons(topic, synthesis_result)
    logger.info("prepare_preview_node: sent preview for topic=%r message_id=%d", topic, msg_id)
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
