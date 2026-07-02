"""Tests for the Knowledge Agent LangGraph graph.

Test 1: graph loads and pauses
  - Invoke graph with topic + chat_id (synthesize_node internals mocked).
  - Verify graph pauses at prepare_confirm_node (has pending interrupt).
  - Verify checkpoint: pending_message_id, topic, chat_id, synthesis_result.

Test 2: approval flow
  - synthesize_node mocked; interrupt() patched to "kg_approve".
  - Verify write_concept_note is called and write_result is set.

Test 3: rejection flow
  - synthesize_node mocked; interrupt() patched to "kg_reject".
  - Verify write_concept_note is NOT called.

Test 4: synthesis failure routes to END without sending buttons.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import src.knowledge.nodes as _nodes
from src.knowledge.graph import _has_pending_interrupt, graph

_CHAT_ID = 9001
_FAKE_MSG_ID = 77

_SYNTHESIS_RESULT = {
    "synthesized_note": "Dense technical note about context management. " * 20,
    "concepts": ["context window", "KV cache", "prefix caching"],
    "proposed_relationships": [
        {"from": "KV cache", "type": "PREREQUISITE_OF", "to": "prefix caching"}
    ],
    "source_urls": ["https://example.com/article-1", "https://example.com/article-2"],
    "input_tokens": 100,
    "output_tokens": 200,
}

_FAKE_ARTICLE = {
    "url": "https://example.com/article-1",
    "title": "Context Management",
    "content": "x" * 500,
}


def _enter_synthesize_mocks(stack: ExitStack, clients=None) -> None:
    """Enter patches for all synthesize_node I/O into an ExitStack."""
    stack.enter_context(patch.object(_nodes._telegram, "send_message"))
    stack.enter_context(patch.object(_nodes, "KnowledgeClients", return_value=clients or MagicMock()))
    stack.enter_context(patch.object(_nodes, "search_blogs_for_topic", return_value=[_FAKE_ARTICLE]))
    stack.enter_context(patch.object(_nodes, "select_top_results", return_value=[_FAKE_ARTICLE]))
    stack.enter_context(patch.object(_nodes, "is_extraction_valid", return_value=(True, "ok")))
    stack.enter_context(patch.object(_nodes, "find_prior_concept_notes", return_value=[]))
    stack.enter_context(patch.object(_nodes, "synthesize_concept_note", return_value=_SYNTHESIS_RESULT))


# ---------------------------------------------------------------------------
# Test 1: graph loads and pauses at prepare_confirm_node
# ---------------------------------------------------------------------------

def test_kg_graph_pauses_at_interrupt():
    """Invoking the KG graph pauses at prepare_confirm_node and checkpoints state."""
    config = {"configurable": {"thread_id": "kg_test_pause_v2"}}
    initial_state = {"topic": "context management", "chat_id": _CHAT_ID}

    with ExitStack() as stack:
        _enter_synthesize_mocks(stack)
        stack.enter_context(
            patch.object(_nodes._telegram, "send_inline_buttons", return_value=_FAKE_MSG_ID)
        )
        graph.invoke(initial_state, config=config)

    snapshot = graph.get_state(config)
    assert _has_pending_interrupt(snapshot), "Graph must be paused at interrupt after invoke"

    values = snapshot.values
    assert values.get("pending_message_id") == _FAKE_MSG_ID
    assert values.get("topic") == "context management"
    assert values.get("chat_id") == _CHAT_ID
    assert values.get("synthesis_result") == _SYNTHESIS_RESULT
    assert snapshot.next == ("prepare_confirm_node",)


# ---------------------------------------------------------------------------
# Test 2: approval flow — write_node reached
# ---------------------------------------------------------------------------

def test_kg_graph_approval_calls_write():
    """On kg_approve, write_concept_note is called and write_result is set."""
    config = {"configurable": {"thread_id": "kg_test_approve_v2"}}
    initial_state = {"topic": "context management", "chat_id": _CHAT_ID}

    fake_write_result = {
        "sqlite_id": 5,
        "concepts_created": ["prefix caching"],
        "concepts_matched": ["context window", "KV cache"],
        "edges_by_type": {"DISCUSSES": 3, "RELATED_TO": 0, "PREREQUISITE_OF": 1},
    }

    write_clients = MagicMock()
    write_clients.close = MagicMock()

    with ExitStack() as stack:
        _enter_synthesize_mocks(stack)
        stack.enter_context(
            patch.object(_nodes._telegram, "send_inline_buttons", return_value=_FAKE_MSG_ID)
        )
        stack.enter_context(patch.object(_nodes._telegram, "remove_buttons"))
        stack.enter_context(patch.object(_nodes, "interrupt", return_value="kg_approve"))
        stack.enter_context(patch.object(_nodes, "KnowledgeClients", return_value=write_clients))
        mock_write = stack.enter_context(
            patch.object(_nodes, "write_concept_note", return_value=fake_write_result)
        )

        result = graph.invoke(initial_state, config=config)

    mock_write.assert_called_once()
    assert mock_write.call_args.args[0] == "context management"
    assert mock_write.call_args.args[1] == _SYNTHESIS_RESULT

    assert result.get("user_interest") == "approved"
    assert result.get("write_result") == fake_write_result
    assert result.get("pending_message_id") is None


# ---------------------------------------------------------------------------
# Test 3: rejection flow — write_node skipped
# ---------------------------------------------------------------------------

def test_kg_graph_rejection_skips_write():
    """On kg_reject, write_concept_note is never called."""
    config = {"configurable": {"thread_id": "kg_test_reject_v2"}}
    initial_state = {"topic": "context management", "chat_id": _CHAT_ID}

    with ExitStack() as stack:
        _enter_synthesize_mocks(stack)
        stack.enter_context(
            patch.object(_nodes._telegram, "send_inline_buttons", return_value=_FAKE_MSG_ID)
        )
        stack.enter_context(patch.object(_nodes._telegram, "remove_buttons"))
        stack.enter_context(patch.object(_nodes, "interrupt", return_value="kg_reject"))
        mock_write = stack.enter_context(patch.object(_nodes, "write_concept_note"))

        result = graph.invoke(initial_state, config=config)

    mock_write.assert_not_called()
    assert result.get("user_interest") == "rejected"
    assert result.get("write_result") is None
    assert result.get("pending_message_id") is None


# ---------------------------------------------------------------------------
# Test 4: synthesis failure routes to END (no buttons sent)
# ---------------------------------------------------------------------------

def test_kg_graph_synthesis_failure_ends_without_buttons():
    """When synthesize_node finds no articles, graph ends without sending buttons."""
    config = {"configurable": {"thread_id": "kg_test_fail_v2"}}
    initial_state = {"topic": "context management", "chat_id": _CHAT_ID}

    with ExitStack() as stack:
        stack.enter_context(patch.object(_nodes._telegram, "send_message"))
        stack.enter_context(patch.object(_nodes, "KnowledgeClients", return_value=MagicMock()))
        stack.enter_context(patch.object(_nodes, "search_blogs_for_topic", return_value=[]))
        stack.enter_context(patch.object(_nodes, "select_top_results", return_value=[]))
        mock_buttons = stack.enter_context(patch.object(_nodes._telegram, "send_inline_buttons"))

        result = graph.invoke(initial_state, config=config)

    mock_buttons.assert_not_called()
    assert result.get("synthesis_result") is None
