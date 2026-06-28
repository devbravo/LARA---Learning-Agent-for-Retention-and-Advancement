"""Topic-scoped blog search via Jina AI search API.

Reads blog sources from the ``sources`` SQLite table — no hardcoded URLs here.
All search results are returned as plain dicts; no database writes happen here.

Dev runner:
    python -m src.knowledge.search "context management"
"""

import logging
import sys
from itertools import zip_longest
from urllib.parse import urlparse

import numpy as np
import requests

from src.knowledge.clients import KnowledgeClients

logger = logging.getLogger(__name__)

_JINA_SEARCH_URL = "https://s.jina.ai/"
_RESULTS_PER_BLOG = 5

# Starting threshold — not validated, tuned by eye on early test runs.
# Raise if off-topic results are slipping through; lower if relevant
# results with semantically shifted wording are being dropped.
_DEFAULT_SIMILARITY_THRESHOLD = 0.45


def search_blogs_for_topic(topic: str, clients: KnowledgeClients) -> list[dict]:
    """Search each configured blog for the given topic via Jina AI search.

    Runs one site-scoped search per blog (one request per source row) and
    interleaves results so the merged list cycles across blogs before going
    deeper into any one source. Never combines domains into a single OR
    query — that makes result ordering unpredictable.

    Args:
        topic: The keyword or phrase to search for (e.g. ``"context management"``).
        clients: Shared client container; ``clients.jina_api_key`` and
            ``clients.db`` are used here.

    Returns:
        List of result dicts, each containing:
        ``title``, ``url``, ``snippet``, ``blog`` (source name).
        Order: round-robin across blogs by rank (rank-1 from each, then
        rank-2 from each, etc.).
    """
    sources = clients.db.execute(
        "SELECT name, site_url FROM sources ORDER BY id"
    ).fetchall()

    headers = {
        "Authorization": f"Bearer {clients.jina_api_key}",
        "Accept": "application/json",
    }

    per_blog: list[list[dict]] = []
    for source in sources:
        blog_name = source["name"]
        domain = urlparse(source["site_url"]).netloc
        query = f"site:{domain} {topic}"

        logger.info("Searching %s: %r", blog_name, query)
        try:
            resp = requests.get(
                _JINA_SEARCH_URL,
                params={"q": query},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("Search failed for %s (%s): %s", blog_name, query, exc)
            per_blog.append([])
            continue

        results = data.get("data", [])[:_RESULTS_PER_BLOG]
        per_blog.append([
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
                "blog": blog_name,
            }
            for item in results
        ])

    merged = [
        item
        for round_ in zip_longest(*per_blog)
        for item in round_
        if item is not None
    ]
    return merged


