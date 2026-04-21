# LARA — CLAUDE.md

Personal Learning Assistant for Diego Sabajo. Tracks study topics via SM-2
spaced repetition, sends proactive daily plans via Telegram, reads Google
Calendar to plan around real schedule, generates study briefs via Claude API,
books [Mock] events on Google Calendar after user confirmation, and 
creates or rebooks [Study] events for the in-progress study flow.

**Stack:** Python 3.11+, LangGraph, FastAPI, APScheduler, SQLite, Telegram Bot API

---

## Architecture — HITL Pattern

Every Telegram interaction either **starts a fresh flow** or **resumes a paused one**.
The webhook does not decide which node to run. The graph decides.

```
Telegram → FastAPI /webhook → dispatcher.py
                                  │
                    ┌─────────────┴──────────────┐
                    │                            │
             has pending interrupt?         no interrupt
                    │                            │
        graph.invoke(Command(resume=payload))   graph.invoke({trigger, chat_id})
                    │                            │
                    └─────────────┬──────────────┘
                                  │
                            LangGraph graph
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
               Google Calendar  SQLite      Claude API
               (read + write)  (SM-2 state,  (study briefs
                                sessions,     only)
                                checkpoints)
```

**Key principle:** `dispatcher.py` is the only place in the HTTP layer that reads
graph state — solely to check `has_pending_interrupt()`. All routing decisions
live in `route_from_router`.

---

## LangGraph nodes

| Node | Responsibility |
|---|---|
| `router` | Entry point. Routes fresh triggers only. 7 targets. |
| `daily_planning` | Assembles morning plan from calendar + SM-2 + gap finder. Sets proposed_slots. Calls interrupt(). |
| `weekend_brief` | Sat/Sun brief. Shows SM-2 due topics with weak areas + overdue indicators. No interrupt. |
| `send_duration_picker` | Sends "How long do you have?" buttons. Cleans up stale picker. Calls interrupt(). |
| `on_demand` | Picks highest-priority due topic for requested duration. |
| `generate_brief` | Calls Claude API. Only node that uses an LLM. |
| `book_events` | Writes GCal events after user confirms. Handles both single and multi-slot flows. |
| `done_parser` | Finds first unlogged slot from proposed_slots. Sends rating buttons. Calls interrupt(). |
| `log_session` | Logs session row with quality score. Updates SM-2. Sends weak areas prompt. Calls interrupt(). |
| `log_weak_areas` | Saves weak areas (or clears on Skip). Prompts for next unlogged slot or ends. |
| `study_topic` | Starts /pick flow. Sends category inline buttons. Cleans up stale subtopic lists. Calls interrupt(). |
| `study_topic_category` | Handles category resume. Sends matching subtopic inline buttons. Calls interrupt(). |
| `study_topic_confirm` | Marks selected topic as in_progress. Sets confirmation message. |
| `activate_topic` | Lists in-progress topics as inline buttons. Calls interrupt(). |
| `graduate_topic` | Graduates selected topic to active SM-2. Sets confirmation message. |
| `output` | Shared terminal node. Sends state["messages"][-1] via Telegram and ends. |

---

## Graph flows

**Morning / Evening briefing:**
```
START → router → daily_planning → interrupt() →
[resume: "yes, book them"] → book_events → output → END
[resume: "skip"] → output → END
```

**Weekend brief:**
```
START → router → weekend_brief → output → END
```

**On-demand study (/study):**
```
START → router → send_duration_picker → interrupt() →
[resume: "30 min" | "45 min" | "60 min"] → on_demand → generate_brief → interrupt() →
[resume: "yes, book them"] → book_events → output → END
[resume: "skip"] → output → END
```

**Done / logging (/done):**
```
START → router → done_parser → interrupt() →
[resume: "😕 hard" | "😐 ok" | "😊 easy"] → log_session → interrupt() →
[resume: <text> | "skip"] → log_weak_areas →
  if more unlogged slots → interrupt() → [repeat from log_session]
  else → output → END
```

