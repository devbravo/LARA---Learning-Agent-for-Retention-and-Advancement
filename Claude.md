# LARA — CLAUDE.md

Personal Learning Assistant for Diego Sabajo. Tracks study topics via SM-2
spaced repetition, sends proactive daily plans via Telegram, reads Google
Calendar to plan around real schedule, generates study briefs via Claude API,
books [Mock] events on Google Calendar after user confirmation, and 
creates or rebooks [Study] events for the in-progress study flow.

**Stack:** Python 3.11+, LangGraph, FastAPI, APScheduler, SQLite, Telegram Bot API

---

## LangGraph nodes

| Node | Responsibility |
|---|---|
| `router` | Entry point. Reads checkpointed state. Routes by trigger. |
| `daily_planning` | Assembles morning plan from calendar + SM-2 + gap finder. Sets proposed_slots. |
| `on_demand` | Handles `/study` flow. Picks highest-priority due topic. |
| `done_parser` | Finds first unlogged slot from proposed_slots. Sends rating buttons. |
| `log_session` | Logs session row with quality score. Prompts for weak areas. |
| `log_weak_areas` | Saves weak areas (or clears on Skip). Prompts for next unlogged slot or ends. |
| `calendar_reader` | Read-only GCal fetch. |
| `sm2_engine` | Returns due topics ranked by tier + easiness factor. Pure Python. |
| `gap_finder` | Computes free windows respecting protected blocks. Pure Python. |
| `generate_brief` | Calls Claude API. Only node that uses an LLM. |
| `confirm` | Sends plan to Telegram. Awaits button tap. |
| `output` | Final Telegram send + GCal write after confirmation. |

## Triggers

| Trigger | What it starts |
|---|---|
| APScheduler daily | `daily_planning` → `confirm` → `output` |
| APScheduler Sunday | Weekly planning variant of `daily_planning` |
| APScheduler evening | `daily_planning` (tomorrow preview) → `output` |
| `/study` | `on_demand` → `generate_brief` → `confirm` |
| Duration tap (`30/45/60 min`) | `on_demand` → `generate_brief` → `confirm` |
| `confirm` tap | `output` → writes GCal events |
| `/done` | `done_parser` → END (waits for rating tap) |
| Rating tap (😕 😐 😊) | `log_session` → `output` → END (waits for weak areas reply) |
| Weak areas reply or Skip | `log_weak_areas` → `output` → END |
| `/briefing` | `daily_planning` (manual trigger for testing) |
| `/studied` | Webhook helper sends in-progress topic picker (inline buttons) |
| `studied:<topic_id>` tap | Promotes topic to `active`, resets SM-2 fields, sets first review for tomorrow |

## Graph flow

```
START → router → daily_planning → confirm → END
                               └→ output → END (no plan)

               → daily_planning (evening trigger) → output → END (preview only)

               → on_demand → generate_brief → confirm → END

               → done_parser → END (sends rating buttons, waits)

               → log_session → output → END (sends weak areas prompt, waits)

               → log_weak_areas → output → END
```

---

## Agent module structure (`src/agent/`)

```
src/
  agent/
    graph.py                 # LangGraph wiring + checkpointer
    nodes.py                 # Node orchestration and state transitions
    planning_helpers.py      # Study-event matching, synthetic busy blocks, rebooking
    daily_planning_helpers.py  # Daily/evening message sections + mock slot packing
    formatting.py            # Shared time/date formatting helpers
```

`daily_planning` in `nodes.py` should stay orchestration-focused and use helper
modules for section assembly and slot packing logic.

---

## Done flow — session logging

Triggered by `/done`. No structured paste required.

1. `/done` → `done_parser` finds first unlogged slot from `proposed_slots`
2. Sends rating buttons: "How did {topic} go?" `[😕 Hard] [😐 OK] [😊 Easy]`
3. Rating tap → `log_session` logs session row, updates SM-2, sends weak areas prompt
4. Text reply → saved to `sessions.weak_areas` + overwrites `topics.weak_areas`
5. Skip tap → clears `topics.weak_areas` to NULL (historical record in sessions preserved)
6. If more unlogged slots → repeat from step 2 for next topic
7. All logged → "All sessions logged for today. Great work! 💪"

| Button | Score | SM-2 effect |
|---|---|---|
| 😕 Hard | 2 | Below threshold — interval resets |
| 😐 OK | 3 | Passes — modest growth |
| 😊 Easy | 5 | Confident recall — fast growth |

**Weak areas design:**
- `topics.weak_areas` = operational field, drives brief generation context
- `sessions.weak_areas` = immutable historical record per session (future dashboard)
- Cleared on Skip = "no unresolved weak areas for next brief"
- Overwritten on new text = fresh unresolved issues

---

## Calendar safety rule — CRITICAL

Never modify a Google Calendar event unless `creator.self == True`.
The agent reads all events to plan around them but only writes events it created.
All agent-created events are prefixed `[Mock]`.

