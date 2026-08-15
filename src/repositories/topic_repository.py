"""Topic repository SQL helpers.

This module contains topic-specific database reads and writes used by service
and node layers.

Catalog-only lookups (``get_topic_name_by_id``, ``get_topic_id_by_name``,
``get_topic_type_by_id``, ``get_default_duration_by_name``) query
``topic_catalog`` directly and take no ``engineer_id`` — those fields are
shared, training-team-owned data, not per-engineer state.

Every other function is engineer-scoped and takes ``engineer_id: int`` as its
first parameter. They query ``engineer_topic_progress`` (optionally joined to
``topic_catalog``). Per-engineer progress rows are created lazily: an engineer
who has never interacted with a topic has no row in ``engineer_topic_progress``,
which is treated as ``status='inactive'`` with SM-2 defaults.
"""

from typing import Any

from src.infrastructure.db import get_connection
from src.repositories import session_repository


def get_topic_name_by_id(topic_id: int) -> str | None:
    """Return catalog topic name for a given id.

    Args:
        topic_id: Topic catalog primary key.

    Returns:
        Topic name, or ``None`` when the row does not exist.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM topic_catalog WHERE id = ?", (topic_id,)).fetchone()
    return row["name"] if row else None


def get_topic_id_by_name(topic_name: str) -> int | None:
    """Return catalog topic id for a case-insensitive topic name.

    Tries an exact match first; if that fails, falls back to a substring
    (LIKE) search and returns the id only when exactly one topic matches.
    Raises ``ValueError`` with candidate names when multiple topics match
    the substring, so the caller can surface them to the user.

    Args:
        topic_name: Topic display name (exact or partial).

    Returns:
        Topic id, or ``None`` when no match exists.

    Raises:
        ValueError: If the substring matches more than one topic.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM topic_catalog WHERE name = ? COLLATE NOCASE",
            (topic_name,),
        ).fetchone()
        if row:
            return row["id"]
        rows = conn.execute(
            "SELECT id, name FROM topic_catalog WHERE name LIKE ? COLLATE NOCASE",
            (f"%{topic_name}%",),
        ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        candidates = ", ".join(f'"{r["name"]}"' for r in rows)
        raise ValueError(
            f"Ambiguous topic name '{topic_name}'. Did you mean: {candidates}?"
        )
    return None


def get_topic_type_by_id(topic_id: int) -> str | None:
    """Return catalog topic_type for a given topic id.

    Args:
        topic_id: Topic catalog primary key.

    Returns:
        Topic type string, or ``None`` when the row does not exist.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT topic_type FROM topic_catalog WHERE id = ?", (topic_id,)
        ).fetchone()
    return row["topic_type"] if row else None


def get_default_duration_by_name(topic_name: str) -> int:
    """Return default_duration_minutes for a catalog topic name, falling back to 30.

    Args:
        topic_name: Topic display name (case-insensitive lookup).

    Returns:
        Default session duration in minutes, or ``30`` when the topic is
        not found or the column has not been seeded yet.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT default_duration_minutes FROM topic_catalog WHERE name = ? COLLATE NOCASE",
            (topic_name,),
        ).fetchone()
    if row is None or row["default_duration_minutes"] is None:
        return 30
    return row["default_duration_minutes"]


def graduate_topic_to_active(engineer_id: int, topic_id: int) -> bool:
    """Set an engineer's progress on a topic to active and reset SM-2 fields.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.

    Returns:
        ``True`` when a progress row was updated, else ``False`` (e.g. the
        engineer has no progress row yet for this topic).
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE engineer_topic_progress
               SET status = 'active',
                   repetitions = 0,
                   easiness_factor = 2.5,
                   next_review = date('now', '+1 day'),
                   updated_at = CURRENT_TIMESTAMP
               WHERE engineer_id = ? AND topic_id = ?""",
            (engineer_id, topic_id),
        )
    return cursor.rowcount > 0


def activate_topic_from_discuss(engineer_id: int, topic_id: int) -> bool:
    """Set an engineer's progress on a topic to active after discuss-readiness.

    Like ``graduate_topic_to_active`` but sets ``next_review`` to *today*
    so that the first SM-2 mock session is scheduled immediately rather than
    deferred to tomorrow.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.

    Returns:
        ``True`` when a progress row was updated, else ``False``.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE engineer_topic_progress
               SET status = 'active',
                   repetitions = 0,
                   easiness_factor = 2.5,
                   next_review = date('now'),
                   updated_at = CURRENT_TIMESTAMP
               WHERE engineer_id = ? AND topic_id = ?""",
            (engineer_id, topic_id),
        )
    return cursor.rowcount > 0


