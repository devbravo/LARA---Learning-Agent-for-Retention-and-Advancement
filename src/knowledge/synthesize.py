"""Concept note synthesis via Claude API — Map-Reduce architecture.

Map step: each article is independently distilled by Haiku into dense
technical bullet points (cheap, parallel, fast).
Reduce step: Sonnet synthesizes across all distilled extracts into a
single structured Concept Note via tool use.

The ONE place in the knowledge pipeline that calls the LLM.
No database writes happen here.

Dev runner:
    python -m src.knowledge.synthesize "context management"
"""
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from anthropic.types import (
    CacheControlEphemeralParam,
    MessageParam,
    TextBlockParam,
    ToolChoiceToolParam,
    ToolParam,
)
from anthropic.types.content_block import ToolUseBlock

from src.knowledge.clients import KnowledgeClients

logger = logging.getLogger(__name__)

# Map step: cheap, parallel extraction per article.
_MAP_MODEL = "claude-haiku-4-5-20251001"
_MAP_MAX_TOKENS = 2048

# Reduce step: structured synthesis across all distilled extracts.
_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 4096

_MAP_SYSTEM_PROMPT = (
    "You are an expert Machine Learning Infrastructure Engineer. "
    "Your task is rigid information extraction. "
    "Do not write introductory or concluding remarks. "
    "Do not 'summarize'. Extract dense, technical facts."
)

_MAP_USER_TEMPLATE = (
    "Extract the core engineering concepts related to the topic: '{topic}' "
    "from the following article. Follow these strict rules:\n"
    "1) Focus on the 'How' and 'Why': Extract specific algorithms, architecture "
    "patterns, latency/cost trade-offs, and scaling bottlenecks.\n"
    "2) Ignore the fluff: Completely ignore author bios, generic introductions, "
    "marketing speak, and 'Further Reading' links.\n"
    "3) Format: Output ONLY a Markdown list of dense bullet points. "
    "Use sub-bullets for technical depth.\n\n"
    "Article Text:\n{content}"
)

# Tool definition for structured output — forces Sonnet to return
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
                    "The 5–10 most important concepts from the synthesized note. "
                    "Each must be a standalone idea a learner would need to understand independently — "
                    "not a detail or sub-component of another concept already in the list. "
                    "Technique/idea level only (e.g. 'speculative decoding', 'prefix caching'). "
                    "Never code identifiers, library names, author names, or paper titles. "
                    "If two terms refer to the same idea, pick one and drop the other."
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
                    "Be selective: 3–5 relationships maximum, only the most load-bearing connections. "
                    "Skip obvious or trivially implied links. "
                    "Only propose one where the source text explicitly supports it."
                ),
            },
        },
        "required": ["synthesized_note", "concepts", "proposed_relationships"],
    },
    "cache_control": {"type": "ephemeral"},
}

_SYSTEM_PROMPT = """\
You are a technical study note synthesizer. You receive one or more distilled article \
extracts on a topic and produce a single Concept Note for a learner preparing for a \
technical interview.

Rules for the synthesized note:
- Write ONE integrated guide. Actively cross-reference: where sources cover the same concept, \
combine them into one explanation. Where they differ or complement each other, note it explicitly.
- Be dense and precise — this is a study reference, not a summary for a general audience.
- Code examples in the sources are useful grounding. Reference what they DEMONSTRATE \
(the technique or pattern) rather than reproducing the full snippet verbatim.

Rules for concept extraction:
- Extract the 5–10 most important concepts only. Quality over quantity.
- Each concept must be a standalone idea a learner would need to understand independently. \
Drop anything that is a detail, sub-component, or near-synonym of another concept already in your list.
- Technique/idea level only (e.g. "speculative decoding", "prefix caching"). \
Never code identifiers, library class names, author names, or paper titles.
- Every concept must reflect something the source text actually discusses. \
Do not infer or coin new compound terms not present in the sources.
- If two terms refer to the same technique, use the more standard name and drop the other.

Rules for proposed relationships:
- Only between concepts in your own list. Only RELATED_TO or PREREQUISITE_OF.
- 3–5 relationships maximum. Only the most load-bearing connections — \
ones that would genuinely help a learner understand the dependency between two ideas. \
Skip anything obvious or trivially implied.
- Only propose a relationship where the source text explicitly supports it.

If prior notes are provided:
- Treat them as established knowledge the learner already has.
- Focus the new note on what is genuinely new, different, or deeper \
in the current sources versus the prior notes.
- Explicitly flag where the new sources confirm, extend, or contradict \
prior coverage. Do not silently re-summarize what prior notes already say.\
"""


