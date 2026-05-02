"""Discuss-mode service — readiness assessment and context retrieval.

All business logic for the /discuss flow lives here. MCP tools in
src/api/routes/mcp.py are thin wrappers that delegate to these functions.
"""

import json
import logging
from typing import Any

from src.agent import messages
from src.integrations import telegram_client as _telegram
from src.repositories import session_repository, topic_repository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_weak_area_keys(raw: str | None) -> list[str]:
    """Extract weak-area identifiers (dict keys) from a stored teacher_weak_areas value.

    Handles three shapes defensively:
    - ``None`` or empty string → empty list
    - Valid JSON object → list of top-level keys
    - Non-JSON plain string → single-element list containing the raw string

    Args:
        raw: Raw ``teacher_weak_areas`` value as stored in the DB.

    Returns:
        List of weak-area identifier strings.
    """
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return [k for k, v in parsed.items() if v]
        return []
    except (json.JSONDecodeError, TypeError, ValueError):
        return [raw.strip()]


def _find_repeated_weak_areas(sessions: list[dict]) -> list[str]:
    """Return weak-area keys that appear in two or more sessions.

    Args:
        sessions: List of session dicts, each with a ``teacher_weak_areas`` field.

    Returns:
        Sorted list of key strings present in at least 2 sessions.
    """
    counts: dict[str, int] = {}
    for session in sessions:
        for key in _parse_weak_area_keys(session.get("teacher_weak_areas")):
            counts[key] = counts.get(key, 0) + 1
    return sorted(k for k, n in counts.items() if n >= 2)


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def get_discuss_context(topic_name: str) -> dict[str, Any]:
    """Fetch all context needed to run a discuss session for a topic.

    Args:
        topic_name: Topic display name (case-insensitive lookup).

    Returns:
        Dict with keys ``topic_id``, ``topic_name``, ``topic_type``,
        ``weak_areas``, ``discuss_history``, ``mock_history_exists``, and
        ``mock_history``.  Returns ``{"error": str}`` when the topic is not
        found or a DB error occurs.
    """
    try:
        topic_id = topic_repository.get_topic_id_by_name(topic_name)
    except ValueError as exc:
        return {"error": str(exc)}

    if topic_id is None:
        return {"error": f"Topic not found: {topic_name}"}

    try:
        context = topic_repository.get_topic_context(topic_id)
        discuss_history = session_repository.get_discuss_sessions(topic_id, limit=5)
        mock_history = session_repository.get_mock_sessions(topic_id, limit=5)
        mock_history_exists = session_repository.has_mock_history(topic_id)
    except Exception as exc:
        logger.error("get_discuss_context DB error for topic_id=%d: %s", topic_id, exc)
        return {"error": f"Database error: {exc}"}

    return {
        "topic_id": topic_id,
        "topic_name": context["name"],
        "topic_type": context.get("topic_type"),
        "weak_areas": context.get("weak_areas"),
        "discuss_history": discuss_history,
        "mock_history_exists": mock_history_exists,
        "mock_history": mock_history,
    }


def assess_discuss_readiness(
    topic_name: str,
    teacher_quality: int,
    teacher_weak_areas: str,
) -> dict[str, Any]:
    """Log a discuss session and evaluate whether the topic is ready for mock.

    Inserts the session, checks for repeated weak areas across the last 10
    discuss sessions, applies the readiness rubric, sends a Telegram
    notification, and — when the topic needs more study — moves it back to
    in-progress.

    Readiness rubric (evaluated after inserting the session):
    - Any repeated weak area (2+ sessions) → ``go_back_to_study``
    - No repeats **and** ``teacher_quality >= 4`` → ``ready``
    - Otherwise → ``not_ready``

    Args:
        topic_name: Topic display name (case-insensitive lookup).
        teacher_quality: Quality score for this discuss session (integer).
        teacher_weak_areas: JSON string representing weak areas observed
            (dict keys = dimension names, values = observations).

    Returns:
        Dict with keys ``recommendation`` (``"ready"`` | ``"not_ready"`` |
        ``"go_back_to_study"``), ``reason`` (human-readable explanation), and
        ``repeated_weak_areas`` (list of repeated dimension names).
        Returns ``{"error": str}`` when the topic is not found or a critical
        error occurs.
    """
    try:
        topic_id = topic_repository.get_topic_id_by_name(topic_name)
    except ValueError as exc:
        return {"error": str(exc)}

    if topic_id is None:
        return {"error": f"Topic not found: {topic_name}"}

    # Insert the current session first so it is included in the repetition check.
    try:
        session_repository.insert_discuss_session(topic_id, teacher_quality, teacher_weak_areas)
    except Exception as exc:
        logger.error("assess_discuss_readiness insert failed for topic_id=%d: %s", topic_id, exc)
        return {"error": f"Failed to insert session: {exc}"}

    # Fetch last 10 discuss sessions (includes the one just inserted).
    try:
        sessions = session_repository.get_discuss_sessions(topic_id, limit=10)
        is_reentry = session_repository.has_mock_history(topic_id)
    except Exception as exc:
        logger.error("assess_discuss_readiness query failed for topic_id=%d: %s", topic_id, exc)
        return {"error": f"Database error: {exc}"}

    repeated_weak_areas = _find_repeated_weak_areas(sessions)

    # --- Readiness rubric ---
    if repeated_weak_areas:
        recommendation = "go_back_to_study"
        reason = (
            f"Recurring gaps in {len(repeated_weak_areas)} area(s) — "
            f"needs more study before discussing again: "
            f"{', '.join(repeated_weak_areas)}."
        )
    elif teacher_quality >= 4:
        recommendation = "ready"
        reason = (
            "No repeated gaps and quality is strong — ready for a mock session."
            if is_reentry
            else "First discuss session with strong quality — ready for a mock session."
        )
    else:
        recommendation = "not_ready"
        current_areas = _parse_weak_area_keys(teacher_weak_areas)
        reason = (
            f"Quality score {teacher_quality} is below the readiness threshold. "
            f"Focus areas: {', '.join(current_areas)}."
            if current_areas
            else f"Quality score {teacher_quality} is below the readiness threshold."
        )

    # --- Side effects ---
    try:
        if recommendation == "ready":
            text, buttons = messages.discuss_ready(topic_name)
            _telegram.send_buttons(text, buttons)
        elif recommendation == "not_ready":
            current_areas = _parse_weak_area_keys(teacher_weak_areas)
            _telegram.send_message(messages.discuss_not_ready(topic_name, current_areas))
        else:  # go_back_to_study
            topic_repository.set_topic_back_to_in_progress(topic_id)
            _telegram.send_message(messages.discuss_go_back_to_study(topic_name, repeated_weak_areas))
    except Exception as exc:
        # Telegram or DB failure on side effects — log but still return the result.
        logger.warning(
            "assess_discuss_readiness side-effect failed for %s (%s): %s",
            topic_name, recommendation, exc,
        )

    return {
        "recommendation": recommendation,
        "reason": reason,
        "repeated_weak_areas": repeated_weak_areas,
    }
