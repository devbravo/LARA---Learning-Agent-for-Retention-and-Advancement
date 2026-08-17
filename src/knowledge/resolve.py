"""Concept resolution: match a candidate name against existing Neo4j Concept nodes.

Pure embedding-similarity, no LLM call. Determines whether a concept extracted
during synthesis already exists in the graph under a different phrasing, or is
genuinely new and should be created.
"""

import logging

import numpy as np

from src.knowledge.clients import KnowledgeClients
from src.settings import VOYAGE_MODEL

logger = logging.getLogger(__name__)

# Starting threshold — not validated, tune once real scores are visible.
# Raise if false matches occur; lower if obvious paraphrases are missed.
_DEFAULT_THRESHOLD = 0.75


def resolve_concept(
    concept_name: str,
    clients: KnowledgeClients,
    threshold: float = _DEFAULT_THRESHOLD,
) -> dict:
    """Check a candidate concept name against existing Concept nodes in Neo4j.

    Fetches all existing concept names, embeds the candidate and all existing
    names in two batch calls, then returns the best cosine-similarity match
    above ``threshold``, or instructs the caller to create a new node.

    Args:
        concept_name: The concept name extracted from a synthesis run.
        clients: Shared client container; ``clients.neo4j``, ``clients.neo4j_database``,
            and ``clients.voyage`` are used.
        threshold: Minimum cosine similarity to count as a match (default 0.75).
            Not validated — tune after reviewing real similarity scores.

    Returns:
        One of:
        ``{"action": "matched", "existing_name": str, "similarity": float}``
        if an existing concept is close enough, or
        ``{"action": "create", "name": concept_name}``
        if the concept is new.
    """
    with clients.neo4j.session(database=clients.neo4j_database) as session:
        result = session.run("MATCH (c:Concept) RETURN c.name AS name")
        existing_names = [record["name"] for record in result]

    if not existing_names:
        logger.debug("Graph has no Concept nodes — returning create for %r", concept_name)
        return {"action": "create", "name": concept_name}

    # Two batch embed calls: candidate alone + all existing names.
    # Using a single combined batch would mix query/document roles; Voyage
    # voyage-4-lite doesn't distinguish them, so one call is fine in practice,
    # but keeping them separate makes intent clear and is easy to switch later.
    candidate_emb = np.array(
        clients.voyage.embed([concept_name], model=VOYAGE_MODEL).embeddings[0]
    )
    existing_embs = np.array(
        clients.voyage.embed(existing_names, model=VOYAGE_MODEL).embeddings
    )

    candidate_norm = candidate_emb / np.linalg.norm(candidate_emb)
    existing_norms = existing_embs / np.linalg.norm(existing_embs, axis=1, keepdims=True)
    scores = existing_norms @ candidate_norm

    best_idx = int(np.argmax(scores))
    best_score = float(scores[best_idx])
    best_name = existing_names[best_idx]

    logger.debug(
        "resolve_concept %r: best match %r (%.4f, threshold=%.2f)",
        concept_name,
        best_name,
        best_score,
        threshold,
    )

    if best_score >= threshold:
        return {
            "action": "matched",
            "existing_name": best_name,
            "similarity": best_score,
        }
    return {"action": "create", "name": concept_name}
