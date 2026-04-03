import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parents[2] / "db" / "learning.db"


def calculate_next_review(
    quality: int,
    easiness_factor: float,
    interval_days: int,
    repetitions: int,
) -> tuple[float, int, int]:
    """
    Returns (new_easiness_factor, new_interval_days, new_repetitions).

    SM-2 rules:
    - quality < 3:  reset interval to 1, reset repetitions to 0
    - quality >= 3:
        - repetitions == 0: interval = 1
        - repetitions == 1: interval = 6
        - repetitions > 1:  interval = round(interval * easiness_factor)
    - EF update: ef + (0.1 - (5-q) * (0.08 + (5-q) * 0.02))
    - EF minimum: 1.3
    - Increment repetitions by 1 if quality >= 3
    """
    new_ef = easiness_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ef = max(1.3, new_ef)

    if quality < 3:
        return new_ef, 1, 0

    if repetitions == 0:
        new_interval = 1
    elif repetitions == 1:
        new_interval = 6
    else:
        new_interval = round(interval_days * easiness_factor)

    return new_ef, new_interval, repetitions + 1


def get_due_topics(db_path: str | None = None) -> list[dict]:
    """Return topics where next_review <= today and active = 1, ordered by tier ASC, easiness_factor ASC."""
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, name, tier, easiness_factor, interval_days, repetitions, next_review, weak_areas
            FROM topics
            WHERE next_review <= date('now')
              AND active = 1
            ORDER BY tier ASC, easiness_factor ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def update_topic_after_session(db_path: str | None = None, topic_id: int = 0, quality: int = 3) -> None:
    """Run calculate_next_review and persist results to the topics table."""
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT easiness_factor, interval_days, repetitions FROM topics WHERE id = ?",
            (topic_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Topic id={topic_id} not found")

        new_ef, new_interval, new_reps = calculate_next_review(
            quality=quality,
            easiness_factor=row["easiness_factor"],
            interval_days=row["interval_days"],
            repetitions=row["repetitions"],
        )
        next_review = (date.today() + timedelta(days=new_interval)).isoformat()

        conn.execute(
            """
            UPDATE topics
            SET easiness_factor = ?,
                interval_days   = ?,
                repetitions     = ?,
                next_review     = ?,
                updated_at      = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_ef, new_interval, new_reps, next_review, topic_id),
        )
        conn.commit()
    finally:
        conn.close()