**Pick a topic (/pick):**
```
START → router → study_topic → interrupt() →
[resume: "category:DSA" | ...] → study_topic_category → interrupt() →
[resume: "subtopic_id:14" | ...] → study_topic_confirm → output → END
```

**Activate a topic (/activate):**
```
START → router → activate_topic → interrupt() →
[resume: "studied:14" | ...] → graduate_topic → output → END
```

---

## Router — fresh entry points only

```python
mapping = {
    "daily":    "daily_planning",
    "evening":  "daily_planning",
    "weekend":  "weekend_brief",
    "study":    "send_duration_picker",
    "done":     "done_parser",
    "pick":     "study_topic",
    "activate": "activate_topic",
}
```

These are the only valid fresh triggers. Everything else is a resume value.

---

## Dispatcher bifurcation (`dispatcher.py`)

```python
def invoke_safe(chat_id: int, payload: str) -> None:
    state = graph.get_state(chat_id)
    if has_pending_interrupt(state):
        graph.invoke(Command(resume=payload), config=...)
    else:
        trigger = resolve_trigger(payload)  # "/done" → "done", "/pick" → "pick"
        graph.invoke({"trigger": trigger, "chat_id": chat_id}, config=...)

def has_pending_interrupt(state) -> bool:
    tasks = getattr(state, "tasks", [])
    return any(getattr(t, "interrupts", None) for t in tasks)
```

---

## Telegram layer — responsibilities

**`handler.py`** — thin orchestrator only:
- Deduplicates updates
- Extracts `chat_id` + `payload` from callback or message text
- Calls `dispatcher.invoke_safe(chat_id, payload)`
- Returns direct response for `/help` and `/view` only

**`dispatcher.py`** — owns:
- Dedup sets (`_processed_updates`, `_in_flight_message_ids`, `_confirmed_message_ids`)
- `has_pending_interrupt()` check
- Bifurcation between `Command(resume=...)` and fresh `invoke`
- `invoke_safe()` thread safety

**`callback_handlers.py`** — Telegram callback mechanics only:
- Idempotency guards (dedup repeat taps)
- Returns raw callback payload for downstream handling
- No routing decisions
- No graph state reads

**`message_handlers.py`** — command normalization + static responses:
- Maps command text to payload string
- `/help` → direct response (no graph)
- `/view` → direct response (no graph)
- All other commands → payload passed to dispatcher

**`intent_parser.py`** — payload normalization only:
- Extracts and normalizes callback data or message text
- No routing decisions

---

## Done flow — session logging

Triggered by `/done`. No structured paste required.

1. `/done` → `done_parser` finds first unlogged slot from `proposed_slots`
2. Sends rating buttons: "How did {topic} go?" `[😕 Hard] [😐 OK] [😊 Easy]`
3. Rating resume → `log_session` logs session row, updates SM-2, sends weak areas prompt
4. Text resume → saved to `sessions.weak_areas` + overwrites `topics.weak_areas`
5. Skip resume → clears `topics.weak_areas` to NULL (historical record in sessions preserved)
6. If more unlogged slots → repeat from step 2 for next topic
7. All logged → "All sessions logged for today. Great work! 💪"

| Button | Score | SM-2 effect |
|---|---|---|
| 😕 Hard | 2 | Below threshold — interval resets |
| 😐 OK | 3 | Passes — modest growth |
| 😊 Easy | 5 | Confident recall — fast growth |

**Weak areas design:**
- `topics.weak_areas` = operational field, drives brief generation context
- `sessions.weak_areas` = immutable historical record per session
- Cleared on Skip = "no unresolved weak areas for next brief"
- Overwritten on new text = fresh unresolved issues

---

## State fields (AgentState)

