# LARA

Personal Learning Assistant for Diego Sabajo. 
Tracks study topics using SM-2 spaced repetition, sends proactive daily plans via Telegram, 
reads Google Calendar to plan around your real schedule, generates focused study briefs via Claude, and books `[Mock]` events on Google Calendar after confirmation.
---

## Features

- **SM-2 spaced repetition** — topics ranked by tier and easiness factor; intervals grow automatically based on session quality
- **Morning briefing** — sent daily via Telegram with your calendar, free windows, and assigned study blocks
- **On-demand study** — send `/study` to generate a brief for the highest-priority due topic (defaults to 30 min unless a duration callback is provided)
- **Done flow** — send `/done` after studying; LARA asks how each session went, prompts for weak areas, logs everything, and updates SM-2
- **In-progress graduation flow** — send `/activate`, pick an in-progress topic, and promote it to active with first review scheduled for tomorrow
- **Calendar safety** — reads all events to plan around them and only creates new tagged `[Mock]` and `[Study]` events; it does not modify unrelated calendar entries
- **Protected block** — never sends messages or fires jobs during configured protected hours

---

## Architecture

```
Telegram ──► FastAPI /webhook ──► LangGraph graph ──► Telegram
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                   │
               Google Calendar      SQLite            Claude API
               (read + write)    (SM-2 state,       (study briefs
                                  sessions log)       only)
APScheduler ──► daily_planning (Mon–Fri morning + evening preview) / weekend_brief (Sat–Sun)
```

### LangGraph nodes

| Node | Responsibility |
|---|---|
| `router` | Entry point — routes by trigger type |
| `daily_planning` | Assembles morning plan from calendar + SM-2 + gap finder |
| `weekend_brief` | Sat/Sun brief — shows due topics with weak areas and overdue indicators |
| `on_demand` | Handles `/study` flow, picks highest-priority due topic |
| `done_parser` | Finds first unlogged slot, sends rating buttons |
| `log_session` | Logs session with quality score, prompts for weak areas |
| `log_weak_areas` | Saves weak areas or clears on Skip, prompts for next topic |
| `generate_brief` | Calls Claude API — the only LLM call in the graph |
| `confirm` | Sends plan to Telegram with inline keyboard; waits for tap |
| `output` | Final Telegram send + GCal write after confirmation |
| `study_topic` | Starts `/pick` flow, sends category inline buttons, cleans up stale lists |
| `study_topic_category` | Handles category tap, sends matching subtopic inline buttons |
| `study_topic_confirm` | Marks selected topic as `in_progress`, notifies user |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent framework | LangGraph |
| Web server | FastAPI + uvicorn |
| Scheduler | APScheduler (AsyncIO) |
| LLM | Anthropic Claude (via `anthropic` SDK) |
| Messaging | python-telegram-bot |
| Calendar | Google Calendar API v3 (OAuth2) |
| Database | SQLite (via `langgraph-checkpoint-sqlite`) |
| Config | YAML + python-dotenv |

---

## Project Structure

```
lara/
├── config.yaml              # Schedule, focus windows, protected blocks
├── topics.yaml              # Study topic catalog (tier/status/default duration)
├── requirements.txt
├── .env.example
├── pytest.ini
├── db/                      # SQLite files (gitignored)
├── credentials/             # GCal OAuth credentials (gitignored)
├── src/
│   ├── main.py              # Entry point — starts FastAPI + scheduler
│   ├── server.py            # Backwards compat re-export: from src.api.app import app
│   ├── agent/
│   │   ├── graph.py                   # LangGraph graph + SqliteSaver checkpointer
│   │   ├── nodes.py                   # Node orchestration + AgentState
│   │   ├── planning_helpers.py        # Study-event matching + rebooking helpers
│   │   ├── daily_planning_helpers.py  # Daily/evening section builders + slot packing
│   │   ├── formatting.py              # Shared time/date formatting helpers
│   │   └── tools.py                   # LangGraph tools
│   ├── api/
│   │   ├── app.py           # FastAPI app factory + lifespan
│   │   ├── routes/
│   │   │   ├── health.py              # GET /health
│   │   │   ├── webhook.py             # POST /webhook (auth + parse)
│   │   │   └── scheduler_status.py    # GET /scheduler-status
│   │   └── telegram/
│   │       ├── handler.py             # handle_update() — thin orchestrator
│   │       ├── intent_parser.py       # Intent dataclass; parse_callback / parse_message
│   │       ├── callback_handlers.py   # one function per callback type
│   │       ├── message_handlers.py    # one function per command
│   │       └── dispatcher.py          # dedup sets, idempotency lock, invoke_safe()
│   ├── core/
│   │   ├── db.py            # Schema init, seed, connection helper
│   │   ├── sm2.py           # SM-2 algorithm (pure Python)
│   │   └── gap_finder.py    # Free window computation (pure Python)
│   ├── infrastructure/
│   │   └── scheduler.py     # APScheduler jobs (weekday, weekend, evening)
│   ├── integrations/
│   │   ├── gcal.py          # Google Calendar read + write
│   │   ├── telegram_client.py  # send_message / send_buttons / remove_buttons
│   │   └── claude_api.py    # generate_brief()
│   ├── repositories/
│   │   ├── session_repository.py
│   │   ├── sm2_repository.py
│   │   └── topic_repository.py
│   └── services/
│       └── topic_service.py # graduate_topic(), get_in_progress_topics()
│       └── view_service.py
└── tests/
    ├── test_sm2.py
    ├── test_gap_finder.py
    ├── test_tools.py
    ├── test_study_topic.py
    ├── test_nodes_daily_planning.py
    ├── test_nodes_weekend_brief.py
    ├── test_repositories.py
    ├── test_dispatcher.py
    └── test_webhook_handler.py
```