Enforce this in the calendar write path (tool/integration boundary):
```python
if not event.get("creator", {}).get("self", False):
    raise PermissionError("Cannot modify event not created by this agent")
```

---

## Telegram UX

**Morning briefing:**
```
☀️ Good morning Diego — <Day> <Date>

📅 Your day:
  <time> Event name (duration)

🧠 Today's mock interview(s) plan:
  <time>–<time> [Mock] <topic> (<duration>min)

Confirm these mock interview blocks?
[Yes, book them] [Skip]
```

**On-demand study:** `/study` triggers a default on-demand brief (currently 30min unless a duration is provided via callback).

**Done flow:** `/done` → rating buttons → weak areas prompt → next topic or done

**Never message during:** protected block defined in `config.yaml` (current default in repo: `15:00-19:00`).

---

## State fields (AgentState)

| Field | Type | Purpose |
|---|---|---|
| `trigger` | str | Routing signal |
| `chat_id` | int | Telegram chat ID / LangGraph thread_id |
| `message_id` | int | Telegram message_id for button removal |
| `duration_min` | int | Requested session duration |
| `proposed_topic` | str | Single-slot flow (on_demand) |
| `proposed_slot` | dict | Single-slot flow (on_demand) |
| `proposed_slots` | list[dict] | Multi-slot flow (daily_planning) |
| `has_study_plan` | bool | False → skip confirm, go to output |
| `preview_only` | bool | True for evening preview (skip confirm, go to output) |
| `current_topic_id` | int | Topic currently being rated/logged |
| `current_topic_name` | str | Topic name for display |
| `awaiting_weak_areas` | bool | True = next plain text is a weak areas reply |
| `quality_score` | int | SM-2 rating: 2, 3, or 5 |
| `messages` | list[str] | Outbound Telegram messages |

---

## HTTP endpoints

| Method | Path | Defined in | Purpose |
|---|---|---|---|
| POST | `/webhook` | `src/api/routes/webhook.py` | Telegram webhook receiver |
| GET | `/health` | `src/api/routes/health.py` | VPS uptime check |
| GET | `/scheduler-status` | `src/api/routes/scheduler_status.py` | Scheduler running state + job metadata |

---

## API structure (`src/api/`)

```
src/
  api/
    app.py               # FastAPI app factory + lifespan (scheduler start/stop)
    routes/
      health.py          # GET /health
      webhook.py         # POST /webhook — auth check + parse → delegates to handle_update()
      scheduler_status.py  # GET /scheduler-status
    telegram/
      __init__.py
      handler.py         # handle_update() — dedup + parse + dispatch (thin orchestrator)
      intent_parser.py   # Intent dataclass; parse_callback / parse_message
      callback_handlers.py  # one function per callback type (confirm, skip, rating, etc.)
      message_handlers.py   # one function per command (/done, /study, /briefing, etc.)
      dispatcher.py      # _invoke_safe, dedup sets, idempotency lock
  server.py              # Backwards compat: from src.api.app import app
  services/
    topic_service.py     # graduate_topic(), get_in_progress_topics() — studied: DB logic
```

**Responsibilities:**
- `src/api/app.py` — app factory only; registers routers, manages scheduler lifespan
- `src/api/routes/webhook.py` — validates `X-Telegram-Bot-Api-Secret-Token`, parses raw JSON into `TelegramUpdate`, delegates to `handle_update()`
- `src/api/telegram/handler.py` — thin orchestrator: dedup via dispatcher, parse via intent_parser, dispatch to invoke_safe or return direct response
- `src/api/telegram/dispatcher.py` — owns dedup sets (`_processed_updates`, `_in_flight_message_ids`, `_confirmed_message_ids`), idempotency lock, `invoke_safe()`
- `src/api/telegram/intent_parser.py` — defines `Intent` dataclass; `parse_callback` and `parse_message` delegate to handler modules
- `src/api/telegram/callback_handlers.py` — one function per callback type; handles idempotency checks; `handle_studied` returns `JSONResponse` directly
- `src/api/telegram/message_handlers.py` — one function per command; `handle_studied_command` returns `JSONResponse` directly
- `src/services/topic_service.py` — `graduate_topic()` and `get_in_progress_topics()`; uses `get_connection()` from `src.core.db`
- `src/server.py` — one-liner re-export (`from src.api.app import app`) to preserve the `from src.server import app` import in `main.py`

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
- Prefer `get_connection()` from `src.core.db` for SQLite access (legacy direct connections may still exist)
- Error handling required in all nodes — catch exceptions, return user-friendly messages
- Never overwrite checkpointed state with None — only pass kwargs that are explicitly provided

## Security

- `.env` is never committed
- `credentials/` is never committed
- Validate `X-Telegram-Bot-Api-Secret-Token` header on every webhook request
- SQLite files are local only — never exposed via HTTP