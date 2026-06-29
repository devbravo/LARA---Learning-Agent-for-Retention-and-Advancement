"""Clean text extraction via Jina AI Reader API (r.jina.ai).

Converts raw web pages to clean Markdown. Failures on individual URLs are
logged and skipped — the batch continues with whatever succeeds.
No database writes here.
"""

import logging
import re

import requests

from src.knowledge.clients import KnowledgeClients

logger = logging.getLogger(__name__)

_JINA_READER_BASE = "https://r.jina.ai/"

_MIN_SUBSTANTIVE_LENGTH = 500  # chars — below this is likely a paywall block or failed fetch

# Terms dense in cookie-consent boilerplate; absent or sparse in real articles.
_BOILERPLATE_TERMS = [
    "cookie",
    "consent",
    "privacy policy",
    "necessary cookies",
    "analytics cookies",
    "marketing cookies",
    "advertisement cookies",
    "functional cookies",
    "reject all",
    "accept all",
    "gdpr",
    "ccpa",
    "cookieyes",
    "cookiebot",
]

# If boilerplate hits / total words exceeds this, the page is junk.
# Not validated — tune up if off-topic boilerplate slips through,
# tune down if real articles about privacy/cookies get dropped.
_BOILERPLATE_DENSITY_THRESHOLD = 0.05

# Markdown heading patterns that signal the start of trailing boilerplate.
# Only matched in the last third of the document by character position.
# "further reading" is flagged but logged rather than silently stripped —
# it sometimes contains genuinely useful author-curated links.
_TRAILING_BOILERPLATE_PATTERNS = re.compile(
    r"^#{1,3}\s+"
    r"(related\s+(articles?|posts?|reading)"
    r"|you\s+(might|may)\s+also\s+like"
    r"|read\s+(next|more)"
    r"|subscribe"
    r"|about\s+the\s+author"
    r"|acknowledgements?"
    r"|references"
    r"|further\s+reading"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_FURTHER_READING_PATTERN = re.compile(
    r"^#{1,3}\s+further\s+reading\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def clean_trailing_boilerplate(text: str) -> str:
    """Remove well-known trailing boilerplate sections from extracted Markdown.

    Only looks in the last third of the document by character position —
    a heading matching one of the boilerplate patterns near the top or middle
    of an article is unlikely to actually be trailing boilerplate, and we
    don't want to truncate real content over a coincidental heading name.

    Does NOT strip inline links or citations within prose paragraphs —
    only structural ``#`` headings that open a whole trailing section.

    ``## Further Reading`` is borderline: author-curated links can be useful
    content. This function strips it but logs a warning so it can be reviewed.

    Args:
        text: Extracted Markdown text, already validated by ``is_extraction_valid``.

    Returns:
        Text with trailing boilerplate section(s) removed, or the original
        text unchanged if no boilerplate heading was found.
    """
    total_len = len(text)
    search_start = total_len * 2 // 3  # only scan last third

    for match in _TRAILING_BOILERPLATE_PATTERNS.finditer(text, search_start):
        if _FURTHER_READING_PATTERN.match(match.group()):
            logger.warning(
                "Stripping '## Further Reading' section at char %d/%d — "
                "may contain useful author-curated links; review if synthesis quality drops",
                match.start(),
                total_len,
            )
        truncated = text[: match.start()].rstrip()
        logger.debug(
            "clean_trailing_boilerplate: stripped %r at char %d/%d (%d chars removed)",
            match.group().strip(),
            match.start(),
            total_len,
            total_len - len(truncated),
        )
        return truncated

    return text


def is_extraction_valid(text: str) -> tuple[bool, str]:
    """Sanity-check extracted text before it enters the pipeline.

    Runs two deterministic formula checks — no LLM call.

    1. **Minimum length**: text shorter than ``_MIN_SUBSTANTIVE_LENGTH``
       characters is likely a paywall block or failed extraction.
    2. **Boilerplate keyword density**: if cookie/consent terms make up
       more than ``_BOILERPLATE_DENSITY_THRESHOLD`` of total word count,
       the page returned a cookie-consent modal instead of article content.

    Args:
        text: Raw extracted text to validate.

    Returns:
        ``(True, "")`` if the text looks like real article content.
        ``(False, reason)`` where ``reason`` is a human-readable explanation
        of which check failed.
    """
    if len(text) < _MIN_SUBSTANTIVE_LENGTH:
        return False, f"too short ({len(text)} chars, min {_MIN_SUBSTANTIVE_LENGTH})"

    text_lower = text.lower()
    words = text_lower.split()
    total_words = len(words)
    if total_words == 0:
        return False, "empty after tokenising"

    boilerplate_hits = sum(text_lower.count(term) for term in _BOILERPLATE_TERMS)
    density = boilerplate_hits / total_words
    if density > _BOILERPLATE_DENSITY_THRESHOLD:
        return (
            False,
            f"boilerplate density {density:.1%} > {_BOILERPLATE_DENSITY_THRESHOLD:.0%} "
            f"({boilerplate_hits} hits / {total_words} words)",
        )

    return True, ""


def extract_clean_text(url: str, clients: KnowledgeClients) -> str | None:
    """Fetch and return clean Markdown text for a single URL via Jina Reader.

    Args:
        url: The article URL to extract.
        clients: Shared client container; ``clients.jina_api_key`` is used.

    Returns:
        Clean Markdown string, or ``None`` if the request failed.
    """
    headers = {
        "Authorization": f"Bearer {clients.jina_api_key}",
        "Accept": "text/plain",
    }
    try:
        resp = requests.get(
            f"{_JINA_READER_BASE}{url}",
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Extraction failed for %s: %s", url, exc)
        return None


def extract_top_results(
    results: list[dict], clients: KnowledgeClients
) -> list[dict]:
    """Extract, validate, and clean text for each result in the list.

    For each URL: fetch → validate (``is_extraction_valid``) → strip trailing
    boilerplate (``clean_trailing_boilerplate``) → add to output.
    Skips any URL where extraction fails or validation rejects the content.

    Args:
        results: List of result dicts from ``select_top_results``, each
            containing at minimum a ``url`` key.
        clients: Shared client container.

    Returns:
        List of dicts (same structure as input) with ``content: str`` added.
        Failed or invalid extractions are omitted entirely.
    """
    extracted = []
    for item in results:
        url = item["url"]
        logger.info("Extracting %s", url)
        content = extract_clean_text(url, clients)
        if content is None:
            logger.warning("Skipping %s — extraction returned None", url)
            continue
        valid, reason = is_extraction_valid(content)
        if not valid:
            logger.warning("Skipping %s — invalid content: %s", url, reason)
            continue
        content = clean_trailing_boilerplate(content)
        extracted.append({**item, "content": content})
    return extracted