| Field | Type | Purpose |
|---|---|---|
| `trigger` | str | Fresh flow routing signal |
| `chat_id` | int | Telegram chat ID / LangGraph thread_id |
| `message_id` | int | Telegram message_id for button removal |
| `duration_min` | int | Requested session duration |
| `proposed_topic` | str | Single-slot flow (on_demand) |
| `proposed_slot` | dict | Single-slot flow (on_demand) |
| `proposed_slots` | list[dict] | Multi-slot flow (daily_planning) |
| `has_study_plan` | bool | False → skip confirm, go to output |
| `preview_only` | bool | True for evening preview |
| `current_topic_id` | int | Topic currently being rated/logged |
| `current_topic_name` | str | Topic name for display |
| `quality_score` | int | SM-2 rating: 2, 3, or 5 |
| `messages` | list[str] | Outbound Telegram messages |
| `study_topic_category` | str | Selected category in /pick flow |
| `pending_subtopic_message_id` | int | message_id of last sent subtopic list |
| `pending_picker_message_id` | int | message_id of last sent duration picker |

**Removed from state:**
- `awaiting_weak_areas` — no longer needed; HITL interrupt() replaces this flag

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

## Agent module structure (`src/agent/`)

```
src/
  agent/
    graph.py                   # LangGraph wiring + checkpointer
    nodes.py                   # All node implementations + AgentState
    planning_helpers.py        # Study-event matching, rebooking helpers
    daily_planning_helpers.py  # Daily/evening message sections + slot packing
    formatting.py              # Shared time/date formatting helpers
```

---

## API structure (`src/api/`)

```
src/
  api/
    app.py               # FastAPI app factory + lifespan (scheduler start/stop)
    routes/
      health.py          # GET /health
      webhook.py         # POST /webhook — auth + parse → handle_update()
      scheduler_status.py
    telegram/
      handler.py         # Thin orchestrator: dedup + extract payload + dispatch
      intent_parser.py   # Payload normalization only
      callback_handlers.py  # Telegram mechanics only (idempotency, remove_buttons)
      message_handlers.py   # Command normalization + /help + /view direct responses
      dispatcher.py      # has_pending_interrupt() + bifurcation + invoke_safe()
      types.py           # Shared type definitions
  server.py              # Backwards compat re-export
  services/
    topic_service.py     # graduate_topic(), get_in_progress_topics()
```

---

## Test pattern for multi-turn flows

```python
# HITL pattern — one flow, multiple resume steps
config = {"configurable": {"thread_id": str(chat_id)}}

graph.invoke({"trigger": "pick", "chat_id": chat_id}, config=config)
graph.invoke(Command(resume="category:DSA"), config=config)
graph.invoke(Command(resume="subtopic_id:14"), config=config)
```

---

## Environment variables

```
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
GOOGLE_CALENDAR_ID=
GOOGLE_CREDENTIALS_PATH=credentials/gcal_credentials.json
DATABASE_PATH=db/learning.db
STATE_DATABASE_PATH=db/state.db
WEBHOOK_SECRET=
```

---

## Database schema
**topics:** id, name, tier, status, easiness_factor, interval_days, repetitions,
next_review, weak_areas, updated_at

**sessions:** id, topic_id, studied_at, duration_min, quality_score, weak_areas,
suggestions

---

## Development principles
- POC first — minimum features that solve the real problem
- No LLM where a formula works — SM-2 and gap_finder are pure Python
- Claude API only inside `generate_brief` — no other node calls the LLM
- Calendar safety rule is non-negotiable — enforce it in calendar write boundaries
- Prefer `get_connection()` from `src.core.db` for SQLite access
- Error handling required in all nodes — catch exceptions, return user-friendly messages
- Never overwrite checkpointed state with None — only pass kwargs explicitly provided
- HITL pattern: interrupt() replaces manual awaiting_* flags in state

## Security
- `.env` is never committed
- `credentials/` is never committed
- Validate `X-Telegram-Bot-Api-Secret-Token` header on every webhook request
- SQLite files are local only — never exposed via HTTP
```