---

## Setup

### 1. Clone and create virtual environment

```bash
git clone <repo-url>
cd lara
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
GOOGLE_CALENDAR_ID=...
GOOGLE_CREDENTIALS_PATH=credentials/gcal_credentials.json
DATABASE_PATH=db/learning.db
STATE_DATABASE_PATH=db/state.db
WEBHOOK_SECRET=   # generate: python -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Google Calendar credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Desktop app)
3. Download the JSON and save to `credentials/gcal_credentials.json`
4. On first run, a browser window opens for OAuth consent — token saved to `credentials/token.json`

### 4. Initialise the database

```bash
python -m src.core.db
```

Creates `db/learning.db`, seeds topics from `topics.yaml`, and prints them to confirm.

### 5. Register the Telegram webhook

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-domain>/webhook&secret_token=<WEBHOOK_SECRET>"
```

---

## Running

```bash
python -m src.main
```

Starts FastAPI (port 8000) and APScheduler in a single async process.

### Scheduler status

```bash
curl http://localhost:8000/scheduler-status
```

### Health check

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

### Dry-run morning briefing

```bash
python -m src.agent.graph
```

---

## SQL queries for manual DB inspection:
```sql
sqlite3 db/learning.db "SELECT id, topic_id, studied_at FROM sessions WHERE topic_id = (SELECT id FROM topics WHERE name = '(TOPIC_NAME)') ORDER BY studied_at DESC LIMIT 5"
sqlite3 db/learning.db "DELETE FROM sessions WHERE id = (ID)"
sqlite3 db/learning.db "UPDATE topics SET easiness_factor = 2.5, interval_days = 1, repetitions = 0, next_review = date('now'), updated_at = CURRENT_TIMESTAMP WHERE id = (TOPIC_ID)"
sqlite3 db/state.db "DELETE FROM checkpoints; DELETE FROM writes;"
``` 

## Resetting the learning database
```commandline
rm db/learning.db
python -m src.core.db
```
## Reseed after topic changes:
```commandline
python -m src.core.db
``` 

## Change a topic's status: 
```sql
# Activate (move to SM-2) 
sqlite3 db/learning.db "UPDATE topics SET status = 'active', next_review = date('now'), updated_at = CURRENT_TIMESTAMP WHERE name = 'TOPIC_NAME'"

# Mark in_progress
sqlite3 db/learning.db "UPDATE topics SET status = 'in_progress', updated_at = CURRENT_TIMESTAMP WHERE name = 'TOPIC_NAME'" 

# Deactivate (remove from SM-2)
sqlite3 db/learning.db "UPDATE topics SET status = 'inactive', updated_at = CURRENT_TIMESTAMP WHERE name = 'TOPIC_NAME'"
```

## Telegram UX

### Morning briefing

```
☀️ Good morning Diego — Tuesday April 7

📅 Your day:
  09:00 Team standup (30min)

🎯 Today's mock interview(s):
  10:00–11:00 [Mock] Gen AI System Design (60min)
  11:00–12:00 [Mock] Data Structures and Algorithms (60min)

Confirm these mock interview blocks?
[Yes, book them] [Skip]
```

### On-demand study

Send `/study` to generate an AI brief immediately (default 30 min).
Duration callbacks (`30/45/60 min`) are also supported when that keyboard is presented.

```
[30 min] [45 min] [60 min]
```

### Done flow

Send `/done` after studying:

```
LARA: How did Gen AI System Design go?
      [😕 Hard] [😐 OK] [😊 Easy]

[tap 😐 OK]

LARA: Any weak areas to note? Reply with text or tap Skip.
      [Skip]

You: Trade-offs in vector DB selection

LARA: How did Data Structures and Algorithms go?
      [😕 Hard] [😐 OK] [😊 Easy]

...

LARA: All sessions logged for today. Great work! 💪
```

| Button | Score | SM-2 effect |
|---|---|---|
| 😕 Hard | 2 | Interval resets to 1 day |
| 😐 OK | 3 | Modest growth |
| 😊 Easy | 5 | Fast growth |

---

## Customising topics

Edit `topics.yaml` and re-run `python -m src.core.db` to seed/update topics.
Seeding uses upsert semantics (`ON CONFLICT(name) DO UPDATE`) for `tier`, `status`, and conditional `next_review` handling.

```yaml
topics:
  - name: "Your Topic"
    tier: 1        # 1 = high priority, 2 = medium, 3 = background
```

Focus windows and protected blocks:

```yaml
focus_windows:
  - start: "08:00"
    end: "09:00"
  - start: "10:00"
    end: "22:00"

protected_blocks:
  - start: "15:00"
    end: "19:00"
```

---

## Tests

```bash
python -m pytest tests/ -v
```

Pure Python — no API calls needed.

---

## Security

- `.env` and `credentials/` are gitignored and never committed
- Every webhook request validated against `WEBHOOK_SECRET` (HTTP 403 on mismatch)
- Calendar write path creates new `[Mock]` events only; existing events are not modified
- SQLite files are local only — never exposed via HTTP

