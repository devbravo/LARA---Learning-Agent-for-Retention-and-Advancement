# LARA — CLAUDE.md

Personal Learning Assistant for Diego Sabajo. SM-2 spaced repetition via SQLite,
proactive daily plans via Telegram, Google Calendar integration, study briefs via
Claude API.

**Stack:** Python 3.11+, LangGraph, FastAPI, APScheduler, SQLite, Telegram Bot API

---

## HITL Pattern — non-negotiable

Every Telegram interaction either **starts a fresh flow** or **resumes a paused one**.
`dispatcher.py` is the only place in the HTTP layer that reads graph state — solely
to check `has_pending_interrupt()`. All routing decisions live inside the graph.

```python
# dispatcher.py — do not change this bifurcation
if has_pending_interrupt(state):
    graph.invoke(Command(resume=payload), config=...)
else:
    graph.invoke({"trigger": trigger, "chat_id": chat_id}, config=...)
```

**`interrupt()` must be the first statement in any node that uses it.**
No side effects, no DB writes, no Telegram sends before `interrupt()`.
This is load-bearing — violating it causes LangGraph to replay side effects on resume.

The pattern is always: **Node A sends buttons → Node B holds the interrupt.**
Never combine send + interrupt in the same node.

---

## Non-obvious design decisions

**`pending_message_id` is intentionally singular.**
At most one button message is ever active at a time. When a node sends new buttons,
it must remove the previous ones first via `pending_message_id`. Never add per-flow
message ID fields — collapse everything into `pending_message_id`.

**Never overwrite checkpointed state with None.**
Only return keys you are explicitly updating. Returning `{"some_field": None}` will
wipe that field from the checkpoint. Omit keys you don't intend to change.

**Claude API only inside `generate_brief`.**
No other node calls the LLM. SM-2 scheduling and gap-finding are pure Python.

**`get_connection()` from `src.infrastructure.db` for all SQLite access.**
Plain `sqlite3`, no ORM.

---

## Done flow — weak areas field semantics

- `topics.weak_areas` — operational field, cleared on Skip, overwritten on new input.
  Drives brief generation context for the next session.
- `sessions.weak_areas` — immutable historical record. Never update after insert.

---

## Calendar safety rule — CRITICAL

Never modify a Google Calendar event unless `creator.self == True`.
The agent reads all events to plan around them but only writes events it created.
All agent-created events are prefixed `[Mock]`.

```python
if not event.get("creator", {}).get("self", False):
    raise PermissionError("Cannot modify event not created by this agent")
```

---

## State — removed fields (do not reintroduce)

These were deliberately removed. Do not bring them back:
- `awaiting_weak_areas` — replaced by `interrupt()`
- Per-flow message ID fields (`pending_rating_message_id`, `pending_booking_message_id`, etc.)
  — collapsed into the single `pending_message_id`

---

## Bash commands

```bash
# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_sm2.py -v

# Start the server
uvicorn src.api.app:app --reload

# Tail logs
tail -f logs/lara.log

# Inspect the database
sqlite3 db/learning.db

# Check scheduled jobs
curl http://localhost:8000/scheduler-status
```

---

## Code style

- Python 3.11+ — use `X | Y` union types, not `Optional[X]`
- Type hints required on all function signatures (arguments and return type)
- Google-style docstrings on all public functions — `Args:` and `Returns:` sections required, `Raises:` when the function raises intentionally:

```python
def update_topic_weak_areas(topic_id: int, weak_areas: str | None) -> None:
    """Set or clear operational weak areas for a topic.

    Args:
        topic_id: Topic primary key.
        weak_areas: Weak-areas text or ``None`` to clear the field.

    Raises:
        ValueError: If topic_id does not exist.
    """
```

- No `Optional[X]` — use `X | None`
- No `Union[X, Y]` — use `X | Y`
- Private helpers prefixed with `_`
- `get_connection()` from `src.infrastructure.db` for all SQLite access — plain `sqlite3`, no ORM

---

## Development principles

- POC first — minimum features that solve the real problem
- No LLM where a formula works
- Error handling required in all nodes — return user-friendly messages, never raise to the user
- Calendar safety rule is non-negotiable