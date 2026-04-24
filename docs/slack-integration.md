# AegisDB Slack Bot Integration — Technical Documentation

> **Last updated:** April 24, 2026  
> **Status:** Production-ready, fully tested  
> **Author:** Session documentation — AegisDB Hackathon

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [File Structure](#file-structure)
4. [Component Deep Dive](#component-deep-dive)
5. [Data Flow](#data-flow)
6. [Slack App Configuration](#slack-app-configuration)
7. [Environment Variables](#environment-variables)
8. [Boot Sequence](#boot-sequence)
9. [API Endpoints Used](#api-endpoints-used)
10. [Redis Streams](#redis-streams)
11. [ChromaDB Collections](#chromadb-collections)
12. [Known Bugs Fixed During Session](#known-bugs-fixed-during-session)
13. [Known Limitations](#known-limitations)
14. [Demo Sequence](#demo-sequence)
15. [Future Improvements](#future-improvements)

---

## Overview

The AegisDB Slack bot is a **conversational database reliability engineer** embedded in Slack. It is not a notification bot — it is a full interactive agent that:

- Posts live anomaly cards when data quality failures are detected
- Self-updates cards as the pipeline progresses (detecting → proposal → resolved)
- Answers questions in proposal threads using Groq LLM with full context
- Stores rejection reasons in ChromaDB and synthesises them on demand
- Learns from engineer decisions over time

The bot runs as a **completely separate process** from the FastAPI backend, connected via Redis Streams and Socket Mode (no public URL required).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Backend                          │
│                     (src/ — port 8001)                          │
│                                                                 │
│  Webhook → Detector → Diagnosis (Groq) → Sandbox → Proposal    │
│                                              │                  │
│                                    slack_notifier.py            │
│                                    XADD aegisdb:slack           │
└─────────────────────────────────────────────────────────────────┘
                              │
                         Redis Streams
                         aegisdb:slack
                              │
┌─────────────────────────────────────────────────────────────────┐
│                      Slack Bot Process                          │
│                  (slack_bot/ — python -m slack_bot.app)         │
│                                                                 │
│  stream_listener.py ──── XREADGROUP ──── posts/updates cards   │
│                                                                 │
│  app.py (Bolt) ──── Socket Mode ──── button/modal/cmd handlers │
│                                                                 │
│  qa_engine.py ──── Groq API ──── thread Q&A                    │
│                                                                 │
│  rejection_store.py ──── ChromaDB ──── aegisdb_rejections      │
└─────────────────────────────────────────────────────────────────┘
```

### Key Architectural Decision

The bot uses **Socket Mode** — no public URL needed. Slack sends events via a persistent WebSocket. This is ideal for local development and hackathons.

The bot is a **separate OS process** from FastAPI. They share:
- The same Redis instance (different stream: `aegisdb:slack`)
- The same ChromaDB on-disk store (`./data/chromadb`)
- The same `.env` file (bot reads `GROQ_API_KEY`, `SLACK_BOT_TOKEN`, etc.)

They do NOT share:
- Python process memory
- FastAPI's in-memory state
- The `proposal_message_map` dict (bot-only, in-memory, resets on restart)

---

## File Structure

```
slack_bot/
├── __init__.py              # Package marker — required for python -m slack_bot.app
├── config.py                # SlackSettings — reads .env via pydantic-settings
├── blocks.py                # All Slack Block Kit card builders
├── slack_notifier.py        # Redis publisher — called by FastAPI after proposal save
├── stream_listener.py       # Redis consumer — posts/updates Slack cards
├── rejection_store.py       # ChromaDB wrapper for aegisdb_rejections collection
├── qa_engine.py             # Context assembly + Groq calls for Q&A
└── app.py                   # Bolt app — all handlers, all commands, boot
```

Backend files touched (minimal):
```
src/core/config.py                  # Added 4 Slack fields
src/services/stream_consumer.py     # _save_proposal() calls slack_notifier
src/main.py                         # Boot/shutdown slack_notifier
```

---

## Component Deep Dive

### `config.py` — SlackSettings

```python
class SlackSettings(BaseSettings):
    slack_bot_token: str       # xoxb- token
    slack_app_token: str       # xapp- token (Socket Mode)
    slack_ops_channel: str     # e.g. "aegis-ops"
    aegisdb_base_url: str      # e.g. "http://localhost:8001"
    redis_host: str
    redis_port: int
    groq_api_key: str          # Plain str (not SecretStr — bot process doesn't need it)
    chroma_persist_dir: str    # Must match backend's CHROMA_PERSIST_DIR
```

**Important:** `groq_api_key` is plain `str` here unlike the backend which uses `SecretStr`. Do not change this — the bot passes it directly to `AsyncGroq()`.

---

### `blocks.py` — Card Builders

Four public functions:

| Function | Purpose | When called |
|---|---|---|
| `detecting_card()` | Initial "anomaly detected" placeholder | Immediately on stream message |
| `proposal_card()` | Full proposal with diff, buttons | After API fetch completes |
| `resolved_card()` | Final state — approved/rejected/failed | After approve/reject/poll |
| `rejection_modal()` | Slack modal for rejection reason | When Reject button clicked |

**V1-V4 visual improvements:**

- **V1** — `_build_diff_section()`: scans `sample_before` for NULL columns, shows only changed column. Does NOT compare before vs after by row index (broken — sandbox returns rows in different order). Instead scans for `None` values in before sample.
- **V2** — `_header_style()`: maps `(failure_categories, confidence, sandbox_passed)` to emoji + label. Priority: sandbox failed → ⚠️, critical+90% → 🔴, 85%+ → 🟠, 70%+ → 🟡.
- **V3** — `resolved_card()`: receipt-style with `fields` grid for all 3 outcomes.
- **V4** — `_cmd_status()` in `app.py`: 4-block dashboard grid with stream depths.

**Critical constraint:** `proposal_card()` has `sample_before` and `sample_after` as optional parameters defaulting to `[]`. Old call sites work without them.

---

### `slack_notifier.py` — Redis Bridge

Called by `src/services/stream_consumer._save_proposal()` after a proposal is created. Publishes to `aegisdb:slack` stream.

**Integration point in backend:**

```python
# src/services/stream_consumer.py — _save_proposal()
from slack_bot.slack_notifier import slack_notifier
await slack_notifier.notify_proposal(proposal)
```

Fields published to Redis:
- `proposal_id`, `table_name`, `table_fqn`
- `failure_categories` (comma-separated string)
- `confidence`, `rows_affected`
- `event_type: "new_proposal"`

---

### `stream_listener.py` — Redis Consumer

Consumer group: `slack-bot`  
Stream: `aegisdb:slack`  
Consumer name: `slack-bot-listener-1`

**`_handle_new_proposal()` flow:**

```
1. Post detecting_card immediately (visual anchor)
2. Store minimal entry in proposal_message_map
3. Fetch full proposal from GET /api/v1/proposals/{id}
4. Update proposal_message_map with real event_id
5. Parse sample_before/sample_after (defensive JSON decode)
6. Update card to proposal_card with diff table
7. DM table owner (if mapped in config.table_owner_map)
```

**Critical bug fixed:** `proposal` variable must be initialized to `None` before the `try` block in `_fetch_proposal`. Otherwise Python raises `UnboundLocalError` if the API call fails.

```python
# CORRECT — always initialize before try
proposal = None
try:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        proposal = resp.json()
except Exception as e:
    logger.error(...)
```

**`proposal_message_map`** — module-level dict shared between `stream_listener.py` and `app.py`. Structure:

```python
{
    "proposal-uuid": {
        "ts":         "1777041557.798069",  # Slack message timestamp
        "channel":    "C0AUGFL07TR",
        "table_name": "orders",
        "table_fqn":  "northwind_svc.northwind.public.orders",
        "event_id":   "d9ae0c52-...",
    }
}
```

**This dict resets on bot restart.** Old cards' buttons still work (Slack sends action payloads with `value=proposal_id`) but thread Q&A won't work for pre-restart cards because the `ts` mapping is gone.

---

### `rejection_store.py` — ChromaDB Learning Loop

Collection: `aegisdb_rejections`  
Model: `all-MiniLM-L6-v2` (same as `aegisdb_fixes`)  
Path: same `PersistentClient` as backend (`./data/chromadb`)

**All methods are synchronous.** Always call via `loop.run_in_executor()` from async handlers.

```python
loop = asyncio.get_running_loop()
await loop.run_in_executor(None, lambda: rejection_store.store_rejection(...))
```

Null-safety: `find_rejections_for_table()` returns `[]` if collection is `None` or empty — safe to call before `connect()`.

Document format embedded (mirrors `vector_store._build_document()`):
```
Table: {table_fqn}
Failure: {categories}
Rejected fix: {fix_description}
Fix SQL: {fix_sql}
Rejection reason: {rejection_reason}
Alternative suggested: {alternative}
```

---

### `qa_engine.py` — Groq Q&A Engine

Three public entry points:

| Function | Used by | Context assembled |
|---|---|---|
| `answer_question(question, proposal_id)` | Thread message handler | Proposal detail + similar fixes + rejections + profiling trend |
| `answer_global_question(question)` | `/aegis ask` | Last 10 audit entries + pending proposals |
| `answer_why_table(table_name)` | `/aegis why` | Rejection history synthesis |

**Context assembly runs concurrently:**

```python
profiling_summary, similar_fixes, rejections = await asyncio.gather(
    _fetch_profiling_trend(table_name),
    loop.run_in_executor(None, partial(_sync_similar_fixes, proposal)),
    loop.run_in_executor(None, partial(_sync_rejections, table_fqn, category)),
)
```

**Known limitation:** `_sync_similar_fixes` imports `from src.db.vector_store import vector_store` at call time. If the bot process doesn't have `vector_store` initialized (it won't unless run from project root with correct PYTHONPATH), this fails with `NoneType has no attribute 'count'`. Non-fatal — answer still works using rejection history.

**Groq model:** `llama-3.3-70b-versatile`  
**Max tokens:** 512 for Q&A, 400 for `/aegis why` synthesis  
**Temperature:** 0.2 (slightly higher than diagnosis's 0.1 for natural language)

---

### `app.py` — Bolt Application

**Handler registry:**

| Handler | Trigger | Action |
|---|---|---|
| `handle_approve` | Button `approve_proposal` | POST /proposals/{id}/approve → update card → poll audit |
| `handle_reject_button` | Button `reject_proposal` | Open rejection modal |
| `handle_rejection_submit` | Modal `rejection_modal_submit` | POST reject API + store ChromaDB + update card |
| `handle_thread_message` | `@app.event("message")` | Thread Q&A via qa_engine |
| `handle_aegis_command` | `/aegis` | Dispatch to cmd functions |
| `handle_quick_proposals` | Button `quick_proposals` | Inline proposals list |
| `handle_quick_audit` | Button `quick_audit` | Inline audit list |

**Post-approve polling (`_poll_for_completion`):**

Runs as `asyncio.create_task()` — non-blocking. Polls `GET /audit?limit=10` every 3 seconds, max 12 attempts (36 seconds total). Matches by `event_id`. Updates card to `resolved_card(outcome="completed")` when found.

**Subtype handlers** — required to prevent Bolt from silently dropping message events:

```python
@app.event({"type": "message", "subtype": "bot_message"})
async def handle_bot_message(ack): await ack()

@app.event({"type": "message", "subtype": "message_changed"})
async def handle_message_changed(ack): await ack()

@app.event({"type": "message", "subtype": "message_deleted"})
async def handle_message_deleted(ack): await ack()
```

**Critical:** `handle_thread_message` must NOT have `ack` in its signature. Message events don't use ack — adding it causes Bolt to fail silently.

---

## Data Flow

### Full Happy Path

```
1. Dirty data exists in Northwind PostgreSQL
2. curl POST /webhook/om-test-failure
3. FastAPI: Detector → Diagnosis (Groq) → Sandbox (testcontainers)
4. FastAPI: create_proposal() → slack_notifier.notify_proposal()
5. slack_notifier: XADD aegisdb:slack
6. stream_listener: XREADGROUP → _handle_new_proposal()
7. Slack: detecting_card posted to #aegis-ops
8. stream_listener: GET /proposals/{id} → proposal_card posted
9. Engineer: replies in thread → handle_thread_message fires
10. qa_engine: assembles context → Groq → answer posted in thread
11. Engineer: clicks Approve → handle_approve fires
12. app.py: POST /proposals/{id}/approve → card → "Executing..."
13. _poll_for_completion: GET /audit every 3s → finds entry → card → receipt
```

### Rejection + Learning Loop

```
1. Engineer clicks Reject → modal opens
2. Engineer submits reason + alternative
3. handle_rejection_submit:
   a. POST /proposals/{id}/reject
   b. Update card to rejected receipt
   c. rejection_store.store_rejection() → ChromaDB
4. Engineer: /aegis why orders
5. answer_why_table(): rejection_store.find_rejections_for_table()
6. Groq synthesises rejection history → posted in channel
```

---

## Slack App Configuration

### Bot Token Scopes (`xoxb-`)

| Scope | Purpose |
|---|---|
| `chat:write` | Post and update messages |
| `chat:write.public` | Post to channels without being invited |
| `commands` | Register `/aegis` slash command |
| `channels:history` | Read messages for thread Q&A |
| `groups:history` | Read messages in private channels |
| `im:write` | Send DMs to table owners |
| `users:read` | Resolve user IDs to names |

### App-Level Token Scopes (`xapp-`)

| Scope | Purpose |
|---|---|
| `connections:write` | Socket Mode persistent WebSocket |

### Event Subscriptions

| Event | Required for |
|---|---|
| `message.channels` | Thread Q&A in public channels |
| `message.groups` | Thread Q&A in private channels |

### Critical: Bot Must Be Invited to Channel

`chat:write.public` allows posting without joining, but **message events are only delivered to channels the bot has joined**. Thread Q&A will not work until:

```
/invite @YourBotName
```

is run in the target channel.

### Slash Command

- Command: `/aegis`
- Request URL: blank (Socket Mode)
- Subcommands: `status | proposals | audit [n] | ask [question] | why [table] | help`

---

## Environment Variables

Add these to `.env` (alongside existing backend vars):

```env
# Slack Bot
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_OPS_CHANNEL=aegis-ops

# These are already in .env — bot reads same file
GROQ_API_KEY=gsk_...
REDIS_HOST=localhost
REDIS_PORT=6379
CHROMA_PERSIST_DIR=./data/chromadb
AEGISDB_BASE_URL=http://localhost:8001
```

---

## Boot Sequence

```
python -m slack_bot.app
│
├── stream_listener.connect()
│     ├── Redis ping
│     ├── XGROUP CREATE aegisdb:slack slack-bot (idempotent)
│     └── AsyncWebClient initialized
│
├── rejection_store.connect()  [via run_in_executor — sync]
│     ├── SentenceTransformer load (all-MiniLM-L6-v2)
│     ├── chromadb.PersistentClient(chroma_persist_dir)
│     └── get_or_create_collection(aegisdb_rejections)
│
└── asyncio.gather(
      stream_listener.start(),    # Redis consumer loop
      handler.start_async(),      # Bolt Socket Mode
    )
```

Expected boot log:
```
[Boot] Stream listener connected
[Boot] Rejection store connected (N existing rejections)
[Boot] AegisDB Slack Bot ready ✓
[SlackListener] Listening on 'aegisdb:slack'
Bolt app is running!
```

---

## API Endpoints Used

All calls go to `{aegisdb_base_url}/api/v1/`:

| Method | Path | Used by |
|---|---|---|
| `GET` | `/proposals/{id}` | stream_listener, app.py |
| `GET` | `/proposals?status=pending_approval&limit=5` | _cmd_proposals |
| `POST` | `/proposals/{id}/approve` | handle_approve |
| `POST` | `/proposals/{id}/reject` | handle_rejection_submit |
| `GET` | `/audit?limit=N` | _poll_for_completion, _cmd_audit |
| `GET` | `/status` | _cmd_status |
| `GET` | `/streams` | _cmd_status |
| `GET` | `/profiles?limit=3` | _fetch_profiling_trend |
| `GET` | `/profile/{id}/anomalies?table_name=X` | _fetch_profiling_trend |

---

## Redis Streams

| Stream | Publisher | Consumer | Purpose |
|---|---|---|---|
| `aegisdb:events` | webhook.py | stream_consumer.py | Main pipeline events |
| `aegisdb:repair` | stream_consumer | repair_agent | Repair tasks |
| `aegisdb:apply` | repair_agent | apply_agent | Apply tasks |
| `aegisdb:escalation` | stream_consumer | (manual) | Low confidence events |
| `aegisdb:slack` | slack_notifier | stream_listener | **Bot notifications** |

The bot only reads/writes `aegisdb:slack`. It never touches the main pipeline streams.

---

## ChromaDB Collections

| Collection | Owner | Purpose |
|---|---|---|
| `aegisdb_fixes` | Backend `vector_store.py` | Successful fix history for RAG |
| `aegisdb_rejections` | Bot `rejection_store.py` | Engineer rejection reasons |

Both use `all-MiniLM-L6-v2` embeddings and cosine similarity. Both live in the same `PersistentClient` at `./data/chromadb`.

**Windows note:** ChromaDB holds file locks while running. If you get a lock error on startup: stop all processes → delete `./data/chromadb/` → restart.

---

## Known Bugs Fixed During Session

### Bug 1 — `UnboundLocalError: proposal`
**File:** `slack_bot/stream_listener.py`  
**Cause:** `proposal_message_map` update referenced `proposal` before it was assigned.  
**Fix:** Initialize `proposal = None` before the `try` block. Move map update to after the fetch.

### Bug 2 — Thread Q&A never firing
**Cause:** Bot was posting to `#aegis-ops` via `chat:write.public` without being a member. Message events only delivered to channels bot has joined.  
**Fix:** `/invite @BotName` in the channel. Required even with `channels:history` scope.

### Bug 3 — Diff table showing all columns changing
**Cause:** `_build_diff_section` compared `sample_before[i]` vs `sample_after[i]` by index. Sandbox returns rows in different order, so every column appeared changed.  
**Fix:** Scan `sample_before` for `None` values to find NULL columns. Only show those columns. Don't compare row-by-row.

### Bug 4 — Pipeline stages `not_started`
**Cause:** FastAPI lifespan not starting stream consumer on first boot.  
**Fix:** Restart uvicorn. The consumer starts inside FastAPI's lifespan event.

### Bug 5 — Bolt silently dropping message events
**Cause:** Missing subtype handlers. Bolt requires explicit handlers for `bot_message`, `message_changed`, `message_deleted` subtypes or it drops all message events silently.  
**Fix:** Add three no-op ack handlers for each subtype.

---

## Known Limitations

### `proposal_message_map` is in-memory
Resets on bot restart. After restart, old cards' buttons (Approve/Reject) still work because Slack sends `proposal_id` in the action payload. But thread Q&A won't work for pre-restart cards — the `ts` → `proposal_id` mapping is gone.

**Future fix:** Persist the map to Redis with TTL.

### `vector_store` not initialized in bot process
`_sync_similar_fixes` in `qa_engine.py` imports `from src.db.vector_store import vector_store`. This only works if the bot is started from the project root and `vector_store.connect()` has been called. Currently it fails silently — Q&A still works using rejection history and profiling data.

**Future fix:** Initialize `vector_store` explicitly in bot boot sequence.

### Table owner map is hardcoded
`slack_settings.table_owner_map` returns a hardcoded dict. Replace `U00000000` with real Slack member IDs.

**Future fix:** Store in `.env` or a config file.

### No persistence for `DRY_RUN` toggle
`POST /dry-run/toggle` changes in-memory state only. Server restart resets to `.env` default (`true`).

### Rows Affected shows `500 → 500`
Sandbox fetches 500 rows from production and seeds 500 into the container. The `rows_before`/`rows_after` counts reflect sandbox container row counts, not production. The `rows_affected` (301) is correct.

---

## Demo Sequence

Run in this order for maximum impact:

```
Terminal 1: uvicorn src.main:app --port 8001
Terminal 2: python -m slack_bot.app
Terminal 3: (for curl commands)
```

1. `/aegis help` → verify bot alive
2. `/aegis status` → dashboard grid
3. Fire webhook → watch detecting card → self-update to proposal
4. Reply in thread: `is it safe to approve?` → Groq answers with context
5. Click **Reject** → enter reason → card updates
6. `/aegis why orders` → **MONEY MOMENT** — Groq synthesises your rejection back
7. Fire webhook again → click **Approve** → card morphs through states → receipt
8. `/aegis audit 5` → color-coded history
9. `/aegis ask which tables have the most anomalies?` → global Q&A

---

## Future Improvements

### High Priority

**Persist `proposal_message_map` to Redis**  
Key: `aegisdb:slack:map:{proposal_id}` with 24h TTL. Allows thread Q&A to survive bot restarts.

**Fix diff table for non-NULL failures**  
Current `_build_diff_section` only handles NULL violations well. For range violations or uniqueness violations, extend to detect value changes beyond just `None` checks.

**Initialize `vector_store` in bot boot**  
Add to `main()`:
```python
from src.db.vector_store import vector_store
await loop.run_in_executor(None, vector_store.connect)
```

### Medium Priority

**Proposal status stuck at `executing`**  
Proposals that fail mid-pipeline are never cleaned up. Add a background task that resets stale `executing` proposals after a timeout.

**Real table owner map**  
Replace hardcoded dict with environment variable or database lookup.

**Confidence trend across proposals**  
Show whether confidence is improving or degrading for a given table across multiple proposals.

### Low Priority

**Streaming Groq responses**  
Groq supports streaming but Slack doesn't support token-by-token updates in `chat.postMessage`. Would require posting a placeholder then updating — complex for marginal gain.

**Multi-workspace support**  
Current config is single-workspace. Would need workspace-specific token storage.

---

## Running the Bot

```bash
# From project root — required for src.db.vector_store import
python -m slack_bot.app
```

**Must run from project root** so that `from src.db.vector_store import vector_store` resolves correctly via PYTHONPATH.

**Dependencies:**
```bash
pip install slack-bolt
# All other deps already in requirements.txt
```

**Test the webhook:**
```bash
curl.exe -X POST http://localhost:8001/api/v1/webhook/om-test-failure \
  -H "Content-Type: application/json" \
  --data-binary "@test_payload.json"
```

`test_payload.json`:
```json
{
  "eventType": "entityUpdated",
  "entityType": "testCase",
  "entityFQN": "northwind_svc.northwind.public.orders.ship_region.orders_ship_region_not_null",
  "entity": {
    "name": "orders_ship_region_not_null",
    "fullyQualifiedName": "northwind_svc.northwind.public.orders.ship_region.orders_ship_region_not_null",
    "testCaseResult": {
      "testCaseStatus": "Failed",
      "result": "Found 301 null values in column ship_region"
    }
  },
  "timestamp": 1712345678000
}
```