def get_topic_status_by_id(engineer_id: int, topic_id: int) -> str | None:
    """Return an engineer's current status for a topic.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.

    Returns:
        Status string (e.g. ``'in_progress'``, ``'discussing'``, ``'active'``).
        ``'inactive'`` when the topic exists but the engineer has no progress
        row yet (lazy row semantics). ``None`` when the topic itself does not
        exist in the catalog.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM engineer_topic_progress WHERE engineer_id = ? AND topic_id = ?",
            (engineer_id, topic_id),
        ).fetchone()
        if row:
            return row["status"]
        catalog_row = conn.execute(
            "SELECT id FROM topic_catalog WHERE id = ?", (topic_id,)
        ).fetchone()
    return "inactive" if catalog_row else None


def get_in_progress_topics(engineer_id: int) -> list[dict[str, int | str]]:
    """Return an engineer's in-progress topics ordered by tier and name.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        List of dicts containing ``id`` and ``name``.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.id, tc.name
               FROM topic_catalog tc
               JOIN engineer_topic_progress etp ON etp.topic_id = tc.id AND etp.engineer_id = ?
               WHERE etp.status = 'in_progress'
               ORDER BY tc.tier ASC, tc.name ASC""",
            (engineer_id,),
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]


def get_in_progress_topic_names(engineer_id: int) -> list[str]:
    """Return an engineer's in-progress topic names ordered by tier and name.

    Args:
        engineer_id: Engineer primary key.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.name
               FROM topic_catalog tc
               JOIN engineer_topic_progress etp ON etp.topic_id = tc.id AND etp.engineer_id = ?
               WHERE etp.status = 'in_progress'
               ORDER BY tc.tier ASC, tc.name ASC""",
            (engineer_id,),
        ).fetchall()
    return [row["name"] for row in rows]


def get_topic_weak_areas_by_name(engineer_id: int, topic_name: str) -> str | None:
    """Return an engineer's operational weak-areas text for a topic name.

    Args:
        engineer_id: Engineer primary key.
        topic_name: Topic display name (case-insensitive lookup).

    Returns:
        Weak-areas string, or ``None`` when missing/empty.
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT etp.weak_areas
               FROM topic_catalog tc
               JOIN engineer_topic_progress etp ON etp.topic_id = tc.id AND etp.engineer_id = ?
               WHERE tc.name = ? COLLATE NOCASE""",
            (engineer_id, topic_name),
        ).fetchone()
    return row["weak_areas"] if row and row["weak_areas"] else None


def update_topic_weak_areas(engineer_id: int, topic_id: int, weak_areas: str | None) -> None:
    """Set or clear an engineer's operational weak areas for a topic.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.
        weak_areas: Weak-areas text or ``None`` to clear the field.
    """
    with get_connection() as conn:
        conn.execute(
            """UPDATE engineer_topic_progress
               SET weak_areas = ?, updated_at = CURRENT_TIMESTAMP
               WHERE engineer_id = ? AND topic_id = ?""",
            (weak_areas, engineer_id, topic_id),
        )


def get_inactive_topics_tier1_or2(engineer_id: int) -> list[dict[str, Any]]:
    """Return an engineer's inactive tier-1/2 topics ordered by tier and name.

    A topic with no progress row yet is treated as inactive (lazy row
    semantics), so this LEFT JOINs against ``engineer_topic_progress``.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        List of dicts with keys ``id``, ``name``, and ``tier``.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.id, tc.name, tc.tier
               FROM topic_catalog tc
               LEFT JOIN engineer_topic_progress etp
                   ON etp.topic_id = tc.id AND etp.engineer_id = ?
               WHERE tc.tier IN (1, 2) AND COALESCE(etp.status, 'inactive') = 'inactive'
               ORDER BY tc.tier ASC, tc.name ASC""",
            (engineer_id,),
        ).fetchall()
    return [{"id": row["id"], "name": row["name"], "tier": row["tier"]} for row in rows]


def fetch_overdue_topics(engineer_id: int, today_str: str) -> list[dict[str, Any]]:
    """Fetch an engineer's active topics whose next review date is before today.

    Args:
        engineer_id: Engineer primary key.
        today_str: ISO date string (``YYYY-MM-DD``) for the cutoff.

    Returns:
        List of dicts with ``name``, ``next_review``, and ``weak_areas``,
        ordered most-overdue first.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.name, etp.next_review, etp.weak_areas
               FROM engineer_topic_progress etp
               JOIN topic_catalog tc ON tc.id = etp.topic_id
               WHERE etp.engineer_id = ? AND etp.status = 'active' AND etp.next_review < ?
               ORDER BY etp.next_review ASC""",
            (engineer_id, today_str),
        ).fetchall()
    return [{"name": r["name"], "next_review": r["next_review"], "weak_areas": r["weak_areas"]} for r in rows]


def fetch_due_today_topics(engineer_id: int, today_str: str) -> list[dict[str, Any]]:
    """Fetch an engineer's active topics due for review today.

    Args:
        engineer_id: Engineer primary key.
        today_str: ISO date string (``YYYY-MM-DD``) for today.

    Returns:
        List of dicts with ``name`` and ``weak_areas``, ordered by
        tier then easiness factor.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.name, etp.weak_areas
               FROM engineer_topic_progress etp
               JOIN topic_catalog tc ON tc.id = etp.topic_id
               WHERE etp.engineer_id = ? AND etp.status = 'active' AND etp.next_review = ?
               ORDER BY tc.tier ASC, etp.easiness_factor ASC""",
            (engineer_id, today_str),
        ).fetchall()
    return [{"name": r["name"], "weak_areas": r["weak_areas"]} for r in rows]


def fetch_in_progress_topics_with_weak_areas(engineer_id: int) -> list[dict[str, Any]]:
    """Fetch an engineer's in-progress and discussing topics with weak areas.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        List of dicts with ``name`` and ``weak_areas``, ordered by tier
        then name.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.name, etp.weak_areas
               FROM engineer_topic_progress etp
               JOIN topic_catalog tc ON tc.id = etp.topic_id
               WHERE etp.engineer_id = ? AND etp.status IN ('in_progress', 'discussing')
               ORDER BY tc.tier ASC, tc.name ASC""",
            (engineer_id,),
        ).fetchall()
    return [{"name": r["name"], "weak_areas": r["weak_areas"]} for r in rows]


def get_active_unlogged_topics_today(engineer_id: int) -> list[dict]:
    """Return an engineer's active topics not yet logged today.

    Ordered by tier ASC, easiness_factor ASC.

    The "not yet logged" check itself is NOT engineer-scoped — it delegates
    to ``session_repository.get_logged_topic_names_for_today()``, which
    returns names logged by any engineer (see that function's docstring).
    A topic could be incorrectly excluded here if a different engineer
    logged a same-named topic today. Fixing this requires
    ``insert_session``/``upsert_today_session`` to write ``engineer_id``
    (multi-user data layer Ticket 4); the ``engineer_id`` param on *this*
    function only scopes the active-topics half of the query.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        List of dicts with keys ``id`` and ``name``.
    """
    logged_names = session_repository.get_logged_topic_names_for_today()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.id, tc.name
               FROM engineer_topic_progress etp
               JOIN topic_catalog tc ON tc.id = etp.topic_id
               WHERE etp.engineer_id = ? AND etp.status = 'active'
               ORDER BY tc.tier ASC, etp.easiness_factor ASC""",
            (engineer_id,),
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows if row["name"] not in logged_names]