def filter_relevant_results(
    results: list[dict],
    topic: str,
    clients: KnowledgeClients,
    threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Filter search results by embedding similarity to the topic.

    Embeds the topic and each result's (title + snippet) in two batch calls,
    then computes cosine similarity. Results below ``threshold`` are dropped;
    survivors are returned sorted by similarity descending.

    Uses Voyage ``voyage-4-lite`` embeddings — NOT an LLM judgment call.
    The Claude-only-in-synthesis convention is preserved.

    .. warning::
        ``threshold=0.5`` is a starting guess, not a validated value.
        Check the printed scores after each real run and adjust if
        off-topic results slip through or relevant ones are dropped.

    Args:
        results: Output of ``search_blogs_for_topic``.
        topic: The original search topic string; embedded once as the query.
        clients: Shared client container; ``clients.voyage`` is used.
        threshold: Minimum cosine similarity to keep a result (default 0.5).

    Returns:
        Filtered and similarity-sorted list of result dicts, each with a
        ``similarity`` key (float) added. May be empty if nothing passes.
    """
    if not results:
        return []

    from src.knowledge.clients import VOYAGE_MODEL

    # Embed topic + all result texts in two batch calls (not N+1 individual calls).
    topic_vec = np.array(
        clients.voyage.embed([topic], model=VOYAGE_MODEL).embeddings[0]
    )

    texts = [f"{r['title']} {r['snippet']}" for r in results]
    result_vecs = np.array(
        clients.voyage.embed(texts, model=VOYAGE_MODEL).embeddings
    )

    # Cosine similarity: dot product of unit vectors.
    topic_norm = topic_vec / np.linalg.norm(topic_vec)
    result_norms = result_vecs / np.linalg.norm(result_vecs, axis=1, keepdims=True)
    scores = result_norms @ topic_norm

    scored = [
        {**result, "similarity": float(score)}
        for result, score in zip(results, scores)
    ]
    survivors = [r for r in scored if r["similarity"] >= threshold]
    survivors.sort(key=lambda r: r["similarity"], reverse=True)
    return survivors


def select_top_results(results: list[dict], n: int = 3) -> list[dict]:
    """Return the first ``n`` results from a filtered, sorted list.

    Intended to operate on the output of ``filter_relevant_results``, which
    already sorts by similarity descending. Taking the head picks the most
    relevant survivors across blogs.

    Args:
        results: Output of ``filter_relevant_results``.
        n: Number of results to keep (default 3).

    Returns:
        Sublist of the first ``n`` entries, or fewer if the list is shorter.
    """
    return results[:n]


if __name__ == "__main__":
    from src.knowledge.extract import extract_top_results

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "context management"
    threshold = _DEFAULT_SIMILARITY_THRESHOLD
    print(f"\nTopic    : {topic!r}")
    print(f"Threshold: {threshold}\n")

    clients = KnowledgeClients()
    try:
        all_results = search_blogs_for_topic(topic, clients)

        print(f"{'─'*60}")
        print(f"All search results ({len(all_results)} total)")
        print(f"{'─'*60}")
        for i, r in enumerate(all_results, 1):
            print(f"[{i:2}] [{r['blog']}]")
            print(f"     {r['title']}")
            print(f"     {r['url']}")
            snippet = r['snippet'][:100] + ("…" if len(r['snippet']) > 100 else "")
            print(f"     {snippet}")

        # Filter with scores — show ALL candidates including filtered-out ones.
        from src.knowledge.search import filter_relevant_results
        scored = filter_relevant_results(all_results, topic, clients, threshold=0.0)

        print(f"\n{'─'*60}")
        print(f"Similarity scores (threshold={threshold})")
        print(f"{'─'*60}")
        for r in scored:
            verdict = "PASS" if r["similarity"] >= threshold else "FAIL"
            print(f"  [{verdict}] {r['similarity']:.4f}  [{r['blog']}] {r['title']}")

        survivors = [r for r in scored if r["similarity"] >= threshold]
        top = select_top_results(survivors)

        print(f"\n{'─'*60}")
        print(f"Top {len(top)} after filter (of {len(survivors)} survivors)")
        print(f"{'─'*60}")
        for i, r in enumerate(top, 1):
            print(f"[{i}] {r['similarity']:.4f}  [{r['blog']}] {r['title']}")
            print(f"    {r['url']}")

        if not top:
            print("No results passed the threshold — nothing to extract.")
        else:
            from src.knowledge.extract import extract_clean_text, is_extraction_valid

            print(f"\n{'─'*60}")
            print(f"Extraction + validation ({len(top)} candidates)")
            print(f"{'─'*60}")
            valid_items = []
            for item in top:
                url = item["url"]
                print(f"\n  [{item['blog']}] {item['title']}")
                print(f"  {url}")
                content = extract_clean_text(url, clients)
                if content is None:
                    print(f"  SKIP — fetch failed")
                    continue
                ok, reason = is_extraction_valid(content)
                if not ok:
                    print(f"  SKIP — invalid: {reason}")
                    print(f"  (first 200 chars: {content[:200]!r})")
                else:
                    print(f"  PASS — {len(content):,} chars")
                    valid_items.append({**item, "content": content})

            print(f"\n{'─'*60}")
            print(f"Final: {len(valid_items)}/{len(top)} passed validation")
            print(f"{'─'*60}")
            for item in valid_items:
                print(f"\n[{item['blog']}] {item['title']}")
                print(f"URL   : {item['url']}")
                print(f"Chars : {len(item['content'])}")
                print(f"Preview:\n{item['content'].strip()}")
                print()
    finally:
        clients.close()
