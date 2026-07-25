"""Tests for Keep/Discard decision logging on concept notes.

Approvals are logged by ``write_concept_note`` (user_interest='confirmed');
rejections by ``record_declined_note`` (user_interest='declined'). Together
they make the discard rate queryable — the pipeline's zero-cost quality
metric.
"""

import json
import sqlite3

from unittest.mock import patch

import src.knowledge.write as write_mod
from src.knowledge.write import record_declined_note

# Mirrors migrations/migrate_add_articles_notes.py — including the CHECK
# constraint, so this test proves 'declined' passes it.
_CONCEPT_NOTES_SCHEMA = """
CREATE TABLE concept_notes (
    id INTEGER PRIMARY KEY,
    topic_keyword TEXT NOT NULL,
    synthesized_text TEXT NOT NULL,
    source_urls TEXT NOT NULL,
    created_at TEXT NOT NULL,
    user_interest TEXT NOT NULL DEFAULT 'pending',
    CHECK (user_interest IN ('pending', 'confirmed', 'declined'))
);
"""

_SYNTHESIS_RESULT = {
    "synthesized_note": "A dense note about caching.",
    "concepts": ["prefix caching"],
    "proposed_relationships": [],
    "source_urls": ["https://example.com/a", "https://example.com/b"],
}


def test_record_declined_note_inserts_declined_row(tmp_path):
    db_file = tmp_path / "learning.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(_CONCEPT_NOTES_SCHEMA)
    conn.commit()
    conn.close()

    with patch.object(
        write_mod, "get_connection", side_effect=lambda: sqlite3.connect(db_file)
    ):
        record_declined_note("caching", _SYNTHESIS_RESULT)

    row = (
        sqlite3.connect(db_file)
        .execute(
            "SELECT topic_keyword, synthesized_text, source_urls, user_interest, created_at "
            "FROM concept_notes"
        )
        .fetchone()
    )
    assert row is not None
    topic_keyword, synthesized_text, source_urls, user_interest, created_at = row
    assert topic_keyword == "caching"
    assert synthesized_text == "A dense note about caching."
    assert json.loads(source_urls) == _SYNTHESIS_RESULT["source_urls"]
    assert user_interest == "declined"
    assert created_at  # populated, format owned by local_now()