def get_topic_context(engineer_id: int, topic_id: int) -> dict[str, Any]:
    """Fetch an engineer's SM-2 state and last session signal for a topic.

    LEFT JOINs progress (lazy-row defaults apply when the engineer has never
    interacted with the topic) and the engineer's most recent session for it.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.

    Returns:
        Dict with topic SM-2 fields and last-session data (session fields
        are ``None`` when no session exists yet). Empty dict when the topic
        does not exist in the catalog.
    """
    with get_connection() as conn:
        row = conn.execute(
            """SELECT
                   tc.id, tc.name, tc.topic_type,
                   COALESCE(etp.easiness_factor, 2.5) AS easiness_factor,
                   COALESCE(etp.interval_days, 1) AS interval_days,
                   COALESCE(etp.repetitions, 0) AS repetitions,
                   etp.next_review, etp.weak_areas,
                   s.student_quality, s.studied_at, s.student_weak_areas
               FROM topic_catalog tc
               LEFT JOIN engineer_topic_progress etp
                   ON etp.topic_id = tc.id AND etp.engineer_id = ?
               LEFT JOIN sessions s
                   ON s.topic_id = tc.id AND s.engineer_id = ?
               WHERE tc.id = ?
               ORDER BY s.studied_at DESC
               LIMIT 1""",
            (engineer_id, engineer_id, topic_id),
        ).fetchone()
    if row is None:
        return {}
    return {
        "id": row["id"],
        "name": row["name"],
        "topic_type": row["topic_type"],
        "easiness_factor": row["easiness_factor"],
        "interval_days": row["interval_days"],
        "repetitions": row["repetitions"],
        "next_review": row["next_review"],
        "weak_areas": row["weak_areas"],
        "student_quality": row["student_quality"],
        "studied_at": row["studied_at"],
        "student_weak_areas": row["student_weak_areas"],
    }


