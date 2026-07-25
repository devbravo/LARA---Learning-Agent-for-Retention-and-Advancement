# LARA

Personal Learning Assistant for Diego Sabajo.

LARA is two things bolted onto one Telegram bot:

1. **A study system** — tracks topics using SM-2 spaced repetition, sends proactive
   daily plans via Telegram, reads Google Calendar to plan around your real schedule,
   generates focused study briefs via Claude, books `[Mock]` mock-interview events on
   Google Calendar after confirmation, and runs a `/discuss` flow that assesses whether
   a topic is ready to graduate out of active review.
2. **A knowledge graph pipeline** — on `/prepare <topic>`, searches your configured
   blogs for the topic, synthesizes a dense "Concept Note" via Claude, and — once you
   approve it in Telegram — writes it into a Neo4j concept graph plus SQLite. It's a
   separate LangGraph graph from the study system today; the intent is for the graph
   to eventually enrich study briefs, but that link doesn't exist yet.

Both features share the same Telegram bot, the same SQLite database file, and the same
FastAPI process, but run as two independent LangGraph graphs with independent
checkpointers.

---

## Features

### Study system
- **SM-2 spaced repetition** — topics ranked by tier and easiness factor; intervals grow automatically based on session quality
- **Morning briefing** — sent daily via Telegram with your calendar, free windows, and assigned study blocks
- **On-demand study** — send `/study` to generate a brief for the highest-priority due topic (defaults to 30 min unless a duration callback is provided)
- **Done flow** — send `/done` after studying; LARA asks how each session went, prompts for weak areas (topic-type-aware, two-question breakdown), logs everything, and updates SM-2
- **In-progress graduation flow** — send `/activate`, pick an in-progress topic, and promote it to active with first review scheduled for tomorrow
- **Discuss / mock-readiness flow** — send `/discuss`; an external Claude session (via the MCP server) runs a live mock/discussion, then reports back a quality score and any repeated weak areas, and LARA decides whether the topic is ready to graduate, needs to go back to study, or isn't ready yet
- **Calendar safety** — reads all events to plan around them and only creates new tagged `[Mock]` and `[Study]` events; it does not modify unrelated calendar entries
- **Protected block** — never sends messages or fires jobs during configured protected hours

