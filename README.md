# LARA

Personal Learning Assistant for Diego Sabajo. Tracks study topics using SM-2 spaced repetition, sends proactive daily plans via Telegram, reads Google Calendar to plan around your real schedule, generates focused study briefs via Claude, and books `[Study]` events on Google Calendar after confirmation.

---

## Features

- **SM-2 spaced repetition** — topics ranked by tier and easiness factor; intervals grow automatically based on session quality
- **Morning briefing** — sent daily via Telegram with your calendar, free windows, and assigned study blocks
- **On-demand study** — send `/study`, tap a duration, get an AI-generated brief for the highest-priority due topic
- **Done flow** — send `/done` after studying; LARA asks how each session went, prompts for weak areas, logs everything, and updates SM-2
- **Calendar safety** — reads all events to plan around them, only writes events it created (`[Study]` prefix, `creator.self == True`)
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
APScheduler ──► daily_planning (daily + Sunday variant)
```

### LangGraph nodes

| Node | Responsibility |
|---|---|
| `router` | Entry point — routes by trigger type |
| `daily_planning` | Assembles morning plan from calendar + SM-2 + gap finder |
| `on_demand` | Handles `/study` flow, picks highest-priority due topic |
| `done_parser` | Finds first unlogged slot, sends rating buttons |
| `log_session` | Logs session with quality score, prompts for weak areas |
| `log_weak_areas` | Saves weak areas or clears on Skip, prompts for next topic |
| `calendar_reader` | Read-only GCal fetch |
| `sm2_engine` | Returns due topics ranked by tier + easiness factor |
| `gap_finder` | Computes free windows respecting protected blocks |
| `generate_brief` | Calls Claude API — the only LLM call in the graph |
| `confirm` | Sends plan to Telegram with inline keyboard; waits for tap |
| `output` | Final Telegram send + GCal write after confirmation |

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
│   │   ├── nodes.py         # Node implementations + AgentState
│   │   └── tools.py         # LangGraph tools
│   ├── core/
│   │   ├── db.py            # Schema init, seed, connection helper
│   │   ├── sm2.py           # SM-2 algorithm (pure Python)
│   │   └── gap_finder.py    # Free window computation (pure Python)
│   └── integrations/
│       ├── gcal.py          # Google Calendar read + write
│       ├── telegram_client.py  # send_message / send_buttons / remove_buttons
│       └── claude_api.py    # generate_brief()
└── tests/
    ├── test_sm2.py
    ├── test_gap_finder.py
    └── test_tools.py
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

Creates `db/learning.db`, seeds topics from `config.yaml`, and prints them to confirm.

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

## Telegram UX

### Morning briefing

```
☀️ Good morning Diego — Tuesday April 7

📅 Your day:
  09:00 Team standup (30min)

🧠 Today's study plan:
  10:00–11:00 → Gen AI System Design (60min)
  11:00–12:00 → Data Structures and Algorithms (60min)

Confirm these study blocks?
[Yes, book them] [Skip]
```

### On-demand study

Send `/study` → tap duration → receive AI-generated study brief

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

Edit `config.yaml` and re-run `python -m src.core.db` to seed new topics. Existing topics are never overwritten (`INSERT OR IGNORE`).

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
  - start: "22:00"
    end: "23:00"
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
- Agent never modifies GCal events it didn't create (`creator.self` check at tool level)
- SQLite files are local only — never exposed via HTTP