def set_topic_in_progress(engineer_id: int, topic_name: str) -> bool:
    """Set an engineer's status for a topic to in_progress.

    Creates the engineer's progress row lazily if it doesn't exist yet
    (a missing row is implicitly inactive); otherwise only transitions an
    existing 'inactive' row.

    Args:
        engineer_id: Engineer primary key.
        topic_name: Topic display name (exact match).

    Returns:
        ``True`` when the row was created or updated, else ``False`` (unknown
        topic name, or the engineer's existing status isn't 'inactive').
    """
    with get_connection() as conn:
        catalog_row = conn.execute(
            "SELECT id FROM topic_catalog WHERE name = ?", (topic_name,)
        ).fetchone()
        if catalog_row is None:
            return False
        topic_id = catalog_row["id"]

        cursor = conn.execute(
            """INSERT INTO engineer_topic_progress (engineer_id, topic_id, status, updated_at)
               VALUES (?, ?, 'in_progress', CURRENT_TIMESTAMP)
               ON CONFLICT(engineer_id, topic_id) DO UPDATE SET
                   status = 'in_progress', updated_at = CURRENT_TIMESTAMP
               WHERE engineer_topic_progress.status = 'inactive'""",
            (engineer_id, topic_id),
        )
    return cursor.rowcount > 0


def get_discussing_topics(engineer_id: int) -> list[dict[str, Any]]:
    """Return all of an engineer's topics with status 'discussing'.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        List of dicts with keys ``id`` and ``name``, ordered by tier and name.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.id, tc.name
               FROM topic_catalog tc
               JOIN engineer_topic_progress etp ON etp.topic_id = tc.id AND etp.engineer_id = ?
               WHERE etp.status = 'discussing'
               ORDER BY tc.tier ASC, tc.name ASC""",
            (engineer_id,),
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]


def set_topic_discussing(engineer_id: int, topic_id: int) -> None:
    """Set an engineer's status for a topic to 'discussing'.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.
    """
    with get_connection() as conn:
        conn.execute(
            """UPDATE engineer_topic_progress
               SET status = 'discussing', updated_at = CURRENT_TIMESTAMP
               WHERE engineer_id = ? AND topic_id = ?""",
            (engineer_id, topic_id),
        )


def set_topic_back_to_in_progress(engineer_id: int, topic_id: int) -> bool:
    """Return an engineer's topic from 'discussing' back to 'in_progress'.

    Only acts when the current status is 'discussing', preventing accidental
    overwrites of active or in_progress topics.

    Args:
        engineer_id: Engineer primary key.
        topic_id: Topic catalog primary key.

    Returns:
        ``True`` when a row was updated, ``False`` when no discussing
        progress row with that id exists for this engineer.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE engineer_topic_progress
               SET status = 'in_progress', updated_at = CURRENT_TIMESTAMP
               WHERE engineer_id = ? AND topic_id = ? AND status = 'discussing'""",
            (engineer_id, topic_id),
        )
    return cursor.rowcount > 0


def get_in_progress_and_active_topics(engineer_id: int) -> list[dict[str, Any]]:
    """Return an engineer's topics eligible as discuss targets.

    Eligible statuses are 'in_progress' or 'active'.

    Args:
        engineer_id: Engineer primary key.

    Returns:
        List of dicts with keys ``id`` and ``name``, ordered by tier then name.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT tc.id, tc.name
               FROM topic_catalog tc
               JOIN engineer_topic_progress etp ON etp.topic_id = tc.id AND etp.engineer_id = ?
               WHERE etp.status IN ('in_progress', 'active')
               ORDER BY tc.tier ASC, tc.name ASC""",
            (engineer_id,),
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]
