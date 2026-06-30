"""Concept note synthesis via Claude API.

The ONE place in the knowledge pipeline that calls the LLM.
Accepts already-extracted article texts; returns a structured dict
with the synthesized note, concept names, and proposed relationships.
No database writes happen here.

Dev runner:
    python -m src.knowledge.synthesize "context management"
"""
import logging
import sys

from src.knowledge.clients import KnowledgeClients
from anthropic.types import TextBlockParam, ToolParam, CacheControlEphemeralParam, ToolChoiceToolParam, MessageParam
from anthropic.types.content_block import ToolUseBlock

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 4096

# Tool definition for structured output — forces Claude to return
# exactly these three fields with no free-form preamble.
_SUBMIT_TOOL: ToolParam = {
    "name": "submit_concept_note",
    "description": (
        "Submit the synthesized concept note, extracted concept names, "
        "and proposed relationships between those concepts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "synthesized_note": {
                "type": "string",
                "description": (
                    "A single, dense study guide synthesizing across ALL provided sources. "
                    "Not per-source summaries — actively cross-reference where sources "
                    "cover the same ground. Aimed at technical interview preparation."
                ),
            },
            "concepts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Concept names extracted from the synthesized note. "
                    "Technique/idea level only (e.g. 'semantic chunking', 'prefix caching'). "
                    "Never code identifiers, library class names, or function names."
                ),
            },
            "proposed_relationships": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["RELATED_TO", "PREREQUISITE_OF"],
                        },
                        "to": {"type": "string"},
                    },
                    "required": ["from", "type", "to"],
                },
                "description": (
                    "Relationships ONLY between concepts in the 'concepts' list above. "
                    "Use RELATED_TO for symmetric associations, PREREQUISITE_OF when "
                    "concept A must be understood before concept B. "
                    "Be selective: only the most significant, non-obvious connections. "
                    "Target fewer than half the concept count in total relationships. "
                    "Only propose one where the source text genuinely supports it."
                ),
            },
        },
        "required": ["synthesized_note", "concepts", "proposed_relationships"],
    },
    "cache_control": {"type": "ephemeral"}
}

_SYSTEM_PROMPT = """\
You are a technical study note synthesizer. You receive one or more article extracts \
on a topic and produce a single Concept Note for a learner preparing for a technical interview.

Rules for the synthesized note:
- Write ONE integrated guide. Actively cross-reference: where sources cover the same concept, \
combine them into one explanation. Where they differ or complement each other, note it explicitly.
- Be dense and precise — this is a study reference, not a summary for a general audience.
- Code examples in the sources are useful grounding. Reference what they DEMONSTRATE \
(the technique or pattern) rather than reproducing the full snippet verbatim.

Rules for concept extraction:
- Technique/idea level only (e.g. "semantic chunking", "prefix caching"). \
Never code identifiers, library class names, or function names.
- Every concept must reflect something the source text actually discusses. \
Do not blend, infer, or combine terminology from different concepts into \
a new compound term that doesn't appear in (or isn't a clear paraphrase of) the source material.
- Do NOT extract two concepts that are near-synonyms within the same note. \
If two terms refer to the same technique, pick the more standard or widely-used name and use only that one. \
Cross-note deduplication is handled elsewhere — here, avoid obvious same-note duplicates.

Rules for proposed relationships:
- Only between concepts in your own list. Only RELATED_TO or PREREQUISITE_OF.
- Be selective: propose only the most significant or non-obvious relationships — \
ones that would genuinely help a learner navigate from one concept to another. \
Do NOT propose a relationship just because two concepts appear in the same section. \
A reasonable target is fewer relationships than half the concept count.
- Only propose a relationship where the source text actually supports a meaningful connection.\
"""


