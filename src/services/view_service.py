"""View service — read-only study snapshot for the /view command."""

from datetime import date

from src.infrastructure.db import get_connection


def get_study_snapshot(today: date | None = None) -> dict:
    """Return overdue, due-today, and in-progress topics.

    Args:
        today: Reference date for due calculations. Defaults to date.today().

    Returns:
        dict with keys:
          - ``overdue``: list of dicts {name, days_overdue, weak_areas}, most overdue first
          - ``due_today``: list of dicts {name, weak_areas}
          - ``in_progress``: list of dicts {name, weak_areas}
    """
    if today is None:
        today = date.today()

    today_str = today.isoformat()

    with get_connection() as conn:
        overdue_rows = conn.execute(
            """SELECT name, next_review, weak_areas FROM topics
               WHERE status = 'active' AND next_review < ?
               ORDER BY next_review ASC""",
            (today_str,),
        ).fetchall()

        due_today_rows = conn.execute(
            """SELECT name, weak_areas FROM topics
               WHERE status = 'active' AND next_review = ?
               ORDER BY tier ASC, easiness_factor ASC""",
            (today_str,),
        ).fetchall()

        in_progress_rows = conn.execute(
            """SELECT name, weak_areas FROM topics
               WHERE status = 'in_progress'
               ORDER BY tier ASC, name ASC""",
        ).fetchall()

    overdue = []
    for row in overdue_rows:
        next_review_date = date.fromisoformat(row["next_review"])
        days_overdue = (today - next_review_date).days
        overdue.append({
            "name": row["name"],
            "days_overdue": days_overdue,
            "weak_areas": row["weak_areas"] or None,
        })
    overdue.sort(key=lambda t: t["days_overdue"], reverse=True)

    due_today = [
        {"name": row["name"], "weak_areas": row["weak_areas"] or None}
        for row in due_today_rows
    ]

    in_progress = [
        {"name": row["name"], "weak_areas": row["weak_areas"] or None}
        for row in in_progress_rows
    ]

    return {
        "overdue": overdue,
        "due_today": due_today,
        "in_progress": in_progress,
    }
