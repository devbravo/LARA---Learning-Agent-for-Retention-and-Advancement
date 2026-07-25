"""Tests for the synthesize Map-Reduce robustness behavior.

Covers the failure-tolerance contract of ``synthesize_concept_note``:
- a single failed Map call is skipped, synthesis continues with survivors
- junk (too-short) distillations are discarded before the Reduce step
- all-failures raises RuntimeError (caught upstream by synthesize_node)
- a Reduce response truncated at max_tokens raises instead of returning
  an incomplete note
- the Reduce step is split: Sonnet writes the note (Reduce A), Haiku
  extracts concepts from the finished note (Reduce B)
- both submit tools are declared strict so the API validates tool input
"""

from unittest.mock import MagicMock, patch

import pytest
from anthropic.types import ToolUseBlock

import src.knowledge.synthesize as synth
from src.knowledge.synthesize import (
    _CONCEPTS_TOOL,
    _NOTE_TOOL,
    synthesize_concept_note,
)

_ARTICLE_A = {"url": "https://example.com/a", "content": "content-a"}
_ARTICLE_B = {"url": "https://example.com/b", "content": "content-b"}

# Comfortably above _MIN_DISTILLATION_LENGTH.
_GOOD_DISTILLATION = "- dense technical bullet point about caching\n" * 10

_NOTE_INPUT = {"synthesized_note": "A dense note."}
_CONCEPTS_INPUT = {
    "concepts": ["prefix caching"],
    "proposed_relationships": [
        {"from": "prefix caching", "type": "RELATED_TO", "to": "prefix caching"}
    ],
}


def _good_map_result() -> dict:
    return {"text": _GOOD_DISTILLATION, "input_tokens": 10, "output_tokens": 20}


def _fake_tool_response(
    tool_name: str,
    tool_input: dict,
    stop_reason: str = "tool_use",
    in_tokens: int = 50,
    out_tokens: int = 60,
) -> MagicMock:
    """Build a fake API response carrying a real ToolUseBlock."""
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.usage.input_tokens = in_tokens
    resp.usage.output_tokens = out_tokens
    resp.content = [
        ToolUseBlock(
            type="tool_use", id="toolu_test", name=tool_name, input=tool_input
        )
    ]
    return resp


def _clients_with_reduce(*responses: MagicMock) -> MagicMock:
    """Clients mock whose messages.create returns the given responses in order."""
    clients = MagicMock()
    clients.anthropic.messages.create.side_effect = list(responses)
    return clients


def _happy_reduce_clients() -> MagicMock:
    return _clients_with_reduce(
        _fake_tool_response("submit_note", _NOTE_INPUT),
        _fake_tool_response(
            "submit_concepts", _CONCEPTS_INPUT, in_tokens=5, out_tokens=6
        ),
    )


# ---------------------------------------------------------------------------
# Map fault tolerance
# ---------------------------------------------------------------------------


def test_single_map_failure_is_skipped():
    """One failed Haiku call must not kill synthesis — survivors carry on."""
    clients = _happy_reduce_clients()

    def map_side_effect(topic, content, _clients):
        if content == "content-a":
            raise RuntimeError("simulated Haiku API error")
        return _good_map_result()

    with patch.object(synth, "_map_article", side_effect=map_side_effect):
        result = synthesize_concept_note("caching", [_ARTICLE_A, _ARTICLE_B], clients)

    assert result["source_urls"] == ["https://example.com/b"]
    assert result["synthesized_note"] == "A dense note."
    assert result["concepts"] == ["prefix caching"]


def test_junk_distillation_is_discarded():
    """A too-short distillation is dropped before the Reduce step."""
    clients = _happy_reduce_clients()

    def map_side_effect(topic, content, _clients):
        if content == "content-a":
            return {"text": "nothing here", "input_tokens": 1, "output_tokens": 1}
        return _good_map_result()

    with patch.object(synth, "_map_article", side_effect=map_side_effect):
        result = synthesize_concept_note("caching", [_ARTICLE_A, _ARTICLE_B], clients)

    assert result["source_urls"] == ["https://example.com/b"]
    # Discarded map tokens must not be counted; both reduce calls are.
    assert result["input_tokens"] == 10 + 50 + 5
    assert result["output_tokens"] == 20 + 60 + 6


def test_all_map_failures_raise():
    """No survivors → RuntimeError, and the Reduce step is never called."""
    clients = _happy_reduce_clients()

    with patch.object(
        synth, "_map_article", side_effect=RuntimeError("simulated Haiku API error")
    ):
        with pytest.raises(RuntimeError, match="no usable distillations"):
            synthesize_concept_note("caching", [_ARTICLE_A, _ARTICLE_B], clients)

    clients.anthropic.messages.create.assert_not_called()


def test_empty_input_raises_value_error():
    with pytest.raises(ValueError, match="empty"):
        synthesize_concept_note("caching", [], MagicMock())


# ---------------------------------------------------------------------------
# Split Reduce step
# ---------------------------------------------------------------------------


def test_reduce_is_split_into_note_then_extraction():
    """Reduce A (Sonnet, note) then Reduce B (Haiku, concepts from the note)."""
    clients = _happy_reduce_clients()

    with patch.object(synth, "_map_article", return_value=_good_map_result()):
        result = synthesize_concept_note("caching", [_ARTICLE_A], clients)

    calls = clients.anthropic.messages.create.call_args_list
    assert len(calls) == 2

    note_call, extract_call = calls
    assert note_call.kwargs["model"] == synth._MODEL
    assert note_call.kwargs["tools"] == [_NOTE_TOOL]
    assert extract_call.kwargs["model"] == synth._EXTRACT_MODEL
    assert extract_call.kwargs["tools"] == [_CONCEPTS_TOOL]
    # Reduce B must receive the finished note, not the source articles.
    assert "A dense note." in extract_call.kwargs["messages"][0]["content"]

    assert result["synthesized_note"] == "A dense note."
    assert result["concepts"] == ["prefix caching"]
    assert result["proposed_relationships"] == _CONCEPTS_INPUT["proposed_relationships"]


def test_note_truncation_raises():
    """stop_reason == max_tokens on Reduce A must fail loudly."""
    clients = _clients_with_reduce(
        _fake_tool_response("submit_note", _NOTE_INPUT, stop_reason="max_tokens"),
    )

    with patch.object(synth, "_map_article", return_value=_good_map_result()):
        with pytest.raises(RuntimeError, match="Reduce A.*truncated at max_tokens"):
            synthesize_concept_note("caching", [_ARTICLE_A], clients)


def test_extraction_truncation_raises():
    """stop_reason == max_tokens on Reduce B must also fail loudly."""
    clients = _clients_with_reduce(
        _fake_tool_response("submit_note", _NOTE_INPUT),
        _fake_tool_response(
            "submit_concepts", _CONCEPTS_INPUT, stop_reason="max_tokens"
        ),
    )

    with patch.object(synth, "_map_article", return_value=_good_map_result()):
        with pytest.raises(RuntimeError, match="Reduce B.*truncated at max_tokens"):
            synthesize_concept_note("caching", [_ARTICLE_A], clients)


# ---------------------------------------------------------------------------
# Strict tool schemas
# ---------------------------------------------------------------------------


def test_submit_tools_are_strict():
    """Guard the strict-mode invariants the API requires."""
    for tool in (_NOTE_TOOL, _CONCEPTS_TOOL):
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False

    rel_items = _CONCEPTS_TOOL["input_schema"]["properties"]["proposed_relationships"][
        "items"
    ]
    assert rel_items["additionalProperties"] is False
    assert set(rel_items["required"]) == {"from", "type", "to"}
