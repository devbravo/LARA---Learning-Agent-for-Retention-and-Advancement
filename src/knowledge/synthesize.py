"""Concept note synthesis via Claude API — Map-Reduce architecture.

Map step: each article is independently distilled by Haiku into dense
technical bullet points (cheap, parallel, fast).
Reduce A: Sonnet synthesizes across all distilled extracts into a
single Concept Note via strict tool use.
Reduce B: Haiku extracts concepts + relationships from the finished note.

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
    Usage,
)
from anthropic.types.content_block import ToolUseBlock

from src.knowledge.clients import KnowledgeClients

logger = logging.getLogger(__name__)

# Map step: cheap, parallel extraction per article.
_MAP_MODEL = "claude-haiku-4-5-20251001"
_MAP_MAX_TOKENS = 2048

# Reduce A: note synthesis across all distilled extracts.
_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 8192

# Reduce B: concept/relationship extraction from the finished note.
# Cheap — input is just the note, so Haiku is plenty.
_EXTRACT_MODEL = _MAP_MODEL
_EXTRACT_MAX_TOKENS = 2048

# Distillations shorter than this are junk (refusal, empty page, error text) —
# too short to be a real technical bullet list from a validated article.
_MIN_DISTILLATION_LENGTH = 200

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

# The Reduce step is split into two focused calls so each does one job well:
#   Reduce A (Sonnet) — write the note. Full attention on cross-referencing.
#   Reduce B (Haiku)  — extract concepts + relationships FROM the finished
#     note, guaranteeing every concept is grounded in what the note actually
#     says (not in source material the note may have dropped).
# ``strict`` makes the API guarantee each tool input validates against its
# schema exactly, so malformed relationships can't reach write_node.

_NOTE_TOOL: ToolParam = {
    "name": "submit_note",
    "description": "Submit the synthesized concept note.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "synthesized_note": {
                "type": "string",
                "description": (
                    "A single, dense study guide synthesizing across ALL provided sources. "
                    "Not per-source summaries — actively cross-reference where sources "
                    "cover the same ground. Aimed at technical interview preparation."
                ),
            },
        },
        "required": ["synthesized_note"],
    },
    "cache_control": {"type": "ephemeral"},
}

_CONCEPTS_TOOL: ToolParam = {
    "name": "submit_concepts",
    "description": (
        "Submit the concept names and proposed relationships extracted "
        "from the concept note."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "concepts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The 5–10 most important concepts from the note. "
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
                    "additionalProperties": False,
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
                    "Only propose one where the note explicitly supports it."
                ),
            },
        },
        "required": ["concepts", "proposed_relationships"],
    },
}

_SYNTH_SYSTEM_PROMPT = """\
You are a technical study note synthesizer. You receive one or more distilled article \
extracts on a topic and produce a single Concept Note for a learner preparing for a \
technical interview.

Rules for the synthesized note:
- Write ONE integrated guide. Actively cross-reference: where sources cover the same concept, \
combine them into one explanation. Where they differ or complement each other, note it explicitly.
- Be dense and precise — this is a study reference, not a summary for a general audience.
- Code examples in the sources are useful grounding. Reference what they DEMONSTRATE \
(the technique or pattern) rather than reproducing the full snippet verbatim.

If prior notes are provided:
- Treat them as established knowledge the learner already has.
- Focus the new note on what is genuinely new, different, or deeper \
in the current sources versus the prior notes.
- Explicitly flag where the new sources confirm, extend, or contradict \
prior coverage. Do not silently re-summarize what prior notes already say.\
"""

_EXTRACT_SYSTEM_PROMPT = """\
You are a concept extractor. You receive a finished technical Concept Note and \
extract the concepts it teaches plus the relationships between them.

Rules for concept extraction:
- Extract the 5–10 most important concepts only. Quality over quantity.
- Each concept must be a standalone idea a learner would need to understand independently. \
Drop anything that is a detail, sub-component, or near-synonym of another concept already in your list.
- Technique/idea level only (e.g. "speculative decoding", "prefix caching"). \
Never code identifiers, library class names, author names, or paper titles.
- Every concept must reflect something the note actually discusses. \
Do not infer or coin new compound terms not present in the note.
- If two terms refer to the same technique, use the more standard name and drop the other.

