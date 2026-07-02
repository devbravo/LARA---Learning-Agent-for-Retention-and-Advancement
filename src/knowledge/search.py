"""Topic-scoped blog search via Jina AI search API.

Reads blog sources from the ``sources`` SQLite table — no hardcoded URLs here.
All search results are returned as plain dicts; no database writes happen here.

Pipeline:
  1. search_blogs_for_topic  — concurrent Jina searches, returns title/url/snippet only
  2. select_top_results      — Voyage reranker on snippets, returns top N
  3. fetch_article_contents  — concurrent Jina Reader calls only for top N articles

Fetching full content only after reranking avoids paying Jina token cost for
articles that are discarded by the reranker (~90% of raw results).

Dev runner:
    python -m src.knowledge.search "context management"
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import zip_longest
from urllib.parse import urlparse

import requests

from src.knowledge.clients import KnowledgeClients, VOYAGE_RERANK_MODEL

logger = logging.getLogger(__name__)

_JINA_SEARCH_URL = "https://s.jina.ai/"
_JINA_READER_URL = "https://r.jina.ai/"

# All results that pass the reranker threshold are forwarded to synthesis.
TOP_N_FOR_SYNTHESIS = 8

# Minimum reranker relevance score to keep a result.
_RERANK_THRESHOLD = 0.45

# Patterns that indicate a URL is a CMS artifact, tag page, or raw asset.
_EXCLUDED_URL_PATTERNS = [
    "/attachment/",
    "/wp-content/",
    "/tag/",
    "/category/",
    "/author/",
    ".png",
    ".jpg",
    ".pdf",
]


def _search_one_blog(
    blog_name: str,
    site_url: str,
    topic: str,
    headers: dict,
) -> list[dict]:
    """Run a single site-scoped Jina search, returning metadata only (no content).

    Full article content is intentionally excluded — it is fetched later via
    the Jina Reader only for articles that pass reranking, avoiding token cost
    for the ~90% of results that are discarded.

    Args:
        blog_name: Human-readable source name (stored in each result as ``blog``).
        site_url: The blog's root URL; only the netloc is used for the query.
        topic: Search keyword or phrase.
        headers: HTTP headers including the Jina Bearer token.

    Returns:
        List of result dicts with ``title``, ``url``, ``snippet``, ``blog``.
        May be empty on network error or no results.
    """
    domain = urlparse(site_url).netloc
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
        return []

    raw_results = data.get("data", [])
    clean_results = [
        item for item in raw_results
        if not any(bad in item.get("url", "").lower() for bad in _EXCLUDED_URL_PATTERNS)
    ]

    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
            "blog": blog_name,
        }
        for item in clean_results
    ]


def _fetch_one_article(url: str, headers: dict) -> str:
    """Fetch full article content from Jina Reader for a single URL.

    Args:
        url: The article URL to fetch.
        headers: HTTP headers including the Jina Bearer token.

    Returns:
        Article text, or empty string on failure.
    """
    try:
        resp = requests.get(
            f"{_JINA_READER_URL}{url}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Reader fetch failed for %s: %s", url, exc)
        return ""


def search_blogs_for_topic(topic: str, clients: KnowledgeClients) -> list[dict]:
    """Search each configured blog for the given topic via Jina AI search.

    All blogs are searched concurrently (one thread per source). Returns
    metadata only (title, url, snippet) — full content is not fetched here.
    Call ``fetch_article_contents`` on the reranked subset to get content.

    Args:
        topic: The keyword or phrase to search for (e.g. ``"context management"``).
        clients: Shared client container; ``clients.jina_api_key`` and
            ``clients.db`` are used here.

    Returns:
        List of result dicts with ``title``, ``url``, ``snippet``, ``blog``.
        Order: round-robin across blogs by rank (rank-1 from each, then
        rank-2 from each, etc.).
    """
    sources = clients.db.execute(
        "SELECT name, site_url FROM sources ORDER BY id"
    ).fetchall()

    if not sources:
        return []

    headers = {
        "Authorization": f"Bearer {clients.jina_api_key}",
        "Accept": "application/json",
    }

    per_blog: list[list[dict]] = [[] for _ in sources]

    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        futures = {
            executor.submit(
                _search_one_blog, source["name"], source["site_url"], topic, headers
            ): i
            for i, source in enumerate(sources)
        }
        for future in as_completed(futures):
            idx = futures[future]
            per_blog[idx] = future.result()

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
    """Rerank raw search results on snippets and return the top relevant ones.

    Uses Voyage's cross-encoder reranker (``rerank-2``). Scores are computed
    on ``snippet`` (short description) rather than full content — the reranker
    has enough signal from 100-char descriptions to judge relevance, and using
    snippets avoids sending large payloads to the Voyage API.

    Args:
        query: The original search topic string.
        raw_results: Output of ``search_blogs_for_topic`` (no content field).
        clients: Shared client container; ``clients.voyage`` is used.

    Returns:
        Results with ``relevance_score`` added, filtered to >= ``_RERANK_THRESHOLD``
        and limited to ``TOP_N_FOR_SYNTHESIS``, sorted by score descending.
        No ``content`` field yet — call ``fetch_article_contents`` next.
    """
    if not raw_results:
        return []

    pairs: list[tuple[dict, str]] = []
    for r in raw_results:
        text = r.get("snippet") or r.get("title", "")
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

    return [
        {**originals[item.index], "relevance_score": item.relevance_score}
        for item in rerank_response.results
        if item.relevance_score >= _RERANK_THRESHOLD
    ]


def fetch_article_contents(
    top_results: list[dict],
    jina_api_key: str,
) -> list[dict]:
    """Fetch full article content for each result via Jina Reader, in parallel.

    Called after ``select_top_results`` so content is only fetched for articles
    that passed reranking — typically 5–8 articles instead of 40–80 raw results.

    Args:
        top_results: Output of ``select_top_results``.
        jina_api_key: Jina API key for Bearer auth.

    Returns:
        Same list with ``content`` field added to each dict. Articles whose
        fetch failed have ``content`` set to empty string and are expected to
        be filtered out by ``is_extraction_valid`` downstream.
    """
    if not top_results:
        return []

    headers = {
        "Authorization": f"Bearer {jina_api_key}",
        "Accept": "text/plain",
        "X-Return-Format": "text",
    }

    contents: list[str] = [""] * len(top_results)

    with ThreadPoolExecutor(max_workers=len(top_results)) as executor:
        futures = {
            executor.submit(_fetch_one_article, result["url"], headers): i
            for i, result in enumerate(top_results)
        }
        for future in as_completed(futures):
            idx = futures[future]
            contents[idx] = future.result()

    return [{**result, "content": content} for result, content in zip(top_results, contents)]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "context management"
    print(f"\nTopic: {topic!r}\n")

    clients = KnowledgeClients()
    try:
        all_results = search_blogs_for_topic(topic, clients)

        print(f"{'─'*60}")
        print(f"All search results ({len(all_results)} total, metadata only)")
        print(f"{'─'*60}")
        for i, r in enumerate(all_results, 1):
            snippet = r['snippet'][:100] + ("…" if len(r['snippet']) > 100 else "")
            print(f"[{i:2}] [{r['blog']}] {r['title']}")
            print(f"     {r['url']}")
            print(f"     {snippet}")

        top = select_top_results(topic, all_results, clients)

        print(f"\n{'─'*60}")
        print(f"Reranked: {len(top)} passed threshold {_RERANK_THRESHOLD} (cap {TOP_N_FOR_SYNTHESIS})")
        print(f"{'─'*60}")
        for i, r in enumerate(top, 1):
            print(f"[{i}] {r['relevance_score']:.4f}  [{r['blog']}] {r['title']}")
            print(f"    {r['url']}")

        if top:
            print(f"\nFetching content for {len(top)} article(s) via Jina Reader…")
            top_with_content = fetch_article_contents(top, clients.jina_api_key)

            from src.knowledge.extract import is_extraction_valid
            print(f"\n{'─'*60}")
            print(f"Content validation")
            print(f"{'─'*60}")
            for item in top_with_content:
                ok, reason = is_extraction_valid(item.get("content", ""))
                status = f"PASS — {len(item['content']):,} chars" if ok else f"SKIP — {reason}"
                print(f"  [{item['blog']}] {item['title']}")
                print(f"  {status}")
    finally:
        clients.close()
