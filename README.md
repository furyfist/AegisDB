# AegisDB

> **Autonomous database health monitoring and self-healing system.**  
> AegisDB detects data quality failures, diagnoses root cause using AI, proposes fixes for human review, and applies them to production — safely, with a full audit trail.

**Frontend:** [furyfist/aegisdb-frontend](https://github.com/furyfist/aegisdb-frontend)

---

### Dashboard Overview
![AegisDB Dashboard](./assets/hero_section.png)


---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [Running the Services](#running-the-services)
- [End-to-End Pipeline Walkthrough](#end-to-end-pipeline-walkthrough)
- [Project Structure](#project-structure)
- [API Reference](#api-reference)
- [Known Limitations](#known-limitations)
- [Contributing](#contributing)

---

## Overview

Production databases silently accumulate data quality failures. A NULL in a critical column, a referential integrity breach, a value outside expected range — OpenMetadata flags these, but someone still has to write the fix, test it, and run it manually. That loop takes time and tribal knowledge most teams don't have.

AegisDB closes the loop automatically:

1. OpenMetadata fires a webhook when a test fails
2. AegisDB classifies the failure, diagnoses the root cause with **Groq LLM** and a RAG knowledge base
3. It sandbox-tests a fix in an ephemeral Postgres container
4. A human approves or rejects the proposed fix via Slack or the web UI
5. On approval, AegisDB applies the fix to production inside a transaction with post-apply assertions
6. Every outcome — applied, rolled back, escalated, or dry-run — is written to an audit log
7. Successfully applied fixes annotate the affected column in OpenMetadata and update the Slack card with incident links

No tickets. No tribal knowledge lost. Every table AegisDB has ever touched becomes a self-documenting asset.

---

## Architecture

### System Architecture
![AegisDB Architecture](./assets/architecture.svg)


### Pipeline at a Glance

```
OpenMetadata (test failure)
        │
        ▼
  FastAPI Webhook
        │  background task
        ▼
  Detector → Diagnosis (Groq LLM + ChromaDB RAG)
        │
        ├── low confidence → Escalation Stream
        │
        └── high confidence → Sandbox Preview (testcontainers Postgres)
                │
                ▼
          Proposal Created → Human Review (Slack / Web UI)
                │
                ├── Rejected → Escalation, ChromaDB learns reason
                │
                └── Approved → Repair Agent (sandbox re-validation)
                        │
                        ▼
                  Apply Agent → Production Postgres
                        │
                        ├── COMMIT → Audit Log + OpenMetadata annotation + Slack update
                        └── ROLLBACK → Escalation
```

All inter-agent communication flows through **Redis Streams** — no direct agent-to-agent calls. Every message is consumer-group acknowledged (at-least-once delivery).

---

## Features

**Safe-first execution**
Every fix is sandbox-tested in an ephemeral Postgres container before a human ever sees the proposal. Production applies run inside an explicit transaction with post-apply SQL assertions. Automatic rollback on assertion failure.

**AI-powered diagnosis**
Groq LLM diagnoses root cause with full table schema context. ChromaDB provides RAG from past fixes — the system gets smarter with every approval.

**Human-in-the-loop**
No fix reaches production without explicit human approval. Proposals include a data diff (before/after rows), confidence score, rollback SQL, and a plain-English explanation.

**Conversational Slack bot**
Not just notifications — a full agent embedded in Slack. Posts live anomaly cards, self-updates as the pipeline progresses, answers questions in proposal threads with Gemini context, stores rejection reasons in ChromaDB.

**Auto-documentation**
On every successful production apply: a structured `FixReport` is written to Postgres, the fixed column's description in OpenMetadata is annotated, and the Slack card is updated with links to the incident timeline and audit entry.

**Full audit trail**
Every pipeline outcome — applied, dry run, rolled back, skipped, escalated — is written to `_aegisdb_audit` with the exact SQL that ran, row counts, assertion results, and confidence score.

**Dry-run mode**
Toggle `DRY_RUN` at runtime without restart. The full pipeline runs — diagnosis, sandbox, proposal, approval — but nothing touches production. Safe for demos and staging.

**Database profiler**
Direct Postgres profiler that detects NULL violations, uniqueness violations, range outliers (IQR), referential integrity breaks, and email format errors across all tables. No OpenMetadata required for this path.

---

## Tech Stack

### Backend

| Layer | Technology |
|---|---|
| Framework | FastAPI 0.115.0 + Uvicorn |
| Language | Python 3.12 |
| LLM | Groq (`groq`) |
| Vector store | ChromaDB 0.5.0 + `all-MiniLM-L6-v2` embeddings |
| Message broker | Redis Streams (redis-py 5.0.7) |
| Target DB driver | asyncpg 0.30.0 (async) + SQLAlchemy 2.0 |
| Sandbox | testcontainers-python 4.14.2 (ephemeral Postgres) |
| Data catalog | OpenMetadata 1.6.1 |
| Validation | Pydantic 2.7.0 |
| Infrastructure | Docker Compose |

### Frontend

| Layer | Technology |
|---|---|
| Framework | Next.js (App Router) |
| Language | TypeScript |
| Styling | Tailwind CSS |
| Data fetching | React Query (polling) |

---

## Prerequisites

- **Docker** and **Docker Compose** (for OpenMetadata, Postgres, Redis)
- **Python 3.12**
- **Node.js 18+** (for frontend)
- **Groq API key** — [get one here](https://console.groq.com/keys)
- 8 GB RAM minimum (OpenMetadata stack is heavy)

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/your-org/aegisdb.git
cd aegisdb
```

### 2. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` — at minimum set:

```env
GROQ_API_KEY=your_key_here
OM_ADMIN_PASSWORD=your_om_password
TARGET_DB_PASSWORD=aegisdb_pass
```

Full variable reference → [Environment Variables](#environment-variables)

### 3. Start the infrastructure

```bash
docker compose -f docker/openmetadata/docker-compose.yml up -d
```

Wait ~2 minutes for OpenMetadata to be healthy. Check at `http://localhost:8585`.

### 4. Install backend dependencies

```bash
pip install -r requirements.txt
```

### 5. Start the backend

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8001 --reload
```

Expected output:
```
[Boot] ChromaDB ready
[Boot] Audit table ready
[Boot] Event store ready
[Boot] Profiling store ready
[Boot] Connection registry ready
[Boot] Proposal store ready
[Boot] Event bus ready
[Boot] Diagnosis consumer running
[Boot] Repair agent running
[Boot] Apply agent running
AegisDB ready ✓
```

### 6. Start the Slack bot (optional)

```bash
python -m src.slack.bot
```

Requires `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in `.env`. See [Slack Setup](#slack-setup).

### 7. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`.

---

## Environment Variables

Create a `.env` file in the project root. A `.env.example` is provided.

| Variable | Default | Required | Description |
|---|---|---|---|
| `GROQ_API_KEY` | — | ✅ | Groq API key |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | | Groq model name |
| `LLM_MAX_TOKENS` | `4096` | | Max tokens per LLM call |
| `OM_HOST` | `http://localhost:8585` | ✅ | OpenMetadata server URL |
| `OM_ADMIN_EMAIL` | `admin@open-metadata.org` | ✅ | |
| `OM_ADMIN_PASSWORD` | — | ✅ | |
| `REDIS_HOST` | `localhost` | ✅ | |
| `REDIS_PORT` | `6379` | ✅ | |
| `REDIS_STREAM_NAME` | `aegisdb:events` | | |
| `REDIS_REPAIR_STREAM` | `aegisdb:repair` | | |
| `REDIS_ESCALATION_STREAM` | `aegisdb:escalation` | | |
| `REDIS_APPLY_STREAM` | `aegisdb:apply` | | |
| `REDIS_CONSUMER_GROUP` | `aegisdb-agents` | | |
| `APP_HOST` | `0.0.0.0` | | |
| `APP_PORT` | `8000` | | Frontend proxies to 8001 |
| `TARGET_DB_HOST` | `localhost` | ✅ | |
| `TARGET_DB_PORT` | `5433` | ✅ | |
| `TARGET_DB_NAME` | `northwind` | ✅ | |
| `TARGET_DB_USER` | `aegisdb_user` | ✅ | |
| `TARGET_DB_PASSWORD` | `aegisdb_pass` | ✅ | |
| `CHROMA_PERSIST_DIR` | `./data/chromadb` | | |
| `CHROMA_COLLECTION` | `aegisdb_fixes` | | |
| `CONFIDENCE_THRESHOLD` | `0.70` | | Min confidence to create proposal |
| `SANDBOX_MAX_RETRIES` | `3` | | |
| `SANDBOX_SAMPLE_ROWS` | `500` | | Max rows seeded into sandbox |
| `SANDBOX_DIFF_ROWS` | `20` | | Max rows shown in data diff |
| `SANDBOX_TIMEOUT_SECONDS` | `120` | | |
| `DRY_RUN` | `true` | | Set to `false` for live applies |
| `APPLY_STATEMENT_TIMEOUT_MS` | `30000` | | |
| `POST_APPLY_VERIFY` | `true` | | Run assertions after production apply |
| `SLACK_BOT_TOKEN` | — | Slack only | `xoxb-...` |
| `SLACK_APP_TOKEN` | — | Slack only | `xapp-...` (Socket Mode) |
| `SLACK_CHANNEL_ID` | — | Slack only | Channel for anomaly cards |

> **Security:** Never commit `.env`. The `.gitignore` already excludes it. Credentials submitted via `POST /connect` are never stored — only `host:port/db` is retained.

---

## Running the Services

### Infrastructure only

```bash
# Start OpenMetadata stack + target Postgres + Redis
docker compose -f docker/openmetadata/docker-compose.yml up -d

# Stop everything
docker compose -f docker/openmetadata/docker-compose.yml down

# Stop and wipe volumes (full reset)
docker compose -f docker/openmetadata/docker-compose.yml down -v
```

### Seed dirty test data

```bash
psql postgresql://aegisdb_user:aegisdb_pass@localhost:5433/northwind \
  -f scripts/seed_dirty_data.sql
```

### Toggle dry-run mode

```bash
# Check current state
curl http://localhost:8001/api/v1/status

# Toggle (switches between true/false in memory — resets on restart)
curl -X POST http://localhost:8001/api/v1/dry-run/toggle
```

### Trigger a test failure manually

```bash
curl -X POST http://localhost:8001/api/v1/webhook/om-test-failure \
  -H "Content-Type: application/json" \
  -d '{
    "eventType": "entityUpdated",
    "entityType": "testCase",
    "entityFQN": "northwind_svc.northwind.public.employees.region.employees_region_not_null",
    "entity": {
      "name": "employees_region_not_null",
      "fullyQualifiedName": "northwind_svc.northwind.public.employees.region.employees_region_not_null",
      "testCaseResult": {
        "testCaseStatus": "Failed",
        "result": "Found 5 null values in column region"
      }
    },
    "timestamp": 1712345678000
  }'
```

### Reset ChromaDB

```bash
# Stop the server first, then:
rm -rf ./data/chromadb/
# Restart — 5 bootstrap entries are re-seeded automatically
```

---

## End-to-End Pipeline Walkthrough


**Step 1 — Failure detected**
![Pipeline Health](./assets/dashboard.png)

OpenMetadata runs a data quality test and finds a NULL violation in `employees.region`. It fires a webhook to AegisDB at `POST /webhook/om-test-failure`. AegisDB returns 200 immediately and processes the event in the background.

---


**Step 2 — Classify and diagnose**
The detector classifies the failure as `NULL_VIOLATION` with severity `low`. The diagnosis agent queries ChromaDB for similar past fixes (RAG), builds a prompt with full table schema context, and calls Groq LLM. It returns a `DiagnosisResult` with a confidence score, root cause in plain English, a fix SQL, and rollback SQL.

---



**Step 3 — Sandbox preview and proposal**
![Review Proposal](./assets/proposals.png)

If confidence ≥ 0.70, AegisDB spins up an ephemeral Postgres container, seeds it with up to 500 rows from the target table, runs the fix, validates assertions, and captures a before/after data diff. A proposal is created in `pending_approval` status.

---


**Step 4 — Human review**
The proposal appears in the web UI (and Slack). The operator reviews: the plain-English root cause, the exact SQL that will run, a diff of affected rows, confidence score, and rollback SQL. They click Approve or Reject.

---


**Step 5 — Apply to production**
On approval, the repair agent re-validates in a fresh sandbox (up to 3 retries). On pass, the apply agent executes the fix inside a Postgres transaction with a 30-second statement timeout. Post-apply assertions run. On pass → COMMIT. On fail → ROLLBACK → escalation.

---


**Step 6 — Audit and documentation**
![Audit Log Entry](./assets/Audit-log.png)

The audit log records every detail: the SQL that ran, rows affected, assertion results, confidence score, and whether it was a dry run. The fixed column's description in OpenMetadata is annotated. The Slack card updates with links to the incident timeline and audit entry.

---

## Project Structure

```
aegisdb/
├── src/
│   ├── agents/
│   │   ├── detector.py          # Rule-based failure classifier
│   │   ├── diagnosis.py         # LLM diagnosis via Groq
│   │   ├── profiler.py          # Direct Postgres profiling engine
│   │   ├── repair.py            # Sandbox orchestrator (post-approval)
│   │   └── apply.py             # Production fix executor
│   ├── api/
│   │   ├── webhook.py           # OpenMetadata webhook receiver
│   │   ├── dashboard.py         # Audit / stream / escalation endpoints
│   │   ├── profiler_routes.py   # Profiling API
│   │   ├── onboarding_routes.py # Database connect + re-profile
│   │   ├── proposal_routes.py   # Proposal approve / reject / detail
│   │   └── table_routes.py      # Live table data
│   ├── core/
│   │   ├── config.py            # Settings via pydantic-settings (.env)
│   │   └── models.py            # All Pydantic models
│   ├── db/
│   │   ├── audit_log.py         # _aegisdb_audit CRUD
│   │   ├── event_store.py       # _aegisdb_events CRUD
│   │   ├── vector_store.py      # ChromaDB wrapper
│   │   ├── profiling_store.py   # _aegisdb_profiling_reports CRUD
│   │   ├── proposal_store.py    # _aegisdb_proposals CRUD
│   │   └── connection_registry.py
│   ├── sandbox/
│   │   ├── executor.py          # testcontainers Postgres orchestration
│   │   └── validator.py         # SQL assertions
│   ├── services/
│   │   ├── om_client.py         # OpenMetadata REST client
│   │   ├── event_bus.py         # Redis Streams publisher
│   │   ├── stream_consumer.py   # Redis Streams consumer
│   │   └── om_ingestion.py      # In-process ingestion
│   └── main.py                  # FastAPI app + boot sequence
├── frontend/                    # Next.js frontend
├── docker/openmetadata/         # Docker Compose + env
├── scripts/
│   └── seed_dirty_data.sql      # Test dirty data for Northwind
├── data/chromadb/               # ChromaDB persistent store (gitignored)
├── docs/
│   ├── ARCHITECTURE.md          # Full backend architecture reference
│   ├── slack-integration.md     # Slack bot technical reference
│   └── autodoc.md               # Auto-documentation feature reference
├── .env.example
└── requirements.txt
```

---

## API Reference

Interactive docs available at `http://localhost:8001/docs` (Swagger UI) once the backend is running.

### Core endpoints

| Method | Route | Description |
|---|---|---|
| `POST` | `/api/v1/webhook/om-test-failure` | Receive OpenMetadata test failure event |
| `GET` | `/api/v1/health` | Health check |
| `GET` | `/api/v1/status` | Full pipeline status |
| `GET` | `/api/v1/audit` | Audit log |
| `GET` | `/api/v1/audit/{event_id}` | Single audit entry |
| `GET` | `/api/v1/escalations` | Escalation queue |
| `POST` | `/api/v1/dry-run/toggle` | Toggle live / safe mode |
| `POST` | `/api/v1/connect` | Connect and onboard a database |
| `GET` | `/api/v1/connections` | List connections |
| `GET` | `/api/v1/connections/{id}/health` | Database health score |
| `POST` | `/api/v1/profile` | Direct profiling (no OM required) |
| `GET` | `/api/v1/proposals/pending` | Pending approval queue |
| `POST` | `/api/v1/proposals/{id}/approve` | Approve fix |
| `POST` | `/api/v1/proposals/{id}/reject` | Reject fix |

Full API specification → [`docs/ARCHITECTURE.md § API Reference`](docs/ARCHITECTURE.md#12-api-reference)

---

## Slack Setup

### Conversational Incident Management

AegisDB isn't just a notification bot — it's a conversational SRE embedded in your workspace.

#### 1. Interactive Anomaly Cards
![Slack Proposal Card](./assets/slack_proposal.png)
*Receive detailed anomaly cards with data diffs, confidence scores, and one-click approval buttons directly in Slack.*

#### 2. Conversational Q&A (RAG)
![Slack Thread Q&A](./assets/slack_BotQuery.png)
*Ask questions in the incident thread. The bot uses Groq LLM and the ChromaDB RAG store to explain why a fix is safe or how it compares to past incidents.*

#### 3. Automated Resolution Receipts
![Slack Resolution Receipt](./assets/slack_DBfixed.png)
*Upon approval, cards update in real-time to provide a receipt of the fix, including exact rows healed and a link to the audit trail.*


1. Create a Slack app at `https://api.slack.com/apps`
2. Enable **Socket Mode** — generates an App-Level Token (`xapp-...`)
3. Add Bot Token Scopes: `chat:write`, `chat:write.public`, `channels:history`, `reactions:read`
4. Install the app to your workspace — copy the Bot Token (`xoxb-...`)
5. Set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID` in `.env`
6. Start the bot: `python -m src.slack.bot`

The bot connects via Socket Mode — no public URL or ngrok required.

---

## Frontend Screenshots

### Database Connections
![Connections Page](./assets/connections.png)


### Incident Timeline
![Incident History](./assets/incident.png)


### Data Profiling Report
![Profiling Report](./assets/profilling.png)




---

## Known Limitations

- **No authentication.** All API endpoints are publicly accessible. Do not expose to the internet without an auth layer in front.
- **No credential storage.** Database credentials are validated at connect time but never persisted. Re-profiling always requires re-submitting credentials.
- **`DRY_RUN` is not persisted.** Toggling via API resets to `.env` value on next restart. Default is `true`.
- **Validator assertion coverage.** `format_violation` and `schema_drift` categories skip assertions and return `passed=True`. Range bounds are hardcoded to `[0, 9_999_999]`.
- **Proposals stuck at `executing`.** Proposals that fail mid-pipeline are not automatically reset. Fire a new webhook to generate a fresh proposal.
- **Sandbox on Windows.** testcontainers may produce Docker API 500 errors on container removal. Set `TESTCONTAINERS_HOST_OVERRIDE=localhost` if needed.
- **OM FQN mismatch.** Tables connected via `POST /connect` may not resolve in OpenMetadata table lookups. Pipeline continues with `enrichment_success=false` — does not block diagnosis or repair.

---

## Contributing

1. Branch from `main`
2. Backend changes → update `docs/ARCHITECTURE.md` if any API contract, env var, or data model changes
3. Frontend changes → update `lib/types.ts` and `lib/api.ts` before touching components
4. All new endpoints must be reflected in the API route table in `ARCHITECTURE.md`
5. Do not commit `.env`, `./data/chromadb/`, or any credentials

---

## License

MIT