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

import requests

from src.knowledge.clients import KnowledgeClients, VOYAGE_RERANK_MODEL

logger = logging.getLogger(__name__)

_JINA_SEARCH_URL = "https://s.jina.ai/"
_RESULTS_PER_BLOG = 5

# All results that pass the reranker threshold are forwarded to synthesis.
TOP_N_FOR_SYNTHESIS = 8

# Minimum reranker relevance score to keep a result.
# Not validated — tune up if off-topic results slip through.
_RERANK_THRESHOLD = 0.45

# Patterns that indicate a URL is a CMS artifact, tag page, or raw asset, not an article.
_EXCLUDED_URL_PATTERNS = [
    "/attachment/",
    "/wp-content/",
    "/tag/",
    "/category/",
    "/author/",
    ".png",
    ".jpg",
    ".pdf"
]


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

        raw_results = data.get("data", [])[:_RESULTS_PER_BLOG]

        # Filter out junk URLs before saving the result
        clean_results = [
            item for item in raw_results
            if not any(bad in item.get("url", "").lower() for bad in _EXCLUDED_URL_PATTERNS)
        ][:_RESULTS_PER_BLOG]

        per_blog.append([
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
                "content": item.get("content", ""),
                "blog": blog_name,
            }
            for item in clean_results
        ])

    merged = [
        item
        for round_ in zip_longest(*per_blog)
        for item in round_
        if item is not None
    ]
    return merged


def select_top_results(
    query: str,
    raw_results: list[dict],
    clients: KnowledgeClients,
) -> list[dict]:
    """Rerank raw search results and return the top relevant ones.

    Uses Voyage's cross-encoder reranker (``rerank-2``) which jointly scores
    the query against each document — more accurate than bi-encoder cosine
    similarity because it sees both texts together rather than independently.

    Documents are built from each result's ``snippet`` if available, falling
    back to ``content`` for already-extracted results.

    Args:
        query: The original search topic string.
        raw_results: Output of ``search_blogs_for_topic``.
        clients: Shared client container; ``clients.voyage`` is used.

    Returns:
        Results with ``relevance_score`` added, filtered to >= ``_RERANK_THRESHOLD``
        and limited to ``TOP_N_FOR_SYNTHESIS``, sorted by score descending.
        May be empty if nothing clears the threshold.
    """
    if not raw_results:
        return []

    # Build (original_result, document_text) pairs.
    # Snippet is preferred; title is the fallback for results where Jina
    # returned no description. Results with no text at all are skipped —
    # the Voyage reranker rejects empty strings.
    pairs: list[tuple[dict, str]] = []
    for r in raw_results:
        text = r.get("snippet") or r.get("content") or r.get("title", "")
        if text:
            pairs.append((r, text))

    if not pairs:
        return []

    originals, documents = zip(*pairs)

    rerank_response = clients.voyage.rerank(
        query=query,
        documents=list(documents),
        model=VOYAGE_RERANK_MODEL,
        top_k=TOP_N_FOR_SYNTHESIS,
    )

    results = [
        {**originals[item.index], "relevance_score": item.relevance_score}
        for item in rerank_response.results
        if item.relevance_score >= _RERANK_THRESHOLD
    ]
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "context management"
    print(f"\nTopic: {topic!r}\n")

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

        top = select_top_results(topic, all_results, clients)

        print(f"\n{'─'*60}")
        print(f"Reranked: {len(top)} passed threshold {_RERANK_THRESHOLD} (cap {TOP_N_FOR_SYNTHESIS})")
        print(f"{'─'*60}")
        for i, r in enumerate(top, 1):
            print(f"[{i}] {r['relevance_score']:.4f}  [{r['blog']}] {r['title']}")
            print(f"    {r['url']}")

        if not top:
            print("No results passed the threshold — nothing to extract.")
        else:
            from src.knowledge.extract import is_extraction_valid

            print(f"\n{'─'*60}")
            print(f"Extraction + validation ({len(top)} candidates)")
            print(f"{'─'*60}")
            valid_items = []
            for item in top:
                print(f"\n  [{item['blog']}] {item['title']}")
                print(f"  {item['url']}")
                content = item["content"]
                ok, reason = is_extraction_valid(content)
                if not ok:
                    print(f"  SKIP — invalid: {reason}")
                    print(f"  (first 200 chars: {content[:200]!r})")
                else:
                    print(f"  PASS — {len(content):,} chars")
                    valid_items.append(item)

            print(f"\n{'─'*60}")
            print(f"Final: {len(valid_items)}/{len(top)} passed validation")
            print(f"{'─'*60}")
            for item in valid_items:
                print(f"\n[{item['blog']}] {item['title']}")
                print(f"URL   : {item['url']}")
                print(f"Score : {item['relevance_score']:.4f}")
                print(f"Chars : {len(item['content'])}")
    finally:
        clients.close()
