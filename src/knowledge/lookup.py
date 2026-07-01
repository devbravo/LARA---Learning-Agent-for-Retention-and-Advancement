"""Pre-synthesis lookup: find existing ConceptNotes relevant to a topic.

Called before ``synthesize_concept_note`` to surface prior knowledge so the
new note can extend rather than duplicate existing coverage.

No writes happen here — read-only across both Neo4j and SQLite.
"""
import logging

import numpy as np

from src.knowledge.clients import KnowledgeClients, VOYAGE_MODEL

logger = logging.getLogger(__name__)

# Starting threshold for topic-to-concept matching.
# Copied from resolve_concept's default (0.75) as a reasonable starting point,
# but topic strings are longer and more varied than concept names — this value
# has not yet been validated on real multi-run data and should be tuned once
# enough runs have accumulated to inspect the actual score distribution.
_DEFAULT_THRESHOLD = 0.75


def find_prior_concept_notes(
    topic: str,
    clients: KnowledgeClients,
    threshold: float = _DEFAULT_THRESHOLD,
) -> list[dict]:
    """Find existing ConceptNotes whose linked concepts match the given topic.

    Embeds the topic string and compares it against every ``Concept`` node
    that has a stored embedding. If the best match clears ``threshold``,
    fetches all ``ConceptNote`` ids linked to that concept via ``DISCUSSES``
    and returns the corresponding ``synthesized_text`` rows from SQLite.

    Args:
        topic: The search topic string (e.g. ``"context management"``).
        clients: Shared client container; ``clients.voyage``, ``clients.neo4j``,
            ``clients.neo4j_database``, and ``clients.db`` are used.
        threshold: Minimum cosine similarity between the topic embedding and a
            concept embedding to count as a match. Default 0.75 — same starting
            value as ``resolve_concept``, not yet validated for topic-to-concept
            matching specifically.

    Returns:
        List of dicts ``[{"concept_note_id": int, "synthesized_text": str}, ...]``
        sorted by ``concept_note_id`` ascending (oldest note first).
        Returns ``[]`` if no concept clears the threshold or the graph is empty.
    """
    # Fetch all Concept nodes that have a stored embedding.
    with clients.neo4j.session(database=clients.neo4j_database) as session:
        records = list(session.run(
            "MATCH (c:Concept) WHERE c.embedding IS NOT NULL "
            "RETURN c.name AS name, c.embedding AS embedding"
        ))

    if not records:
        logger.debug("find_prior_concept_notes: no Concept nodes with embeddings — returning []")
        return []

    existing_names = [r["name"] for r in records]
    existing_embs = np.array([r["embedding"] for r in records], dtype=float)

    # Embed the topic string.
    topic_emb = np.array(
        clients.voyage.embed([topic], model=VOYAGE_MODEL).embeddings[0]
    )

    # Cosine similarity: normalize both sides, then dot product.
    topic_norm = topic_emb / np.linalg.norm(topic_emb)
    existing_norms = existing_embs / np.linalg.norm(existing_embs, axis=1, keepdims=True)
    scores = existing_norms @ topic_norm

    # Log all scores at DEBUG so threshold tuning is visible without noise.
    for name, score in sorted(zip(existing_names, scores), key=lambda x: -x[1]):
        logger.debug("find_prior_concept_notes: %r similarity=%.4f", name, score)

    best_idx = int(np.argmax(scores))
    best_score = float(scores[best_idx])
    best_name = existing_names[best_idx]

    if best_score < threshold:
        logger.info(
            "find_prior_concept_notes: best match %r (%.4f) below threshold %.2f — no prior context",
            best_name,
            best_score,
            threshold,
        )
        return []

    logger.info(
        "find_prior_concept_notes: matched concept %r (similarity=%.4f) — fetching linked ConceptNotes",
        best_name,
        best_score,
    )

    # Fetch all ConceptNote ids linked to the matched concept.
    with clients.neo4j.session(database=clients.neo4j_database) as session:
        note_ids = [
            r["id"]
            for r in session.run(
                "MATCH (n:ConceptNote)-[:DISCUSSES]->(c:Concept {name: $name}) RETURN n.id AS id",
                name=best_name,
            )
        ]

    if not note_ids:
        logger.debug(
            "find_prior_concept_notes: concept %r has no linked ConceptNotes", best_name
        )
        return []

    # Fetch synthesized_text from SQLite for each id.
    # Uses clients.db per project convention — not a new get_connection() call.
    results = []
    for note_id in sorted(note_ids):
        row = clients.db.execute(
            "SELECT synthesized_text FROM concept_notes WHERE id = ?", (note_id,)
        ).fetchone()
        if row is None:
            logger.warning(
                "find_prior_concept_notes: ConceptNote id=%d in Neo4j but missing from SQLite",
                note_id,
            )
            continue
        results.append({"concept_note_id": note_id, "synthesized_text": row["synthesized_text"]})

    logger.info(
        "find_prior_concept_notes: returning %d prior ConceptNote(s) for topic %r",
        len(results),
        topic,
    )
    return results
