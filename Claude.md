# Kairos — CLAUDE.md

Personal Learning Assistant for Diego Sabajo. Tracks study topics via SM-2
spaced repetition, sends proactive daily plans via Telegram, reads Google
Calendar to plan around real schedule, generates study briefs via Claude API,
and books [Study] events on Google Calendar after user confirmation.

**Stack:** Python 3.11+, LangGraph, FastAPI, APScheduler, SQLite, Telegram Bot API

---

## LangGraph nodes

| Node | Responsibility |
|---|---|
| `router` | Entry point. Reads checkpointed state. Routes by intent. |
| `daily_briefing` | Assembles morning plan from calendar + SM-2 + gap finder |
| `on_demand` | Handles "I have X min" flow. Validates slot availability. |
| `done_parser` | Parses pasted session summary from Telegram |
| `calendar_reader` | Read-only GCal fetch. Shared by briefing and picker. |
| `sm2_engine` | Returns due topics ranked by tier + easiness factor. Pure Python. |
| `gap_finder` | Computes free windows respecting protected blocks. Pure Python. |
| `brief_generator` | Calls Claude API directly. Only node that uses an LLM. |
| `confirm` | Sends plan to Telegram. Awaits button tap. |
| `log_session` | Writes session log. Updates SM-2 state. Clears conversation state. |
| `output` | Final Telegram send + GCal write after confirmation. |

## Tools

5 tools total. A tool touches something outside the graph's own state.

| Tool | Used by |
|---|---|
| `get_calendar_events` | `calendar_reader` |
| `find_free_windows` | `gap_finder` |
| `get_due_topics` | `sm2_engine` |
| `write_calendar_event` | `output` |
| `log_study_session` | `log_session` |

**Not tools:** `generate_brief` is called directly inside `brief_generator` node.
Conversation state is handled by LangGraph's `SqliteSaver` checkpointer — never manually.

## Triggers

| Trigger | What it starts |
|---|---|
| APScheduler 8am daily | `daily_briefing` → `confirm` → `output` |
| APScheduler Sunday 9am | Weekly planning variant of `daily_briefing` |
| Telegram button tap (duration) | `on_demand` → `brief_generator` → `confirm` → `output` |
| Telegram "done" message | `done_parser` → `log_session` → `output` |
| Telegram confirmed booking | `output` → `write_calendar_event` |

---

## Calendar safety rule — CRITICAL

Never modify a Google Calendar event unless `creator.self == True`.
The agent reads all events to plan around them but only writes events it created.
All agent-created events are prefixed `[Study]`.

Always enforce this in `write_calendar_event`:
```python
if not event.get("creator", {}).get("self", False):
    raise PermissionError("Cannot modify event not created by this agent")
```

---

## Session summary format

Diego pastes this into Telegram after an external study session.
Parse it exactly — validate structure before logging, fail loudly if malformed.

```
📋 Session summary
Topic: <topic name matching topics table>
Duration: <N> min
Weak areas: <comma-separated>
Suggestions: <free text>
```

After parsing, send rating buttons. Only log_session fires after a rating tap.

| Button | Score | SM-2 effect |
|---|---|---|
| 😕 Hard | 2 | Below threshold — interval resets |
| 😐 OK | 3 | Passes — modest growth |
| 😊 Easy | 5 | Confident recall — fast growth |

---

## Telegram UX

**Duration picker:** inline keyboard `[30 min] [45 min] [60 min]`

**Morning briefing:**
```
☀️ Good morning Diego — <Day> <Date>

📅 Your day:
  <time> Event name (duration)

🧠 Study windows:
  <time>–<time> → <topic> (<duration>)

Confirm these study blocks? [Yes, book them] [Skip]
```

**Never message during:** 15:00–19:30 (hard protected block)

---

## HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook` | Telegram webhook receiver |
| GET | `/health` | VPS uptime check |

The scheduler fires internally — no HTTP trigger for scheduled jobs.

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
```

---

## Development principles

- POC first — minimum features that solve the real problem
- No LLM where a formula works — SM-2 and gap_finder are pure Python
- Claude API only inside `brief_generator` — no other node calls the LLM
- Calendar safety rule is non-negotiable — enforce it at the tool level
- Test tools in isolation before wiring into LangGraph
- Error handling is required in all integrations and in `done_parser`

## Security

- `.env` is never committed
- `credentials/` is never committed
- Validate `X-Telegram-Bot-Api-Secret-Token` header on every webhook request
- SQLite files are local only — never exposed via HTTP