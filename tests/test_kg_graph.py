"""Tests for the Knowledge Agent LangGraph graph.

Test 1: graph loads and pauses
  - Invoke graph with a fake synthesis_result.
  - Verify GraphInterrupt is raised (graph paused at prepare_confirm_node).
  - Verify checkpoint state: pending_message_id set, topic/chat_id preserved.

Test 2: approval flow (interrupt bypassed via patch)
  - Patch interrupt() to return "kg_approve".
  - Verify write_node is reached and write_concept_note is called.

Test 3: rejection flow (interrupt bypassed via patch)
  - Patch interrupt() to return "kg_reject".
  - Verify write_concept_note is NOT called.
"""

from unittest.mock import MagicMock, call, patch

import pytest

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
}


# ---------------------------------------------------------------------------
# Test 1: graph loads and pauses at prepare_confirm_node
# ---------------------------------------------------------------------------

def test_kg_graph_pauses_at_interrupt():
    """Invoking the KG graph raises GraphInterrupt and checkpoints state."""
    config = {"configurable": {"thread_id": "kg_test_pause"}}
    initial_state = {
        "topic": "context management",
        "chat_id": _CHAT_ID,
        "synthesis_result": _SYNTHESIS_RESULT,
    }

    with patch.object(_nodes._telegram, "send_inline_buttons", return_value=_FAKE_MSG_ID):
        graph.invoke(initial_state, config=config)

    # Graph is paused — checkpoint should show a pending interrupt.
    snapshot = graph.get_state(config)
    assert _has_pending_interrupt(snapshot), "Graph must be paused at interrupt after first invoke"

    # State written by prepare_preview_node must be in the checkpoint.
    values = snapshot.values
    assert values.get("pending_message_id") == _FAKE_MSG_ID
    assert values.get("topic") == "context management"
    assert values.get("chat_id") == _CHAT_ID
    assert values.get("synthesis_result") == _SYNTHESIS_RESULT

    # Next node to run on resume must be prepare_confirm_node.
    assert snapshot.next == ("prepare_confirm_node",)


# ---------------------------------------------------------------------------
# Test 2: approval flow — interrupt bypassed, write_node reached
# ---------------------------------------------------------------------------

def test_kg_graph_approval_calls_write():
    """On kg_approve, write_concept_note is called and write_result is set."""
    config = {"configurable": {"thread_id": "kg_test_approve"}}
    initial_state = {
        "topic": "context management",
        "chat_id": _CHAT_ID,
        "synthesis_result": _SYNTHESIS_RESULT,
    }

    fake_write_result = {
        "sqlite_id": 5,
        "concepts_created": ["prefix caching"],
        "concepts_matched": ["context window", "KV cache"],
        "edges_by_type": {"DISCUSSES": 3, "RELATED_TO": 0, "PREREQUISITE_OF": 1},
    }

    with (
        patch.object(_nodes._telegram, "send_inline_buttons", return_value=_FAKE_MSG_ID),
        patch.object(_nodes._telegram, "remove_buttons"),
        patch.object(_nodes._telegram, "send_message"),
        patch("src.knowledge.nodes.interrupt", return_value="kg_approve"),
        patch("src.knowledge.nodes.KnowledgeClients") as mock_clients_cls,
        patch("src.knowledge.nodes.write_concept_note", return_value=fake_write_result) as mock_write,
    ):
        mock_clients_cls.return_value.__enter__ = MagicMock(return_value=mock_clients_cls.return_value)
        mock_clients_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_clients_cls.return_value.close = MagicMock()

        result = graph.invoke(initial_state, config=config)

    mock_write.assert_called_once()
    call_args = mock_write.call_args
    assert call_args.args[0] == "context management"   # topic
    assert call_args.args[1] == _SYNTHESIS_RESULT      # synthesis_result

    assert result.get("user_interest") == "approved"
    assert result.get("write_result") == fake_write_result
    assert result.get("pending_message_id") is None


# ---------------------------------------------------------------------------
# Test 3: rejection flow — write_node skipped
# ---------------------------------------------------------------------------

def test_kg_graph_rejection_skips_write():
    """On kg_reject, write_concept_note is never called."""
    config = {"configurable": {"thread_id": "kg_test_reject"}}
    initial_state = {
        "topic": "context management",
        "chat_id": _CHAT_ID,
        "synthesis_result": _SYNTHESIS_RESULT,
    }

    with (
        patch.object(_nodes._telegram, "send_inline_buttons", return_value=_FAKE_MSG_ID),
        patch.object(_nodes._telegram, "remove_buttons"),
        patch("src.knowledge.nodes.interrupt", return_value="kg_reject"),
        patch("src.knowledge.nodes.write_concept_note") as mock_write,
    ):
        result = graph.invoke(initial_state, config=config)

    mock_write.assert_not_called()
    assert result.get("user_interest") == "rejected"
    assert result.get("write_result") is None
    assert result.get("pending_message_id") is None