def _map_article(topic: str, content: str, clients: KnowledgeClients) -> dict:
    """Distill a single article into dense technical bullet points via Haiku.

    This is the Map step of the Map-Reduce pipeline. Runs in parallel across
    all articles before the Reduce (Sonnet) step sees any of them.

    Args:
        topic: The search topic used to focus extraction.
        content: Raw article text from ``extract_top_results``.
        clients: Shared client container; ``clients.anthropic`` is used.

    Returns:
        Dict with keys:
        - ``text`` (str): Distilled Markdown bullet points.
        - ``input_tokens`` (int): Haiku prompt tokens consumed.
        - ``output_tokens`` (int): Haiku response tokens consumed.
    """
    response = clients.anthropic.messages.create(
        model=_MAP_MODEL,
        max_tokens=_MAP_MAX_TOKENS,
        system=_MAP_SYSTEM_PROMPT,
        messages=[
            MessageParam(
                role="user",
                content=_MAP_USER_TEMPLATE.format(topic=topic, content=content),
            )
        ],
    )
    return {
        "text": response.content[0].text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def synthesize_concept_note(
    topic: str,
    extracted_texts: list[dict],
    clients: KnowledgeClients,
    prior_notes: list[dict] | None = None,
) -> dict:
    """Synthesize a Concept Note from one or more extracted article texts.

    Uses a Map-Reduce architecture:
    1. **Map**: Each article is independently distilled by Haiku into dense
       technical bullet points. All map calls run in parallel via
       ``ThreadPoolExecutor``. Prior notes are NOT passed to the Map step —
       they are raw-article distillation only.
    2. **Reduce**: Sonnet receives the combined distilled extracts (and any
       prior notes) and produces a structured Concept Note via tool use.

    No database writes happen here.

    Args:
        topic: The search topic (e.g. ``"context management"``).
        extracted_texts: List of dicts from ``extract_top_results``, each
            containing at minimum ``url`` and ``content`` keys.
        clients: Shared client container; ``clients.anthropic`` is used.
        prior_notes: Optional output of ``find_prior_concept_notes`` — list of
            dicts with ``concept_note_id`` and ``synthesized_text``. When
            non-empty, the Reduce step is instructed to extend rather than
            duplicate prior coverage. Defaults to ``None`` (no prior context).

    Returns:
        Dict with keys:
        - ``synthesized_note`` (str): Dense cross-source study guide.
        - ``concepts`` (list[str]): Concept names extracted from the note.
        - ``proposed_relationships`` (list[dict]): Each dict has
          ``from``, ``type`` (RELATED_TO | PREREQUISITE_OF), ``to``.
        - ``source_urls`` (list[str]): URLs of the articles used.
        - ``input_tokens`` (int): Total tokens across both Haiku and Sonnet calls.
        - ``output_tokens`` (int): Total tokens across both Haiku and Sonnet calls.
    """
    if not extracted_texts:
        raise ValueError("extracted_texts is empty — nothing to synthesize")

    # --- Map step: distil each article in parallel ---
    n = len(extracted_texts)
    mapped: list[dict | None] = [None] * n

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = {
            executor.submit(_map_article, topic, item["content"], clients): i
            for i, item in enumerate(extracted_texts)
        }
        for future in as_completed(futures):
            idx = futures[future]
            mapped[idx] = future.result()

    total_input_tokens = sum(m["input_tokens"] for m in mapped)
    total_output_tokens = sum(m["output_tokens"] for m in mapped)

    logger.info(
        "Map step complete: %d articles, %d in / %d out tokens (Haiku)",
        n,
        total_input_tokens,
        total_output_tokens,
    )

    # --- Reduce step: synthesize across all distilled extracts ---
    distilled_blocks = []
    for i, (item, result) in enumerate(zip(extracted_texts, mapped), 1):
        distilled_blocks.append(
            f"--- Distilled Source {i}: {item['url']} ---\n{result['text']}\n"
        )

    prior_block = ""
    if prior_notes:
        prior_lines = [
            f"--- Prior knowledge on this topic ({len(prior_notes)} existing note(s)) ---"
        ]
        for i, p in enumerate(prior_notes, 1):
            prior_lines.append(
                f"--- Prior Note {i} (ConceptNote id={p['concept_note_id']}) ---\n"
                f"{p['synthesized_text']}\n---"
            )
        prior_block = "\n".join(prior_lines) + "\n\n"

    extension_instruction = (
        "\nWhen prior notes are provided, your new note MUST explicitly extend, "
        "contradict, or confirm what is already known. Do not re-explain concepts "
        "already covered unless the new sources add meaningfully different detail."
        if prior_notes
        else ""
    )

    user_message = (
        f"Topic: {topic}\n\n"
        + prior_block
        + f"{n} distilled source article(s):\n\n"
        + "\n".join(distilled_blocks)
        + "\nSynthesize a Concept Note using the submit_concept_note tool."
        + extension_instruction
    )

    response = clients.anthropic.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=[
            TextBlockParam(
                type="text",
                text=_SYSTEM_PROMPT,
                cache_control=CacheControlEphemeralParam(type="ephemeral"),
            )
        ],
        tools=[_SUBMIT_TOOL],
        tool_choice=ToolChoiceToolParam(type="tool", name="submit_concept_note"),
        messages=[MessageParam(role="user", content=user_message)],
    )

    total_input_tokens += response.usage.input_tokens
    total_output_tokens += response.usage.output_tokens

    logger.info(
        "Reduce step complete: %d in / %d out tokens (Sonnet)",
        response.usage.input_tokens,
        response.usage.output_tokens,
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

    concepts = result.get("concepts", [])
    relationships = result.get("proposed_relationships", [])

    if not concepts:
        logger.warning(
            "synthesize_concept_note: Claude returned no concepts for topic=%r "
            "(raw keys: %s)",
            topic,
            list(result.keys()),
        )

    return {
        "synthesized_note": result["synthesized_note"],
        "concepts": concepts,
        "proposed_relationships": relationships,
        "source_urls": [item["url"] for item in extracted_texts],
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }


if __name__ == "__main__":
    from src.knowledge.extract import is_extraction_valid
    from src.knowledge.lookup import find_prior_concept_notes
    from src.knowledge.search import search_blogs_for_topic, select_top_results

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

        # Validate pre-fetched content (no r.jina.ai call needed — content
        # already arrived in the search response payload)
        extracted = []
        for item in top:
            content = item.get("content", "")
            ok, reason = is_extraction_valid(content)
            if not ok:
                print(f"  SKIP (invalid: {reason}): {item['url']}")
                continue
            extracted.append(item)

        if not extracted:
            print("No valid extractions — cannot synthesize.")
            sys.exit(1)

        print(f"\nArticles for synthesis: {len(extracted)}")
        for e in extracted:
            print(f"  {len(e['content']):,} chars — {e['url']}")

        # Prior context lookup
        prior_notes = find_prior_concept_notes(topic, clients)
        if prior_notes:
            print(f"\nPrior context: {len(prior_notes)} existing ConceptNote(s) matched")
            for p in prior_notes:
                print(f"  ConceptNote id={p['concept_note_id']} "
                      f"({len(p['synthesized_text'])} chars)")
        else:
            print("\nNo prior context found — synthesizing from scratch")

        # Map-Reduce synthesis
        print(f"\nMap step  → {_MAP_MODEL} × {len(extracted)} articles (parallel)…")
        print(f"Reduce step → {_MODEL}…\n")
        result = synthesize_concept_note(topic, extracted, clients, prior_notes=prior_notes)

        # Cost breakdown: Haiku at $0.80/$4 per M, Sonnet at $3/$15 per M.
        # We only have totals here, so report combined with a note.
        cost_note = (
            f"Tokens (combined): {result['input_tokens']:,} in / {result['output_tokens']:,} out"
        )

        print(f"{'═'*60}")
        print("SYNTHESIZED NOTE")
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
