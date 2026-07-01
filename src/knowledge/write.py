"""Persistence layer for the knowledge-graph pipeline.

Writes the output of ``synthesize_concept_note`` to two stores:
  * SQLite — one ``concept_notes`` row per synthesis run.
  * Neo4j  — ``Concept`` nodes, a ``ConceptNote`` node, ``DISCUSSES`` edges,
    and ``RELATED_TO`` / ``PREREQUISITE_OF`` edges between concepts.

All Neo4j writes happen inside ONE explicit transaction; a failure at any
point rolls back completely so partial state is never committed.

``resolve_concept`` is called here (not in the caller) so the remap is
built and applied before any database is touched.
"""
import json
import logging

import numpy as np

from src.infrastructure.time import local_now
from src.knowledge.clients import KnowledgeClients, VOYAGE_MODEL
from src.knowledge.resolve import resolve_concept

logger = logging.getLogger(__name__)


def write_concept_note(
    topic: str,
    synthesis_result: dict,
    clients: KnowledgeClients,
) -> dict:
    """Persist a synthesized concept note to SQLite and Neo4j.

    Resolves every extracted concept against the existing graph, remaps all
    relationship endpoints through the resolved names, then writes atomically:
    one SQLite row and one Neo4j transaction covering all nodes and edges.

    Args:
        topic: The search topic keyword (e.g. ``"context management"``).
        synthesis_result: Dict returned by ``synthesize_concept_note``,
            containing ``synthesized_note``, ``concepts``,
            ``proposed_relationships``, and ``source_urls``.
        clients: Shared client container; ``clients.db``, ``clients.neo4j``,
            ``clients.neo4j_database``, and ``clients.voyage`` are used.

    Returns:
        Dict with keys:
        - ``sqlite_id`` (int): Primary key of the inserted ``concept_notes`` row.
        - ``concepts_created`` (list[str]): Resolved names written as new nodes.
        - ``concepts_matched`` (list[str]): Resolved names that already existed.
        - ``edges_by_type`` (dict[str, int]): Edge counts keyed by relationship
          type (``DISCUSSES``, ``RELATED_TO``, ``PREREQUISITE_OF``).

    Raises:
        Exception: If the Neo4j transaction fails, ``clients.db.rollback()`` is
            called before re-raising — leaving both databases clean with no
            partial writes. SQLite commits only after the Neo4j transaction
            succeeds; a Neo4j failure rolls back the SQLite insert.
    """
    raw_concepts: list[str] = synthesis_result["concepts"]
    raw_relationships: list[dict] = synthesis_result["proposed_relationships"]

    # ------------------------------------------------------------------
    # Step 1: Resolve every concept and build the remap dict
    # extracted_name -> resolved_name (existing or new)
    # ------------------------------------------------------------------
    remap: dict[str, str] = {}
    concepts_created: list[str] = []
    concepts_matched: list[str] = []

    for name in raw_concepts:
        result = resolve_concept(name, clients)
        if result["action"] == "matched":
            resolved = result["existing_name"]
            concepts_matched.append(resolved)
            logger.info(
                "resolve: %r → MATCHED %r (similarity=%.4f)",
                name,
                resolved,
                result["similarity"],
            )
        else:
            resolved = name
            concepts_created.append(resolved)
            logger.info("resolve: %r → CREATE new node", name)
        remap[name] = resolved

    # Unique resolved names for graph writes (matched + created, deduped).
    all_resolved_names = list(dict.fromkeys(remap.values()))

    # ------------------------------------------------------------------
    # Step 2: Rewrite all relationship endpoints through the remap
    # ------------------------------------------------------------------
    remapped_rels = [
        {
            "from": remap.get(rel["from"], rel["from"]),
            "type": rel["type"],
            "to": remap.get(rel["to"], rel["to"]),
        }
        for rel in raw_relationships
    ]

    # ------------------------------------------------------------------
    # Step 3: Execute SQLite INSERT — do NOT commit yet.
    # user_interest is intentionally omitted — DEFAULT 'pending' fires.
    # clients.db is used directly (not get_connection()) so we hold the
    # transaction open until Neo4j commits successfully.
    # ------------------------------------------------------------------
    cursor = clients.db.execute(
        """INSERT INTO concept_notes (topic_keyword, synthesized_text, source_urls, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            topic,
            synthesis_result["synthesized_note"],
            json.dumps(synthesis_result["source_urls"]),
            local_now(),
        ),
    )
    sqlite_id: int = cursor.lastrowid

    logger.info("SQLite INSERT executed: id=%d (not yet committed)", sqlite_id)

    # ------------------------------------------------------------------
    # Step 4: Compute embeddings for all resolved concept names.
    # resolve_concept computes embeddings internally but does not expose
    # them, so we make one fresh batch call here for all unique resolved
    # names. One Voyage call is cheaper than re-entering resolve_concept
    # for each concept and discarding its output.
    # ------------------------------------------------------------------
    embeddings_response = clients.voyage.embed(all_resolved_names, model=VOYAGE_MODEL)
    name_to_embedding: dict[str, list[float]] = {
        name: emb
        for name, emb in zip(all_resolved_names, embeddings_response.embeddings)
    }

    # ------------------------------------------------------------------
    # Step 5: Write to Neo4j in ONE explicit transaction
    # ------------------------------------------------------------------
    edges_by_type: dict[str, int] = {"DISCUSSES": 0, "RELATED_TO": 0, "PREREQUISITE_OF": 0}

    with clients.neo4j.session(database=clients.neo4j_database) as neo4j_session:
        tx = neo4j_session.begin_transaction()
        try:
            # MERGE each resolved Concept node and set its embedding.
            for name in all_resolved_names:
                tx.run(
                    "MERGE (c:Concept {name: $name}) SET c.embedding = $embedding",
                    name=name,
                    embedding=name_to_embedding[name],
                )

            # Create ConceptNote node keyed by the SQLite id.
            tx.run(
                """CREATE (n:ConceptNote {id: $id, topic_keyword: $topic})""",
                id=sqlite_id,
                topic=topic,
            )

            # DISCUSSES edges: ConceptNote → each Concept.
            for name in all_resolved_names:
                tx.run(
                    """MATCH (n:ConceptNote {id: $id})
                       MATCH (c:Concept {name: $name})
                       CREATE (n)-[:DISCUSSES]->(c)""",
                    id=sqlite_id,
                    name=name,
                )
                edges_by_type["DISCUSSES"] += 1

            # Concept-to-Concept relationship edges (remapped).
            for rel in remapped_rels:
                rel_type = rel["type"]
                if rel_type not in ("RELATED_TO", "PREREQUISITE_OF"):
                    raise ValueError(
                        f"Unknown relationship type {rel_type!r} — aborting transaction"
                    )
                tx.run(
                    f"""MATCH (a:Concept {{name: $from_name}})
                        MATCH (b:Concept {{name: $to_name}})
                        MERGE (a)-[:{rel_type}]->(b)""",
                    from_name=rel["from"],
                    to_name=rel["to"],
                )
                edges_by_type[rel_type] = edges_by_type.get(rel_type, 0) + 1

            tx.commit()
            logger.info(
                "Neo4j transaction committed: %d concepts, %d DISCUSSES, "
                "%d RELATED_TO, %d PREREQUISITE_OF edges",
                len(all_resolved_names),
                edges_by_type["DISCUSSES"],
                edges_by_type["RELATED_TO"],
                edges_by_type["PREREQUISITE_OF"],
            )

            # Neo4j succeeded — now safe to commit SQLite.
            # If the process dies between these two lines, the Neo4j ConceptNote
            # node will have no SQLite counterpart. This is detectable via:
            #   MATCH (n:ConceptNote) WHERE n.id IS NOT NULL
            # and a SELECT on concept_notes for the missing id.
            clients.db.commit()
            logger.info("SQLite concept_notes row committed: id=%d", sqlite_id)

        except Exception:
            tx.rollback()
            clients.db.rollback()
            logger.exception(
                "Neo4j transaction rolled back; SQLite INSERT rolled back (id=%d discarded)",
                sqlite_id,
            )
            raise

    return {
        "sqlite_id": sqlite_id,
        "concepts_created": concepts_created,
        "concepts_matched": concepts_matched,
        "edges_by_type": edges_by_type,
    }
