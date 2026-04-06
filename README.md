# Kairos

Personal Learning Assistant for Diego Sabajo. Tracks study topics using SM-2 spaced repetition, sends proactive daily plans via Telegram, reads Google Calendar to plan around your real schedule, generates focused study briefs via Claude, and books `[Study]` events on Google Calendar after confirmation.

---

## Features

- **SM-2 spaced repetition** — topics are ranked by tier and easiness factor; intervals grow automatically based on session quality
- **Morning briefing** — sent at 08:00 daily via Telegram with your calendar, free windows, and top review picks
- **"I have X minutes"** flow — tap 30 / 45 / 60 min and get an AI-generated study brief for the highest-priority due topic
- **Session logging** — paste a structured summary into Telegram; the agent parses it, updates SM-2 state, and logs it to SQLite
- **Calendar safety** — reads all events to plan around them, but only writes events it created (`[Study]` prefix, `creator.self == True`)
- **Protected block** — never sends messages or fires jobs during 15:00–19:30

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
APScheduler ──► daily_planning (08:00 Mon–Sat, 09:00 Sun)
```

### LangGraph nodes

| Node | Responsibility |
|---|---|
| `router` | Entry point — routes by trigger type |
| `daily_planning` | Assembles morning plan from calendar + SM-2 + gap finder |
| `on_demand` | Handles "I have X min" flow, validates slot availability |
| `done_parser` | Parses and validates pasted session summaries |
| `calendar_reader` | Read-only GCal fetch |
| `sm2_engine` | Returns due topics ranked by tier + easiness factor |
| `gap_finder` | Computes free windows respecting protected blocks |
| `generate_brief` | Calls Claude API — the only LLM call in the graph |
| `confirm` | Sends plan to Telegram with inline keyboard; waits for tap |
| `log_session` | Writes session log, updates SM-2 state |
| `output` | Final Telegram send + GCal write after confirmation |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent framework | LangGraph 1.1 |
| Web server | FastAPI + uvicorn |
| Scheduler | APScheduler 3 (AsyncIO) |
| LLM | Anthropic Claude (via `anthropic` SDK) |
| Messaging | python-telegram-bot 22 |
| Calendar | Google Calendar API v3 (OAuth2) |
| Database | SQLite (via `langgraph-checkpoint-sqlite`) |
| Config | YAML + python-dotenv |

---

## Project Structure

```
learning-manager/
├── config.yaml              # Topics, focus windows, protected blocks
├── requirements.txt
├── .env.example
├── pytest.ini
├── db/                      # SQLite files (gitignored)
├── credentials/             # GCal OAuth credentials (gitignored)
├── src/
│   ├── main.py              # Entry point — starts FastAPI + scheduler
│   ├── server.py            # FastAPI webhook receiver
│   ├── scheduler.py         # APScheduler jobs
│   ├── agent/
│   │   ├── graph.py         # LangGraph graph + SqliteSaver checkpointer
│   │   ├── nodes.py         # All 11 node implementations + AgentState
│   │   └── tools.py         # 5 LangGraph tools
│   ├── core/
│   │   ├── db.py            # Schema init, seed, connection helper
│   │   ├── sm2.py           # SM-2 algorithm (pure Python)
│   │   └── gap_finder.py    # Free window computation (pure Python)
│   └── integrations/
│       ├── gcal.py          # Google Calendar read + write
│       ├── telegram.py      # send_message / send_buttons
│       └── claude_api.py    # generate_brief()
└── tests/
    ├── test_sm2.py          # 8 SM-2 unit tests
    ├── test_gap_finder.py   # 11 gap finder unit tests
    └── test_tools.py
```

---

## Setup

### 1. Clone and create virtual environment

```bash
git clone <repo-url>
cd learning-manager
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your real values:

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
3. Download the JSON and save it to `credentials/gcal_credentials.json`
4. On first run, a browser window will open for the OAuth consent flow — the token is saved automatically to `credentials/token.json`

### 4. Initialise the database

```bash
python -m src.core.db
```

This creates `db/learning.db`, seeds all topics from `config.yaml`, and prints them to confirm.

### 5. Register the Telegram webhook

Point Telegram to your server (requires a public HTTPS URL, e.g. via ngrok or a VPS):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-domain>/webhook&secret_token=<WEBHOOK_SECRET>"
```

---

## Running

```bash
python -m src.main
```

This starts both the FastAPI server (port 8000) and the APScheduler in a single async process.

```
INFO: Scheduled: Daily Briefing (Mon–Sat 08:00)  next_run=...
INFO: Scheduled: Weekly Planning (Sun 09:00)      next_run=...
INFO: Starting Kairos on 0.0.0.0:8000
```

### Health check

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

### Dry-run morning briefing (no Telegram send)

```bash
python -m src.agent.graph
```

---

## Telegram UX

### Morning briefing (08:00 daily)

```
☀️ Good morning Diego — Saturday April 4

📅 Your day:
  09:00 Team standup (30min)

🧠 Study windows:
  08:00–09:00 → Gen AI System Design (60min)
  11:00–13:00 → Data Structures and Algorithms (120min)

📌 SM-2 picks today:
  1. Gen AI System Design — due (EF: 2.5)
  2. Data Structures and Algorithms — due (EF: 2.5)
  3. Sales Engineering — due (EF: 2.5)

Confirm these study blocks? [Yes, book them] [Edit] [Skip]
```

### Duration Picker (any unrecognised message)

Tap any message → bot responds with `[30 min] [45 min] [60 min]`

### Session summary format

After an external study session, paste this into the chat:

```
Session summary
Topic: LangGraph
Duration: 45 min
Weak areas: state management, conditional edges
Suggestions: re-read checkpointer docs
```

The bot replies with rating buttons:

| Button | Score | SM-2 effect |
|---|---|---|
| 😕 Hard | 2 | Interval resets to 1 day |
| 😐 OK | 3 | Modest growth |
| 😊 Easy | 5 | Fast growth |

---

## Customising topics

Edit `config.yaml` and re-run `python -m src.core.db` to seed new topics. Existing topics are never overwritten (`INSERT OR IGNORE`).

```yaml
topics:
  - name: "Your Topic"
    tier: 1        # 1 = high priority, 2 = medium, 3 = background
    active: true
```

Focus windows and protected blocks are also configured in `config.yaml`:

```yaml
focus_windows:
  - start: "08:00"
    end: "11:00"

protected_blocks:
  - start: "15:00"
    end: "19:30"
```

---

## Tests

```bash
python -m pytest tests/ -v
```

19 tests covering SM-2 algorithm edge cases and gap finder logic. All pure Python — no API calls needed.

---

## Security notes

- `.env` and `credentials/` are gitignored and never committed
- Every webhook request is validated against `WEBHOOK_SECRET` (HTTP 403 on mismatch)
- The agent never modifies Google Calendar events it didn't create (`creator.self` check enforced at tool level)
- SQLite files are local only — never exposed via HTTP
