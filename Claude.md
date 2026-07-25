# LARA — CLAUDE.md

Personal Learning Assistant for Diego Sabajo. SM-2 spaced repetition via SQLite,
proactive daily plans via Telegram, Google Calendar integration, study briefs via
Claude API. A second, independent LangGraph graph (`src/knowledge/`) does JIT article
ingestion into a Neo4j concept graph, triggered via `/prepare <topic>` in Telegram.

**Stack:** Python 3.11+, LangGraph, FastAPI, APScheduler, SQLite, Telegram Bot API,
Neo4j, Voyage AI, Jina AI

---

## Two graphs, not one

The study system (`src/agent/`) and the knowledge graph pipeline (`src/knowledge/`)
are **separate compiled LangGraph `StateGraph`s**, each with its own `SqliteSaver`
checkpointer, sharing the same `db/state.db` file but namespaced by thread id:
the study graph uses bare `{chat_id}`, the knowledge graph uses `kg_{chat_id}`.
Never let a node from one graph reach into the other's state.

`src/api/telegram/handler.py` decides which graph a Telegram update belongs to
**before** `dispatcher.py` ever sees it: `/prepare` commands and `kg_`-prefixed
callbacks go straight to `src/knowledge/graph.py`; everything else goes to
`dispatcher.py` → the study graph. Adding a new top-level command means deciding
which graph it belongs to at this routing layer, not inside a node.

---

## HITL Pattern — non-negotiable

Every Telegram interaction either **starts a fresh flow** or **resumes a paused one**.
`dispatcher.py` is the only place in the HTTP layer that reads graph state — solely
to check `has_pending_interrupt()`. All routing decisions live inside the graph.
This applies identically inside `src/knowledge/graph.py` (its own
`_has_pending_interrupt` check, same shape, separate thread namespace).

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
This holds in both graphs — e.g. `prepare_confirm_node` in the knowledge graph follows
it exactly like `await_daily_confirmation` or `log_session` do in the study graph.

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

## Knowledge graph write — atomic-ish dual write

`src/knowledge/write.py::write_concept_note()` writes to SQLite (`concept_notes`) and
Neo4j (`Concept`/`ConceptNote` + relationships) for a single approval. The SQLite
`INSERT` is only committed **after** the Neo4j transaction commits successfully; any
exception rolls both back. This is not a true distributed transaction — if the process
dies between the two commit calls, the Neo4j `ConceptNote` node can outlive the SQLite
row. Don't "fix" this with a two-phase commit; it's a known, accepted edge case for a
single-user POC. If you touch this function, keep the ordering (Neo4j commit, then
SQLite commit) — reversing it would let orphaned SQLite rows reference concepts that
never made it into the graph, which is worse.

**`resolve_concept()` and `find_prior_concept_notes()` both use a hardcoded 0.75
cosine-similarity threshold** for matching against existing Neo4j `Concept` nodes.
This is unvalidated/tunable, not a load-bearing constant — don't assume it's been
tested against real data distributions.

**`extract_clean_text()` and `extract_top_results()` in `src/knowledge/extract.py` are
deprecated.** Jina's search endpoint already returns full article content, so the
separate reader-fetch step they supported is gone. Don't call them in new code; don't
delete them without checking nothing imports them first.

**`src/compat/langgraph_sqlite_shim.py` is currently unused.** Both graphs import the
real `langgraph.checkpoint.sqlite.SqliteSaver`. Don't wire code to this shim without
first checking why it exists and whether the real package still works in your
environment.

---

## Discuss flow — MCP is the entry point, not Telegram

`/discuss` doesn't run the mock session inside LARA. `discuss_parser` /
`start_discuss` just hand off context; the actual mock is a live conversation in an
external Claude session that calls LARA's MCP tools (`src/api/routes/mcp.py`):
`get_topic_context`, `log_session`, `get_discuss_context`, `assess_discuss_readiness`.
`assess_discuss_readiness()` (in `src/services/discuss_service.py`) is what decides
`ready` / `not_ready` / `go_back_to_study`, and on `ready` it invokes the study graph
itself via `dispatcher.safe_chat_invoke(chat_id, {"trigger": "discuss_ready_confirm", ...})`
— i.e. the MCP tool layer can push a state transition into the study graph
asynchronously, outside the normal webhook request. Keep this indirection in mind: a
bug in the discuss flow may originate in the MCP tool call, not in `nodes.py`.

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

# Exercise the knowledge graph pipeline without Telegram
python -m src.knowledge.search "<topic>"       # search + rerank only
python -m src.knowledge.synthesize "<topic>"   # search → synthesis, prints the concept note
python -m src.knowledge.dev_trigger "<topic>"  # sends real Telegram preview (server must be running to handle the button tap)
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