def synthesize_concept_note(
    topic: str,
    extracted_texts: list[dict],
    clients: KnowledgeClients,
) -> dict:
    """Synthesize a Concept Note from one or more extracted article texts.

    Makes ONE Claude API call (tool use) that returns a synthesized study note,
    a list of concept names, and proposed relationships between those concepts.
    No database writes happen here.

    Args:
        topic: The search topic (e.g. ``"context management"``).
        extracted_texts: List of dicts from ``extract_top_results``, each
            containing at minimum ``url`` and ``content`` keys.
        clients: Shared client container; ``clients.anthropic`` is used.

    Returns:
        Dict with keys:
        - ``synthesized_note`` (str): Dense cross-source study guide.
        - ``concepts`` (list[str]): Concept names extracted from the note.
        - ``proposed_relationships`` (list[dict]): Each dict has
          ``from``, ``type`` (RELATED_TO | PREREQUISITE_OF), ``to``.
        - ``source_urls`` (list[str]): URLs of the articles used.
        - ``input_tokens`` (int): Tokens consumed by the prompt.
        - ``output_tokens`` (int): Tokens in Claude's response.
    """
    if not extracted_texts:
        raise ValueError("extracted_texts is empty — nothing to synthesize")

    source_blocks = []
    for i, item in enumerate(extracted_texts, 1):
        source_blocks.append(
            f"--- Source {i}: {item['url']} ---\n{item['content']}\n---"
        )

    user_message = (
        f"Topic: {topic}\n\n"
        f"{len(extracted_texts)} source article(s):\n\n"
        + "\n\n".join(source_blocks)
        + "\n\nSynthesize a Concept Note using the submit_concept_note tool."
    )

    response = clients.anthropic.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=[
            TextBlockParam(
                type="text",
                text=_SYSTEM_PROMPT,
                cache_control=CacheControlEphemeralParam(type="ephemeral")
            )
        ],
        tools=[_SUBMIT_TOOL],
        tool_choice=ToolChoiceToolParam(type="tool", name= "submit_concept_note"),
        messages=[MessageParam(role= "user", content= user_message)],
    )

    tool_block = next(
        (b for b in response.content if b.type == "tool_use"),
        None,
    )
    if tool_block is None or not isinstance(tool_block, ToolUseBlock):
        raise RuntimeError(
            f"Claude did not call submit_concept_note — stop_reason={response.stop_reason}, "
            f"content={response.content!r}"
        )

    result = tool_block.input

    return {
        "synthesized_note": result["synthesized_note"],
        "concepts": result["concepts"],
        "proposed_relationships": result["proposed_relationships"],
        "source_urls": [item["url"] for item in extracted_texts],
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


if __name__ == "__main__":
    from src.knowledge.extract import extract_clean_text, is_extraction_valid
    from src.knowledge.search import (
        search_blogs_for_topic,
        select_top_results,
    )

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "context management"
    print(f"\nTopic: {topic!r}\n")

    clients = KnowledgeClients()
    try:
        # Search → rerank → select
        all_results = search_blogs_for_topic(topic, clients)
        top = select_top_results(topic, all_results, clients)

        print(f"Reranker: {len(top)}/{len(all_results)} passed")
        for r in top:
            print(f"  {r['relevance_score']:.4f}  [{r['blog']}] {r['title']}")

        # Extract + validate
        extracted = []
        for item in top:
            content = extract_clean_text(item["url"], clients)
            if content is None:
                print(f"  SKIP (fetch failed): {item['url']}")
                continue
            ok, reason = is_extraction_valid(content)
            if not ok:
                print(f"  SKIP (invalid: {reason}): {item['url']}")
                continue
            extracted.append({**item, "content": content})

        if not extracted:
            print("No valid extractions — cannot synthesize.")
            sys.exit(1)

        print(f"\nExtracting: {len(extracted)} article(s) for synthesis")
        for e in extracted:
            print(f"  {len(e['content']):,} chars — {e['url']}")

        # Synthesize
        print(f"\nCalling Claude ({_MODEL})…\n")
        result = synthesize_concept_note(topic, extracted, clients)

        cost_note = (
            f"Tokens: {result['input_tokens']:,} in / {result['output_tokens']:,} out"
            f"  (~${result['input_tokens']/1_000_000*3 + result['output_tokens']/1_000_000*15:.4f} USD)"
        )

        print(f"{'═'*60}")
        print(f"SYNTHESIZED NOTE")
        print(f"{'═'*60}")
        print(result["synthesized_note"])

        print(f"\n{'═'*60}")
        print(f"CONCEPTS ({len(result['concepts'])})")
        print(f"{'═'*60}")
        for c in result["concepts"]:
            print(f"  • {c}")

        print(f"\n{'═'*60}")
        print(f"PROPOSED RELATIONSHIPS ({len(result['proposed_relationships'])})")
        print(f"{'═'*60}")
        for rel in result["proposed_relationships"]:
            print(f"  {rel['from']}  —[{rel['type']}]→  {rel['to']}")

        print(f"\n{cost_note}")
        print(f"Sources: {', '.join(result['source_urls'])}")

    finally:
        clients.close()