Rules for proposed relationships:
- Only between concepts in your own list. Only RELATED_TO or PREREQUISITE_OF.
- 3–5 relationships maximum. Only the most load-bearing connections — \
ones that would genuinely help a learner understand the dependency between two ideas. \
Skip anything obvious or trivially implied.
- Only propose a relationship where the note explicitly supports it.\
"""


def _is_distillation_valid(text: str) -> tuple[bool, str]:
    """Sanity-check a Haiku distillation before it enters the Reduce step.

    Deterministic formula check — no LLM call. A distillation from a
    pre-validated article should be a substantive Markdown bullet list;
    very short output means the model had nothing to extract (empty page
    slipped through, refusal, error text).

    Args:
        text: Distilled Markdown from ``_map_article``.

    Returns:
        ``(True, "")`` if the distillation looks substantive.
        ``(False, reason)`` with a human-readable explanation otherwise.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_DISTILLATION_LENGTH:
        return False, (
            f"too short ({len(stripped)} chars, min {_MIN_DISTILLATION_LENGTH})"
        )
    return True, ""


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


def _forced_tool_call(
    clients: KnowledgeClients,
    *,
    model: str,
    max_tokens: int,
    system: str | list[TextBlockParam],
    tool: ToolParam,
    user_message: str,
    step: str,
    topic: str,
) -> tuple[dict, Usage]:
    """Make a forced tool-use call and return the validated tool input.

    Shared by both Reduce calls. Fails loudly on truncation — a truncated
    tool call would otherwise produce an incomplete result that could be
    written to the graph as if it were complete.

    Args:
        clients: Shared client container; ``clients.anthropic`` is used.
        model: Model ID for this call.
        max_tokens: Output token cap for this call.
        system: System prompt (plain string or content-block list).
        tool: Strict tool definition the model is forced to call.
        user_message: The user-turn content.
        step: Human-readable step name for error messages (e.g.
            ``"Reduce A (note synthesis)"``).
        topic: The search topic, for error messages.

    Returns:
        Tuple of (tool input dict, usage object for this call).

    Raises:
        RuntimeError: If the response was truncated at ``max_tokens`` or
            the model did not call the tool.
    """
    response = clients.anthropic.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice=ToolChoiceToolParam(type="tool", name=tool["name"]),
        messages=[MessageParam(role="user", content=user_message)],
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"{step} truncated at max_tokens={max_tokens} for topic {topic!r} "
            f"— refusing to use an incomplete result"
        )

    tool_block = next(
        (b for b in response.content if b.type == "tool_use"),
        None,
    )
    if tool_block is None or not isinstance(tool_block, ToolUseBlock):
        raise RuntimeError(
            f"{step}: Claude did not call {tool['name']} — "
            f"stop_reason={response.stop_reason}, content={response.content!r}"
        )

    return tool_block.input, response.usage


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
       they are raw-article distillation only. A failed or junk distillation
       is logged and skipped; synthesis proceeds with whatever survives.
    2. **Reduce A**: Sonnet receives the combined distilled extracts (and any
       prior notes) and writes the Concept Note via strict tool use — full
       attention on cross-referencing sources, nothing else.
    3. **Reduce B**: Haiku extracts concepts and relationships from the
       *finished* note via strict tool use, guaranteeing every concept is
       grounded in what the note actually says.

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
        - ``source_urls`` (list[str]): URLs of the articles that actually
          survived the Map step and fed the note.
        - ``input_tokens`` (int): Total tokens across both Haiku and Sonnet calls.
        - ``output_tokens`` (int): Total tokens across both Haiku and Sonnet calls.

    Raises:
        ValueError: If ``extracted_texts`` is empty.
        RuntimeError: If every Map distillation fails or is junk, if either
            Reduce call is truncated at its ``max_tokens``, or if Claude
            does not call the forced tool.
    """
    if not extracted_texts:
        raise ValueError("extracted_texts is empty — nothing to synthesize")

    # --- Map step: distil each article in parallel ---
    # A single failed or junk distillation must not kill the whole synthesis:
    # log it, skip the article, and continue with whatever survives.
    n = len(extracted_texts)
    mapped: list[dict | None] = [None] * n

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = {
            executor.submit(_map_article, topic, item["content"], clients): i
            for i, item in enumerate(extracted_texts)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                mapped[idx] = future.result()
            except Exception:
                logger.exception(
                    "Map step failed for %s — skipping article",
                    extracted_texts[idx]["url"],
                )

    survivors: list[tuple[dict, dict]] = []
    for item, result in zip(extracted_texts, mapped):
        if result is None:
            continue
        ok, reason = _is_distillation_valid(result["text"])
        if not ok:
            logger.warning(
                "Discarding distillation of %s (%s)", item["url"], reason
            )
            continue
        survivors.append((item, result))

    if not survivors:
        raise RuntimeError(
            f"Map step produced no usable distillations for topic {topic!r} "
            f"({n} article(s) attempted)"
        )

    total_input_tokens = sum(m["input_tokens"] for _, m in survivors)
    total_output_tokens = sum(m["output_tokens"] for _, m in survivors)

    logger.info(
        "Map step complete: %d/%d articles survived, %d in / %d out tokens (Haiku)",
        len(survivors),
        n,
        total_input_tokens,
        total_output_tokens,
    )

    # --- Build the Reduce A user message ---
    distilled_blocks = []
    for i, (item, result) in enumerate(survivors, 1):
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

    synth_message = (
        f"Topic: {topic}\n\n"
        + prior_block
        + f"{len(survivors)} distilled source article(s):\n\n"
        + "\n".join(distilled_blocks)
        + "\nSynthesize a Concept Note using the submit_note tool."
        + extension_instruction
    )

    # --- Reduce A: synthesize the note (Sonnet, full attention on the note) ---
    synth_result, synth_usage = _forced_tool_call(
        clients,
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=[
            TextBlockParam(
                type="text",
                text=_SYNTH_SYSTEM_PROMPT,
                cache_control=CacheControlEphemeralParam(type="ephemeral"),
            )
        ],
        tool=_NOTE_TOOL,
        user_message=synth_message,
        step="Reduce A (note synthesis)",
        topic=topic,
    )
    note: str = synth_result["synthesized_note"]

    total_input_tokens += synth_usage.input_tokens
    total_output_tokens += synth_usage.output_tokens

    logger.info(
        "Reduce A complete: %d in / %d out tokens (Sonnet)",
        synth_usage.input_tokens,
        synth_usage.output_tokens,
    )

    # --- Reduce B: extract concepts from the finished note (Haiku, cheap) ---
    # Grounding guarantee: concepts come from what the note actually says,
    # not from source material the note may have dropped.
    extract_message = (
        f"Topic: {topic}\n\nConcept note:\n{note}\n\n"
        "Extract concepts and relationships using the submit_concepts tool."
    )
    extract_result, extract_usage = _forced_tool_call(
        clients,
        model=_EXTRACT_MODEL,
        max_tokens=_EXTRACT_MAX_TOKENS,
        system=_EXTRACT_SYSTEM_PROMPT,
        tool=_CONCEPTS_TOOL,
        user_message=extract_message,
        step="Reduce B (concept extraction)",
        topic=topic,
    )

    total_input_tokens += extract_usage.input_tokens
    total_output_tokens += extract_usage.output_tokens

    logger.info(
        "Reduce B complete: %d in / %d out tokens (Haiku)",
        extract_usage.input_tokens,
        extract_usage.output_tokens,
    )

    concepts = extract_result.get("concepts", [])
    relationships = extract_result.get("proposed_relationships", [])

    if not concepts:
        logger.warning(
            "synthesize_concept_note: no concepts extracted for topic=%r "
            "(raw keys: %s)",
            topic,
            list(extract_result.keys()),
        )

    return {
        "synthesized_note": note,
        "concepts": concepts,
        "proposed_relationships": relationships,
        "source_urls": [item["url"] for item, _ in survivors],
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
        print(f"Reduce A  → {_MODEL} (note)…")
        print(f"Reduce B  → {_EXTRACT_MODEL} (concepts)…\n")
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