### Knowledge graph pipeline
- **JIT article ingestion** — `/prepare <topic>` searches configured blog sources (Jina AI Search), reranks results (Voyage AI), and filters out low-quality extractions deterministically (no LLM)
- **Map-reduce synthesis** — Claude Haiku distills each article in parallel (failed or junk distillations are skipped, not fatal), Claude Sonnet reduces across distillations plus any prior concept notes into one Concept Note, then Claude Haiku extracts concepts and relationships from the finished note (guaranteeing they're grounded in what the note actually says)
- **Human-in-the-loop approval** — LARA sends a preview to Telegram with Keep/Discard buttons before writing anything
- **Atomic dual-write** — on approval, writes a `concept_notes` row in SQLite and a `Concept`/`ConceptNote` subgraph in Neo4j in one logical transaction (SQLite commits only after the Neo4j transaction commits)
- **MCP server** — exposes topic/session/discuss context as tools so an external Claude session can read and write LARA's data during a live `/discuss` session

---

## Architecture

### Study system graph

```
Telegram ──► FastAPI /webhook ──► dispatcher.py ──► LangGraph graph (agent) ──► Telegram
                                        │                   │
                                has pending interrupt?      │
                                   yes → Command(resume)    │
                                   no  → fresh invoke       │
                                        │                   │
                     ┌──────────────────┼──────────────────┐
                     │                  │                   │
                Google Calendar      SQLite            Claude API
                (read + write)    (SM-2 state,       (study briefs
                                   sessions log,       only)
                                   checkpoints)
APScheduler ──► daily_planning (Mon–Fri morning + evening preview) / weekend_brief (Sat–Sun)
```

### Knowledge graph pipeline

```
Telegram /prepare <topic> ──► handler.py (bypasses dispatcher) ──► LangGraph graph (knowledge)
                                                                          │
                                                       synthesize_node ───┤
                                                       (Jina search, Voyage rerank,   │
                                                        Claude map-reduce synthesis)  │
                                                                          │
                                                       prepare_preview_node
                                                       (Telegram Keep/Discard buttons)
                                                                          │
                                                       prepare_confirm_node — interrupt()
                                                                          │
                                              approved ──► write_node ──► SQLite + Neo4j
                                              rejected ──► END
```

The knowledge graph is a **separate LangGraph `StateGraph`** with its own `SqliteSaver`
checkpointer, using thread ids namespaced `kg_{chat_id}` (vs. the study graph's bare
`{chat_id}`) in the same `db/state.db` file, so the two graphs never collide. In
`src/api/telegram/handler.py`, any `/prepare` command or `kg_`-prefixed callback is
routed straight to the knowledge graph, entirely bypassing `dispatcher.py` and the
study-system's intent parser.

An external Claude session (e.g. during a live `/discuss` mock) talks to LARA over the
MCP server mounted at `/` in `src/api/app.py`, exposing `get_topic_context`,
`log_session`, `get_discuss_context`, and `assess_discuss_readiness` as tools.

### Study system nodes

| Node | Responsibility |
|---|---|
| `router` | Entry point — routes by trigger type (9 targets: `daily`, `evening`, `weekend`, `study`, `done`, `pick`, `activate`, `discuss`, `discuss_ready_confirm`) |
| `daily_planning` | Assembles morning/evening plan from calendar + SM-2 + gap finder; sends booking buttons |
| `await_daily_confirmation` | interrupt() — waits for user to confirm or skip the daily booking proposal |
| `weekend_brief` | Sat/Sun brief — shows due topics with overdue indicators; no weak areas displayed |
| `send_duration_picker` | Sends duration buttons; cleans up stale picker |
| `on_demand` | interrupt() — picks highest-priority due topic for requested duration |
| `generate_brief` | Calls Claude API — the only LLM call in this graph; sends booking buttons when a slot is available |
| `await_brief_confirmation` | interrupt() — waits for user to confirm or skip the on-demand booking proposal |
| `book_events` | Writes `[Mock]` GCal events after user confirmation |
| `done_parser` | Queries active unlogged topics from DB. 0 → message. 1 → rating buttons. 2+ → topic picker |
| `select_done_topic` | interrupt() — receives selected topic name, sends rating buttons, routes to log_session |
| `log_session` | interrupt() — logs session with quality score; sends weak areas prompt |
| `log_weak_areas` | interrupt() — saves first weak-areas answer or clears on Skip; routes to `log_weak_areas_q2` for topic types with a second question |
| `log_weak_areas_q2` | interrupt() — saves second weak-areas answer (topic-type-aware breakdown); ends with remaining unlogged list or all-done message |
| `study_topic` | Starts `/pick` flow, sends category inline buttons, cleans up stale lists |
| `study_topic_category` | interrupt() — handles category tap, sends matching subtopic inline buttons |
| `study_topic_confirm` | interrupt() — marks selected topic as `in_progress`, notifies user |
| `activate_topic` | Lists in-progress topics as inline buttons |
| `graduate_topic` | interrupt() — graduates selected topic to active SM-2 with first review tomorrow |
| `confirm_graduate` | interrupt() — confirms graduation after a `/discuss` readiness check reports "ready" |
| `discuss_parser` | Entry point for `/discuss` — resolves the topic and hands off to `start_discuss` |
| `start_discuss` | Sends discuss-mode context/instructions so the user can start a live session with an external Claude session |
| `notify_discuss_ready` | Triggered programmatically by `discuss_service.assess_discuss_readiness()` when a topic reaches "ready"; sends graduation confirmation buttons |
| `await_discuss_activation` | interrupt() — waits for confirm/skip on the discuss-triggered graduation proposal |
| `output` | Sends `state["messages"][-1]` via Telegram — shared terminal node |

### Knowledge graph nodes

| Node | Responsibility |
|---|---|
| `synthesize_node` | Searches blogs (Jina), reranks (Voyage), filters invalid extractions, pulls prior concept notes, synthesizes via Claude map-reduce. Routes to END if no usable articles found |
| `prepare_preview_node` | Formats the concept note and sends Telegram Keep/Discard buttons |
| `prepare_confirm_node` | interrupt() — first statement, per the HITL rule. Resumes with `kg_approve`/`kg_reject` |
| `write_node` | Only reached on approval — writes to SQLite (`concept_notes`) and Neo4j (`Concept`/`ConceptNote` + relationships) in one logical transaction |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent framework | LangGraph (two independent graphs: study system + knowledge pipeline) |
| Web server | FastAPI + uvicorn |
| Scheduler | APScheduler (AsyncIO) |
| LLM | Anthropic Claude (via `anthropic` SDK / `langchain-anthropic`) — Sonnet for briefs and concept-note synthesis, Haiku for per-article map-step distillation |
| Embeddings / reranking | Voyage AI (`voyageai`) — `voyage-4-lite` embeddings, `rerank-2` reranking |
| Article search | Jina AI Search (`s.jina.ai`) — called via raw HTTP (`requests`), no dedicated pip package |
| Knowledge graph | Neo4j AuraDB (`neo4j` driver) |
| MCP | `mcp[cli]` (LARA's own MCP server) + `neo4j-mcp-server` (Claude Code integration) |
| Messaging | python-telegram-bot |
| Calendar | Google Calendar API v3 (OAuth2) |
| Database | SQLite (via `langgraph-checkpoint-sqlite` for graph checkpoints; plain `sqlite3` for app data) |
| Config | YAML + python-dotenv |

> **Known gap:** `requests` is imported directly by `src/knowledge/search.py` and
> `src/knowledge/extract.py` but isn't listed in `requirements.txt` — it currently
> installs only as a transitive dependency of another package. Pin it explicitly if
> you hit an `ImportError` on a clean install.

---

## Project Structure

```
lara/
├── config.yaml              # Schedule, focus windows, protected blocks
├── topics.yaml               # Study topic catalog (tier/status/default duration)
├── requirements.txt
├── .env.example
├── pytest.ini
├── db/                       # SQLite files (gitignored)
├── credentials/              # GCal OAuth credentials (gitignored)
├── migrations/
│   └── migrate_add_articles_notes.py   # Creates `sources` + `concept_notes` tables
├── src/
│   ├── main.py                # Entry point — starts FastAPI + scheduler
│   ├── server.py               # Backwards compat re-export: from src.api.app import app
│   ├── compat/
│   │   └── langgraph_sqlite_shim.py    # In-memory SqliteSaver-compatible shim — currently UNUSED (not imported anywhere; both graphs use the real langgraph-checkpoint-sqlite package)
│   ├── models/
│   │   └── telegram.py         # Pydantic models for Telegram webhook payloads
│   ├── agent/                  # Study system graph
│   │   ├── graph.py                   # LangGraph graph + SqliteSaver checkpointer
│   │   ├── nodes.py                   # Node orchestration + AgentState
│   │   ├── routes.py                  # Conditional-edge routing functions
│   │   ├── state.py                   # AgentState TypedDict
│   │   ├── messages.py                # Pure (text, buttons) builders for every flow — no I/O
│   │   ├── plan_message.py            # Daily/evening plan message section builders
│   │   ├── slot_builders.py           # Study-event matching + rebooking helpers
│   │   ├── weak_areas_parser.py       # Weak-areas parsing + topic-type vocabularies
│   │   ├── formatting.py              # Shared time/date formatting helpers
│   │   └── tools.py                   # LangGraph tools
│   ├── knowledge/               # Knowledge graph pipeline (separate LangGraph graph)
│   │   ├── graph.py                   # Compiled StateGraph(KGState) + SqliteSaver, thread id `kg_{chat_id}`
│   │   ├── nodes.py                   # synthesize_node, prepare_preview_node, prepare_confirm_node, write_node
│   │   ├── state.py                   # KGState TypedDict
│   │   ├── clients.py                 # KnowledgeClients — Anthropic/Voyage/Jina/Neo4j client setup
│   │   ├── search.py                  # Jina search + Voyage rerank; dev runner: python -m src.knowledge.search "<topic>"
│   │   ├── extract.py                 # Deterministic extraction validation (no LLM); extract_clean_text/extract_top_results are deprecated (Jina search now returns full content)
│   │   ├── lookup.py                  # Finds prior concept notes via embedding similarity (read-only)
│   │   ├── resolve.py                 # Matches candidate concept names to existing Neo4j Concept nodes
│   │   ├── synthesize.py              # Claude map-reduce concept note synthesis; dev runner: python -m src.knowledge.synthesize "<topic>"
│   │   └── write.py                   # Atomic SQLite + Neo4j write on approval
│   ├── api/
│   │   ├── app.py              # FastAPI app factory + lifespan; mounts the MCP app at "/"
│   │   ├── routes/
│   │   │   ├── health.py              # GET /health
│   │   │   ├── webhook.py             # POST /webhook (auth + parse)
│   │   │   ├── scheduler_status.py    # GET /scheduler-status
│   │   │   └── mcp.py                 # MCP server: get_topic_context, log_session, get_discuss_context, assess_discuss_readiness
│   │   └── telegram/
│   │       ├── handler.py             # handle_update() — routes /prepare and kg_* callbacks to the knowledge graph, everything else to dispatcher
│   │       ├── intent_parser.py       # Intent dataclass; parse_callback / parse_message
│   │       ├── callback_handlers.py   # one function per callback type
│   │       ├── message_handlers.py    # one function per command
│   │       ├── types.py
│   │       └── dispatcher.py          # dedup sets, idempotency lock, invoke_safe() — study graph only
│   ├── core/
│   │   ├── sm2.py              # SM-2 algorithm (pure Python)
│   │   └── gap_finder.py       # Free window computation (pure Python)
│   ├── infrastructure/
│   │   ├── db.py               # Schema init, seed, connection helper
│   │   ├── scheduler.py        # APScheduler jobs (weekday, weekend, evening)
│   │   └── time.py             # Local timezone helpers
│   ├── integrations/
│   │   ├── gcal.py             # Google Calendar read + write
│   │   ├── telegram_client.py  # send_message / send_buttons / remove_buttons
│   │   └── claude_api.py       # generate_brief()
│   ├── repositories/
│   │   ├── session_repository.py
│   │   ├── sm2_repository.py
│   │   └── topic_repository.py
│   └── services/
│       ├── topic_service.py    # graduate_topic(), get_in_progress_topics()
│       ├── discuss_service.py  # get_discuss_context(), assess_discuss_readiness() — backs the MCP discuss tools
│       └── view_service.py
└── tests/
    ├── conftest.py
    ├── test_sm2.py
    ├── test_gap_finder.py
    ├── test_tools.py
    ├── test_study_topic.py
    ├── test_done_flow.py
    ├── test_graduate_topic.py
    ├── test_discuss_flow.py
    ├── test_discuss_service.py
    ├── test_kg_graph.py
    ├── test_nodes_daily_planning.py
    ├── test_nodes_weekend_brief.py
    ├── test_output_refactor.py
    ├── test_repositories.py
    ├── test_topic_service.py
    ├── test_view_service.py
    ├── test_dispatcher.py
    ├── test_telegram_client.py
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

Edit `.env` — required for the study system:

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

Additionally required for the knowledge graph pipeline — see steps 5 and 6 below for
where these come from:

```env
NEO4J_URI=
NEO4J_USERNAME=
NEO4J_PASSWORD=
NEO4J_DATABASE=
AURA_INSTANCEID=
AURA_INSTANCENAME=lara-knowledge-graph
VOYAGE_API_KEY=
JINA_API_KEY=
```

`src/knowledge/clients.py` raises `EnvironmentError` at startup of any knowledge-graph
node if any of `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `JINA_API_KEY`, `NEO4J_URI`,
`NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` is missing — you only need these if
you're working on or exercising `/prepare`.

### 3. Google Calendar credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Desktop app)
3. Download the JSON and save to `credentials/gcal_credentials.json`
4. On first run, a browser window opens for OAuth consent — token saved to `credentials/token.json`

### 4. Initialise the database

```bash
python -m src.infrastructure.db
```

Creates `db/learning.db`, seeds topics from `topics.yaml`, and prints them to confirm.

Also reset and reseed with:
```bash
rm db/learning.db && python -m src.infrastructure.db
```

Then run the knowledge-graph migration to add the `sources` and `concept_notes` tables:

```bash
python migrations/migrate_add_articles_notes.py
```

> **Known gap:** there's no seed script for the `sources` table (the blog list
> `search_blogs_for_topic()` reads from). You'll need to `INSERT` your blog sources by
> hand, e.g.:
> ```sql
> sqlite3 db/learning.db "INSERT INTO sources (name, url) VALUES ('Example Blog', 'https://example.com')"
> ```

### 5. Neo4j knowledge graph (AuraDB)

LARA uses a Neo4j AuraDB instance to store a concept graph: articles and notes link to
`Concept` nodes, which are intended to eventually connect to `topics` for enriched
study briefs (not implemented yet — the two systems are independent today).

1. Provision a free-tier instance at the [AuraDB console](https://console.neo4j.io/)
2. Copy `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`,
   `AURA_INSTANCEID`, and `AURA_INSTANCENAME` into `.env`

**Apply the schema constraints** (idempotent, safe to re-run):

```bash
python migrations/migrate_neo4j_constraints.py
```

Or run the Cypher directly:

```cypher
CREATE CONSTRAINT concept_name_unique IF NOT EXISTS FOR (c:Concept) REQUIRE c.name IS UNIQUE;
CREATE CONSTRAINT article_id_unique   IF NOT EXISTS FOR (a:Article) REQUIRE a.id   IS UNIQUE;
CREATE CONSTRAINT note_id_unique      IF NOT EXISTS FOR (n:Note)    REQUIRE n.id   IS UNIQUE;
```

### 6. Voyage AI + Jina AI (knowledge graph pipeline)

- **Voyage AI** — used for embeddings (`voyage-4-lite`) and reranking (`rerank-2`).
  Get a key at [voyageai.com](https://www.voyageai.com/) and set `VOYAGE_API_KEY`.
- **Jina AI** — used for article search (`s.jina.ai`). Get a key at
  [jina.ai](https://jina.ai/) and set `JINA_API_KEY`. There's no pip package for this —
  it's called via raw `requests` HTTP calls.

You can exercise the pipeline piece by piece without touching Telegram:

```bash
python -m src.knowledge.search "<topic>"       # search + rerank only
python -m src.knowledge.synthesize "<topic>"   # full search → synthesis, prints the concept note
python -m src.knowledge.dev_trigger "<topic>"  # sends the real Telegram preview (needs the server running to handle the button tap)
```

### 7. MCP server (Claude Code integration + LARA's own MCP tools)

The repo includes `.mcp.json` which configures the [neo4j-mcp-server](https://github.com/neo4j/mcp) so Claude Code can query and write the graph directly via `write-cypher` / `read-cypher` / `get-schema` tools.

The MCP server reads credentials from environment variables — no credentials live in `.mcp.json`. Credentials are kept in `.claude/settings.local.json` (gitignored). To set up on a new machine:

1. Copy your credentials into `.claude/settings.local.json` under the `env` key:

```json
{
  "env": {
    "NEO4J_URI": "neo4j+s://...",
    "NEO4J_USERNAME": "...",
    "NEO4J_PASSWORD": "...",
    "NEO4J_DATABASE": "..."
  }
}
```

2. Restart Claude Code — the `neo4j` MCP server loads automatically (`.mcp.json` is already approved via `enableAllProjectMcpServers: true` in `.claude/settings.json`).

To point at a different Neo4j instance, change only the four `NEO4J_*` values in `.claude/settings.local.json` and restart.

Separately, **LARA's own MCP server** (`src/api/routes/mcp.py`) is mounted at `/` in the
FastAPI app and exposes `get_topic_context`, `log_session`, `get_discuss_context`, and
`assess_discuss_readiness` — this is what an external Claude session calls into during
a live `/discuss` mock session. It runs automatically with the app; no separate setup.

### 8. Register the Telegram webhook

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
python -m src.infrastructure.db
```
## Reseed after topic changes:
```commandline
python -m src.infrastructure.db
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

Send `/done` after each session. Each `/done` call logs one topic.

**Single unlogged topic** — goes straight to rating:
```
LARA: How did Gen AI System Design go?
      [😕 Hard] [😐 OK] [😊 Easy]

[tap 😐 OK]

LARA: Any weak areas to note? Reply with text or tap Skip.
      [Skip]

You: Trade-offs in vector DB selection

LARA: ✅ Gen AI System Design logged. All done for today! 💪
```

**Multiple unlogged topics** — shows picker first:
```
LARA: Which session are you logging?
      [Gen AI System Design] [Data Structures and Algorithms]

[tap Gen AI System Design]

LARA: How did Gen AI System Design go?
      [😕 Hard] [😐 OK] [😊 Easy]

[tap 😐 OK]

LARA: Any weak areas to note? Reply with text or tap Skip.
      [Skip]

LARA: ✅ Gen AI System Design logged. Still unlogged: Data Structures and Algorithms. Press /done when you're ready.
```

| Button | Score | SM-2 effect |
|---|---|---|
| 😕 Hard | 2 | Interval resets to 1 day |
| 😐 OK | 3 | Modest growth |
| 😊 Easy | 5 | Fast growth |

### Discuss / mock-readiness flow

Send `/discuss` to start a live mock/discussion session for an in-progress topic. An
external Claude session (connected via the MCP server) runs the session, then reports
back a quality score and any weak areas via `assess_discuss_readiness`. LARA applies a
readiness rubric: a repeated weak area across recent sessions routes the topic back to
study; no repeats plus a high quality score marks it ready and sends a graduation
confirmation; otherwise it's not ready yet and stays in the discuss loop.

```
LARA: Discuss mode started for Gen AI System Design. Chat with your reviewer, then wrap up when done.

[... external Claude session runs the mock ...]

LARA: Nice work — no repeated weak areas and strong quality this time.
      Ready to graduate Gen AI System Design to active review?
      [Yes, graduate it] [Not yet]
```

### JIT article ingestion

Send `/prepare <topic>` to search your configured blogs and synthesize a concept note.

```
You: /prepare vector database indexing

LARA: Searching your sources for "vector database indexing"...
LARA: Synthesizing concept note from 5 articles...

LARA: 📝 Vector Database Indexing
      HNSW trades recall for speed via approximate graph traversal...
      [full synthesized note]

      Keep this note?
      [✅ Keep] [❌ Discard]

[tap ✅ Keep]

LARA: Saved — 6 concepts linked, 4 new relationships written to the graph.
```

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

Study system tests are pure Python — no API calls needed. Knowledge graph tests
(`test_kg_graph.py`) mock the LLM/Jina/Voyage/Neo4j calls and exercise the graph
orchestration (interrupt/resume, approval → write, rejection → no write, synthesis
failure → END) rather than hitting real external services.

---

## Security

- `.env` and `credentials/` are gitignored and never committed
- Every webhook request validated against `WEBHOOK_SECRET` (HTTP 403 on mismatch)
- Calendar write path creates new `[Mock]` events only; existing events are not modified
- SQLite files are local only — never exposed via HTTP
