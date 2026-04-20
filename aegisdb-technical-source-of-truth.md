# AegisDB — Technical Source of Truth

> **Audience:** Backend engineers, frontend engineers, and technical reviewers.
> This document covers the backend system only. Frontend implementation is out of scope.
> Last verified state: All 6 backend test layers passing as of April 20, 2026.

---

## 1. Architecture Overview

### 1.1 Tech Stack

| Layer | Technology | Version / Notes |
|---|---|---|
| Backend framework | FastAPI | 0.115.0 |
| ASGI server | Uvicorn | 0.30.6 with `standard` extras |
| Language | Python | 3.12 |
| Target database driver | asyncpg | 0.30.0 (async) |
| Target database ORM | SQLAlchemy | 2.0.36 async |
| Sync DB driver (sandbox) | psycopg2-binary | 2.9.10 |
| Data validation | Pydantic | 2.7.0 |
| Settings management | pydantic-settings | 2.3.0 |
| HTTP client | httpx | 0.27.0 |
| Message broker | Redis Streams | via redis-py 5.0.7 |
| LLM provider | Groq | `groq==0.11.0`, model: `llama-3.3-70b-versatile` |
| Vector store | ChromaDB | 0.5.0 |
| Embedding model | sentence-transformers | 3.0.1, model: `all-MiniLM-L6-v2` |
| Sandbox executor | testcontainers-python | 4.14.2, postgres extra |
| Metadata catalog | OpenMetadata | 1.6.1 (Docker Compose) |
| In-process ingestion | openmetadata-ingestion | 1.6.1, postgres extra |
| Infrastructure | Docker Compose | All supporting services |

### 1.2 Supporting Infrastructure (Docker Compose)

All services run via a single Docker Compose file at `docker/openmetadata/docker-compose.yml`.

| Container | Image | Port Mapping | Purpose |
|---|---|---|---|
| `openmetadata_server` | `docker.getcollate.io/openmetadata/server:1.6.1` | 8585–8586 | OpenMetadata API + UI |
| `openmetadata_postgresql` | `docker.getcollate.io/openmetadata/postgresql:1.6.1` | 5432 | OpenMetadata internal DB |
| `openmetadata_elasticsearch` | `docker.elastic.co/elasticsearch/elasticsearch:8.11.4` | 9200, 9300 | OM search backend |
| `openmetadata_ingestion` | `docker.getcollate.io/openmetadata/ingestion:1.6.1` | 8080 | Airflow-based ingestion (not used for in-process flow) |
| `aegisdb_postgres` | `postgres:16-alpine` | **5433:5432** | Target database AegisDB monitors and heals |
| `aegisdb_redis` | `redis:7.2-alpine` | 6379 | Redis Streams event bus |

> **Note:** The target database container is named `aegisdb_postgres` (not `aegisdb_target_db`). All external connections use `localhost:5433`.

### 1.3 Project Structure

```
aegisdb/
├── src/
│   ├── agents/
│   │   ├── detector.py          # Phase 3 — rule-based failure classifier
│   │   ├── diagnosis.py         # Phase 3 — LLM diagnosis via Groq
│   │   ├── profiler.py          # Phase B — direct Postgres profiling engine
│   │   ├── repair.py            # Phase 4 — sandbox orchestrator
│   │   └── apply.py             # Phase 5 — production fix executor
│   ├── api/
│   │   ├── webhook.py           # Phase 2 — OpenMetadata webhook receiver
│   │   ├── dashboard.py         # Phase 5 — audit/stream/escalation read endpoints
│   │   ├── profiler_routes.py   # Phase B — profiling API endpoints
│   │   ├── onboarding_routes.py # Phase C — database connect endpoint
│   │   └── proposal_routes.py   # Phase D — proposal approve/reject endpoints
│   ├── core/
│   │   ├── config.py            # All settings via pydantic-settings (.env)
│   │   └── models.py            # All Pydantic models (Phases 1–D)
│   ├── db/
│   │   ├── target_db.py         # Async read connection to aegisdb_postgres
│   │   ├── audit_log.py         # _aegisdb_audit table CRUD
│   │   ├── event_store.py       # _aegisdb_events table CRUD
│   │   ├── vector_store.py      # ChromaDB wrapper
│   │   ├── profiling_store.py   # _aegisdb_profiling_reports table CRUD
│   │   ├── proposal_store.py    # _aegisdb_proposals table CRUD
│   │   └── connection_registry.py # _aegisdb_connections table CRUD
│   ├── sandbox/
│   │   ├── executor.py          # testcontainers ephemeral Postgres orchestration
│   │   └── validator.py         # SQL assertions against sandbox or production
│   ├── services/
│   │   ├── om_client.py         # OpenMetadata REST API client (with token refresh)
│   │   ├── event_bus.py         # Redis Streams publisher
│   │   ├── stream_consumer.py   # Redis Streams consumer (Diagnosis pipeline)
│   │   └── om_ingestion.py      # In-process MetadataWorkflow runner
│   └── main.py                  # FastAPI app + lifespan boot sequence
├── docker/
│   └── openmetadata/
│       ├── docker-compose.yml
│       └── openmetadata.env
├── scripts/
│   └── seed_dirty_data.sql      # Intentionally dirty test data for aegisdb_postgres
├── data/
│   └── chromadb/                # ChromaDB persistent storage (local disk)
├── .env                         # Runtime config (never committed)
└── requirements.txt
```

### 1.4 Design Patterns

- **Event-driven pipeline:** All inter-agent communication goes through Redis Streams. No direct agent-to-agent calls.
- **Background tasks:** FastAPI `BackgroundTasks` handles webhook enrichment and onboarding without blocking HTTP responses.
- **Consumer groups:** Redis XREADGROUP with consumer groups ensures at-least-once delivery and allows ACK-based retry on failure.
- **Proposal gate:** Phase D inserts a human-approval step between diagnosis and repair. The repair agent only runs when a proposal is explicitly approved via API.
- **Dry-run toggle:** `DRY_RUN=true` allows the full pipeline to run without writing to production. Toggled at runtime via API.
- **Sandbox-first:** Every fix is validated in an ephemeral testcontainers Postgres clone before either preview (proposal creation) or production apply (post-approval).
- **asyncpg JSONB constraint:** All JSONB inserts use `CAST(:param AS jsonb)` syntax. The `::jsonb` cast operator is rejected by asyncpg's prepared statement engine.

---

## 2. Data Models

### 2.1 Internal Postgres Tables (in `aegisdb_postgres`)

All internal tables are prefixed with `_aegisdb_` and excluded from profiling scans.

---

#### `_aegisdb_events`

Stores enriched webhook events immediately after OM enrichment. Enables downstream agents to look up real schema/column context by `event_id`.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | |
| `event_id` | VARCHAR(36) UNIQUE NOT NULL | UUID from webhook |
| `table_fqn` | TEXT NOT NULL | e.g. `aegisDB.aegisdb.public.orders` |
| `table_name` | TEXT NOT NULL | Last segment of FQN |
| `enrichment_ok` | BOOLEAN | True if OM schema context was fetched |
| `severity` | VARCHAR(20) | low / medium / high / critical |
| `event_data` | JSONB NOT NULL | Full `EnrichedFailureEvent` serialized |
| `created_at` | TIMESTAMPTZ | Default NOW() |

Indexes: `event_id`, `created_at DESC`

---

#### `_aegisdb_proposals`

Stores repair proposals awaiting human approval. Created by the stream consumer after diagnosis + sandbox preview.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | |
| `proposal_id` | VARCHAR(36) UNIQUE NOT NULL | UUID |
| `event_id` | VARCHAR(36) NOT NULL | FK reference to `_aegisdb_events` (soft) |
| `table_fqn` | TEXT NOT NULL | |
| `table_name` | TEXT NOT NULL | |
| `failure_categories` | TEXT[] | e.g. `['null_violation']` |
| `root_cause` | TEXT | LLM-generated plain English |
| `confidence` | FLOAT | 0.0 – 1.0. Threshold: 0.70 |
| `fix_sql` | TEXT | Contains `{table}` placeholder |
| `fix_description` | TEXT | Human-readable |
| `rollback_sql` | TEXT nullable | |
| `estimated_rows` | INTEGER nullable | |
| `sandbox_passed` | BOOLEAN | From preview sandbox run |
| `rows_before` | INTEGER | Sandbox before count |
| `rows_after` | INTEGER | Sandbox after count |
| `rows_affected` | INTEGER | rows_deleted + rows_updated |
| `sample_before` | JSONB | Up to 5 rows before fix |
| `sample_after` | JSONB | Up to 5 rows after fix |
| `status` | VARCHAR(30) | See ProposalStatus enum |
| `created_at` | TIMESTAMPTZ | |
| `decided_at` | TIMESTAMPTZ nullable | Set on approve/reject |
| `decision_by` | TEXT | Default: `system` |
| `rejection_reason` | TEXT nullable | |
| `diagnosis_json` | TEXT | Serialized `DiagnosisResult` |
| `event_json` | TEXT | Serialized `EnrichedFailureEvent` |

Indexes: `proposal_id`, `status`, `created_at DESC`

**ProposalStatus values:** `pending_approval`, `approved`, `rejected`, `executing`, `completed`, `failed`

---

#### `_aegisdb_audit`

One row per fix attempt. Written by the apply agent on every outcome (applied, dry_run, failed, rolled_back).

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | |
| `event_id` | VARCHAR(36) NOT NULL | |
| `table_fqn` | TEXT NOT NULL | |
| `table_name` | TEXT NOT NULL | |
| `action` | VARCHAR(20) NOT NULL | applied / dry_run / failed / rolled_back / skipped |
| `fix_sql` | TEXT | `{table}` already substituted |
| `rollback_sql` | TEXT nullable | |
| `rows_affected` | INTEGER | |
| `dry_run` | BOOLEAN | |
| `sandbox_passed` | BOOLEAN | |
| `confidence` | FLOAT | |
| `failure_categories` | TEXT[] | |
| `applied_at` | TIMESTAMPTZ | Default NOW() |
| `post_apply_json` | JSONB | Array of assertion results |
| `error` | TEXT nullable | Exception message if failed |
| `metadata_json` | JSONB | Reserved |

Indexes: `event_id`, `applied_at DESC`, `table_fqn`

---

#### `_aegisdb_profiling_reports`

Stores full profiling reports from the profiler agent.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | |
| `report_id` | VARCHAR(36) UNIQUE NOT NULL | UUID |
| `connection_hint` | TEXT | `host:port/db` — no credentials stored |
| `status` | VARCHAR(20) | completed / failed / partial |
| `tables_scanned` | INTEGER | |
| `total_anomalies` | INTEGER | |
| `critical_count` | INTEGER | |
| `warning_count` | INTEGER | |
| `duration_ms` | INTEGER | |
| `report_data` | JSONB NOT NULL | Full `ProfilingReport` serialized |
| `created_at` | TIMESTAMPTZ | Default NOW() |

Indexes: `report_id`, `created_at DESC`

---

#### `_aegisdb_connections`

Registry of databases connected via `POST /api/v1/connect`.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | |
| `connection_id` | VARCHAR(36) UNIQUE NOT NULL | UUID |
| `service_name` | TEXT NOT NULL | OM service name, auto-generated if not provided |
| `connection_hint` | TEXT NOT NULL | `host:port/db` |
| `db_name` | TEXT NOT NULL | |
| `schema_names` | TEXT[] | Default: `['public']` |
| `status` | VARCHAR(20) | pending / connected / ingesting / profiling / ready / failed |
| `om_service_fqn` | TEXT | Assigned by OM after registration |
| `profiling_report_id` | TEXT nullable | UUID of associated `_aegisdb_profiling_reports` row |
| `tables_found` | INTEGER | |
| `total_anomalies` | INTEGER | |
| `critical_count` | INTEGER | |
| `error` | TEXT nullable | |
| `registered_at` | TIMESTAMPTZ | Default NOW() |
| `last_profiled_at` | TIMESTAMPTZ nullable | |

Indexes: `connection_id`

---

### 2.2 Redis Streams

| Stream Key | Publisher | Consumer | Purpose |
|---|---|---|---|
| `aegisdb:events` | `event_bus.py` | `stream_consumer.py` | Enriched webhook events |
| `aegisdb:repair` | `proposal_routes.py` (on approve) | `repair.py` | Approved diagnoses ready for repair |
| `aegisdb:apply` | `repair.py` | `apply.py` | Sandbox-validated fixes ready for production apply |
| `aegisdb:escalation` | `stream_consumer.py`, `apply.py`, `proposal_routes.py` | Read-only via dashboard | Events that could not be auto-repaired |

All streams use consumer group `aegisdb-agents`. Max length capped at 500–1000 entries.

---

### 2.3 ChromaDB Collection

Collection name: `aegisdb_fixes`  
Embedding model: `all-MiniLM-L6-v2` (384-dim, cosine similarity)  
Persistent path: `./data/chromadb`

Each document encodes: `table_fqn + failure_category + problem_description + fix_sql`

Metadata fields per document:
- `fix_id` (required — missing in earlier bootstrap entries caused `KeyError`)
- `table_fqn`
- `failure_category`
- `problem_description`
- `fix_sql`
- `was_successful` (string `"True"` / `"False"`)
- `event_id`
- `stored_at`

5 bootstrap entries are seeded on startup if the collection is empty.

---

### 2.4 Target Database — Test Schema

Database: `aegisdb`  
Tables: `orders`, `customers`

**`orders`**

| Column | Type | Constraint |
|---|---|---|
| `order_id` | SERIAL | PRIMARY KEY |
| `customer_id` | INT | Nullable — intentionally contains NULLs |
| `amount` | NUMERIC(10,2) | Nullable — intentionally contains negatives |
| `status` | VARCHAR(50) | Nullable — intentionally contains invalid values |
| `created_at` | TIMESTAMP | DEFAULT NOW() |

**`customers`**

| Column | Type | Constraint |
|---|---|---|
| `customer_id` | SERIAL | PRIMARY KEY |
| `email` | VARCHAR(255) | Nullable — intentionally contains duplicates and NULLs |
| `age` | INT | Nullable — intentionally contains negatives and values > 150 |
| `country` | VARCHAR(100) | Nullable |

Seed file: `scripts/seed_dirty_data.sql`

---

## 3. API Specification

Base URL: `http://localhost:8000/api/v1`  
Authentication: None (open API, no auth layer implemented)  
Content-Type: `application/json` for all requests  

---

### 3.1 Webhook

#### `POST /webhook/om-test-failure`

Receives OpenMetadata test failure events. Returns 200 immediately. Enrichment and pipeline entry run as background tasks.

**Request body:**
```json
{
  "eventType": "entityUpdated",
  "entityType": "testCase",
  "entityFQN": "aegisDB.aegisdb.public.orders.customer_id.orders_customer_id_not_null",
  "entity": {
    "name": "orders_customer_id_not_null",
    "fullyQualifiedName": "aegisDB.aegisdb.public.orders.customer_id.orders_customer_id_not_null",
    "testCaseResult": {
      "testCaseStatus": "Failed",
      "result": "Found 2 null values in column customer_id"
    }
  },
  "timestamp": 1712345678000
}
```

**Response:**
```json
{
  "status": "accepted",
  "event_id": "uuid",
  "table_fqn": "aegisDB.aegisdb.public.orders",
  "failed_tests": 1
}
```

**Non-failure events:** Returns `{"status": "skipped", "reason": "not a test failure event"}`

**FQN parsing rule:** Table FQN = first 4 dot-separated segments of `entityFQN`

---

### 3.2 Health

#### `GET /health`
```json
{ "status": "ok", "service": "aegisdb-webhook" }
```

#### `GET /status`
```json
{
  "version": "0.4.0",
  "dry_run": true,
  "confidence_threshold": 0.7,
  "pipeline": {
    "1_webhook": "ok",
    "2_event_bus": "ok",
    "3_diagnosis_consumer": "ok",
    "4_repair_agent": "ok",
    "5_apply_agent": "ok"
  }
}
```

---

### 3.3 Dashboard

#### `GET /audit?limit={n}`

Returns most recent audit log entries. Default limit: 20. Max: 100.

**Response:**
```json
{
  "count": 4,
  "entries": [
    {
      "id": 4,
      "event_id": "uuid",
      "table_fqn": "aegisDB.aegisdb.public.orders",
      "table_name": "orders",
      "action": "applied",
      "rows_affected": 2,
      "dry_run": false,
      "sandbox_passed": true,
      "confidence": 0.94,
      "failure_categories": ["null_violation"],
      "applied_at": "2026-04-20T07:39:07.375763+00:00",
      "error": null
    }
  ]
}
```

`action` values: `applied`, `dry_run`, `failed`, `rolled_back`, `skipped`

---

#### `GET /streams`
```json
{
  "streams": {
    "aegisdb:events": { "length": 4, "last_message_id": "..." },
    "aegisdb:repair": { "length": 1, "last_message_id": "..." },
    "aegisdb:apply":  { "length": 1, "last_message_id": "..." },
    "aegisdb:escalation": { "length": 2, "last_message_id": "..." }
  }
}
```

---

#### `GET /escalations?limit={n}`
```json
{
  "count": 2,
  "escalations": [
    {
      "message_id": "...",
      "event_id": "uuid",
      "table_fqn": "aegisDB.aegisdb.public.orders",
      "reason": "Rejected by user: Testing rejection flow",
      "stage": "approval",
      "escalated_at": "2026-04-20T12:07:50+00:00"
    }
  ]
}
```

`stage` values: `apply`, `approval`, `diagnosis`

---

#### `POST /dry-run/toggle`

Toggles the DRY_RUN setting at runtime without restart.

**Response:**
```json
{ "dry_run": false, "message": "Apply agent now in LIVE mode" }
```

---

### 3.4 Profiling

#### `POST /profile`

Profiles a Postgres database directly. Runs synchronously. Returns in under 30 seconds for typical schemas.

**Request body:**
```json
{
  "connection_url": "postgresql://user:pass@host:port/database",
  "schemas": ["public"],
  "table_limit": 50
}
```

**Validation:** `connection_url` must start with `postgresql://` or `postgres://`

**Response:**
```json
{
  "report_id": "uuid",
  "connection_hint": "localhost:5433/aegisdb",
  "status": "completed",
  "tables_scanned": 2,
  "total_anomalies": 6,
  "critical_count": 6,
  "warning_count": 0,
  "duration_ms": 172,
  "message": "Found 6 anomalies across 2 tables (6 critical, 0 warnings)"
}
```

---

#### `GET /profile/{report_id}`

Returns full profiling report including per-table per-column anomaly details.

**Response:** Full `ProfilingReport` object including `tables[]` array with `anomalies[]` per table.

Each anomaly:
```json
{
  "column_name": "customer_id",
  "anomaly_type": "null_violation",
  "severity": "critical",
  "affected_rows": 2,
  "total_rows": 6,
  "rate": 0.3333,
  "description": "Column 'customer_id' has 2 NULL values (33.3%) but should be NOT NULL",
  "sample_values": ["(1,)", "(5,)"]
}
```

`anomaly_type` values: `null_violation`, `range_violation`, `uniqueness_violation`, `referential_integrity`, `format_violation`, `schema_drift`, `unknown`

`severity` values: `info`, `warning`, `critical`

---

#### `GET /profile/{report_id}/anomalies?severity={s}&table_name={t}`

Returns filtered flat list of anomalies from a report.

**Response:**
```json
{
  "report_id": "uuid",
  "total_matched": 6,
  "anomalies": [
    {
      "table": "orders",
      "schema": "public",
      "column": "amount",
      "anomaly_type": "range_violation",
      "severity": "critical",
      "affected_rows": 1,
      "total_rows": 6,
      "rate": 0.1667,
      "description": "...",
      "sample_values": []
    }
  ]
}
```

---

#### `GET /profiles?limit={n}`

Lists recent profiling report summaries. Max: 50.

**Response:**
```json
{
  "count": 2,
  "reports": [
    {
      "report_id": "uuid",
      "connection_hint": "localhost:5433/aegisdb",
      "status": "completed",
      "tables_scanned": 2,
      "total_anomalies": 6,
      "critical_count": 6,
      "warning_count": 0,
      "duration_ms": 172,
      "created_at": "2026-04-20T06:41:03.072800+00:00"
    }
  ]
}
```

---

### 3.5 Onboarding

#### `POST /connect`

Validates credentials, registers DB in OpenMetadata, runs in-process ingestion, profiles, and saves results. Returns immediately — all work runs as a background task.

**Request body:**
```json
{
  "host": "localhost",
  "port": 5433,
  "database": "aegisdb",
  "username": "aegisdb_user",
  "password": "aegisdb_pass",
  "schemas": ["public"],
  "service_name": "optional-custom-name"
}
```

**Validation:** Connection is tested synchronously via asyncpg before background task starts. Returns 400 if connection fails.

**Response (200 — returned immediately):**
```json
{
  "connection_id": "uuid",
  "service_name": "aegisdb_localhost_aegisdb_6407e6fb",
  "connection_hint": "localhost:5433/aegisdb",
  "status": "pending",
  "tables_found": 0,
  "total_anomalies": 0,
  "critical_count": 0,
  "profiling_report_id": null,
  "message": "Connection validated. Onboarding started in background. Poll GET /api/v1/connections/{id} for status."
}
```

**Status transitions (background):** `pending → ingesting → profiling → ready` (or `failed`)

---

#### `GET /connections/{connection_id}`

Poll onboarding status. Returns full `DatabaseConnection` object.

**Response (when ready):**
```json
{
  "connection_id": "uuid",
  "service_name": "aegisdb_localhost_aegisdb_6407e6fb",
  "connection_hint": "localhost:5433/aegisdb",
  "db_name": "aegisdb",
  "schema_names": ["public"],
  "status": "ready",
  "om_service_fqn": "aegisdb_localhost_aegisdb_6407e6fb",
  "profiling_report_id": "uuid",
  "tables_found": 2,
  "total_anomalies": 6,
  "critical_count": 6,
  "registered_at": "2026-04-20T06:50:04.292949Z",
  "last_profiled_at": "2026-04-20T06:50:33.730887Z",
  "error": null
}
```

---

#### `GET /connections`

Lists all registered database connections (summary fields only).

**Response:**
```json
{
  "count": 1,
  "connections": [...]
}
```

---

#### `POST /connections/{connection_id}/re-profile` — PARTIAL

Defined but returns 400 requiring credentials to be re-supplied. Re-profiling is accomplished by calling `POST /connect` again with the same credentials.

---

### 3.6 Proposals

#### `GET /proposals/pending`

Returns all proposals with `status=pending_approval`. Auto-refreshed by frontend every 5 seconds recommended.

**Response:**
```json
{
  "count": 1,
  "proposals": [
    {
      "proposal_id": "uuid",
      "event_id": "uuid",
      "table_fqn": "aegisDB.aegisdb.public.orders",
      "table_name": "orders",
      "failure_categories": ["null_violation"],
      "root_cause": "The 'customer_id' column contains null values...",
      "confidence": 0.87,
      "fix_sql": "DELETE FROM {table} WHERE customer_id IS NULL;",
      "fix_description": "Delete rows where customer_id is null.",
      "sandbox_passed": true,
      "rows_affected": 2,
      "status": "pending_approval",
      "created_at": "2026-04-20T07:01:59.328506+00:00",
      "decided_at": null,
      "rejection_reason": null
    }
  ]
}
```

> **Note:** `fix_sql` contains `{table}` placeholder in the summary. The placeholder is substituted during actual execution in `apply.py`.

---

#### `GET /proposals?status={s}&limit={n}`

Lists proposals filtered by status. Max: 100.

---

#### `GET /proposals/{proposal_id}`

Returns full proposal detail including sandbox preview data (`sample_before`, `sample_after`, `rows_before`, `rows_after`) and serialized `diagnosis_json` and `event_json`.

**Additional fields vs summary:**

| Field | Type | Notes |
|---|---|---|
| `rollback_sql` | string \| null | Provided if LLM generated one |
| `estimated_rows` | integer \| null | LLM estimate |
| `rows_before` | integer | Sandbox row count before fix |
| `rows_after` | integer | Sandbox row count after fix |
| `sample_before` | array | Up to 5 rows before |
| `sample_after` | array | Up to 5 rows after |
| `decision_by` | string | `system` or user identifier |
| `diagnosis_json` | string | Serialized `DiagnosisResult` |
| `event_json` | string | Serialized `EnrichedFailureEvent` |

---

#### `POST /proposals/{proposal_id}/approve`

Approves a proposal. Publishes `DiagnosisResult` to `aegisdb:repair` stream, triggering the repair → sandbox → apply pipeline.

**Request body:**
```json
{ "decided_by": "admin" }
```

**Validation:** Proposal must have `status=pending_approval`. Returns 409 if already decided.

**Response:**
```json
{
  "proposal_id": "uuid",
  "status": "executing",
  "message": "Approved. Fix is executing. DRY_RUN=ON. Check GET /api/v1/audit for results.",
  "repair_msg_id": "1776670741126-0",
  "dry_run": true
}
```

---

#### `POST /proposals/{proposal_id}/reject`

Rejects a proposal. No fix is applied. Publishes to escalation stream.

**Request body:**
```json
{ "reason": "False positive", "decided_by": "admin" }
```

`reason` is required. Returns 400 if missing.

**Response:**
```json
{
  "proposal_id": "uuid",
  "status": "rejected",
  "reason": "False positive",
  "message": "Proposal rejected. No changes made to the database."
}
```

---

#### `POST /proposals/{proposal_id}/re-sandbox`

Re-runs sandbox on an existing proposal to get a fresh data diff. Updates `sandbox_passed`, `rows_before`, `rows_after`, `sample_before`, `sample_after` in the DB.

Only valid when `status=pending_approval` or `status=failed`.

---

## 4. Business Logic

### 4.1 Full Pipeline Flow

```
OpenMetadata test failure
    │
    ▼
POST /api/v1/webhook/om-test-failure
    │  (returns 200 immediately)
    │
    ▼ (background task)
om_client.get_table_context(table_fqn)
    │  → calls OM API: GET /api/v1/tables/name/{fqn}?fields=columns,...
    │  → calls OM API: GET /api/v1/lineage/table/{id}
    │
    ▼
event_store.write_event(enriched_event)
    │  → writes to _aegisdb_events
    │
    ▼
event_bus.publish(enriched_event)
    │  → XADD aegisdb:events
    │
    ▼ (stream_consumer reads aegisdb:events)
detector.run_detector(event)
    │  → rule-based classification into FailureCategory enum
    │  → computes severity, is_actionable flag
    │
    ▼
diagnosis_agent.run(event, detector_result)
    │  → vector_store.find_similar_fixes() — RAG from ChromaDB
    │  → Groq LLM call (llama-3.3-70b-versatile)
    │  → parses JSON response into DiagnosisResult
    │  → confidence >= 0.70 → is_repairable=True
    │
    ├── confidence < 0.70 → publish to aegisdb:escalation
    │
    └── confidence >= 0.70 →
            run_sandbox(event, diagnosis)  [preview run]
                │  → fetch schema + 500 rows from aegisdb_postgres
                │  → spin up ephemeral postgres:16-alpine via testcontainers
                │  → seed schema + rows
                │  → execute fix_sql with {table} substituted
                │  → run SQL assertions (validator.py)
                │  → capture data diff (sample_before, sample_after)
                │
                ▼
            proposal_store.create_proposal(proposal)
                │  → writes to _aegisdb_proposals (status=pending_approval)
                │
                ▼
            WAIT FOR USER DECISION (poll GET /proposals/pending)
                │
                ├── POST /proposals/{id}/reject
                │       → status=rejected, publish to escalation
                │
                └── POST /proposals/{id}/approve
                        → publish DiagnosisResult to aegisdb:repair
                        │
                        ▼ (repair_agent reads aegisdb:repair)
                    run_sandbox(event, diagnosis)  [full validation run]
                        │  → retry up to 3 times
                        │  → sandbox passed → store fix in ChromaDB KB
                        │
                        ▼
                    publish to aegisdb:apply
                        │
                        ▼ (apply_agent reads aegisdb:apply)
                    DRY_RUN=true  → log intent, write audit row (action=dry_run)
                    DRY_RUN=false →
                        SET LOCAL statement_timeout = 30000
                        execute fix_sql (with {table} substituted)
                        run post-apply assertions
                        assertions pass  → COMMIT → audit row (action=applied)
                        assertions fail  → ROLLBACK → audit row (action=rolled_back)
                                         → publish to escalation
```

### 4.2 Detector Classification Rules

Keyword-to-category mapping (evaluated in order, first match wins):

| Keywords | FailureCategory |
|---|---|
| `not_null`, `null`, `not be null`, `null values` | `NULL_VIOLATION` |
| `between`, `range`, `min`, `max`, `negative`, `greater`, `less` | `RANGE_VIOLATION` |
| `unique`, `duplicate`, `distinct` | `UNIQUENESS_VIOLATION` |
| `foreign key`, `referential`, `fk`, `not exist` | `REFERENTIAL_INTEGRITY` |
| `regex`, `format`, `pattern`, `like`, `email`, `phone` | `FORMAT_VIOLATION` |
| `column not found`, `schema`, `missing column`, `type mismatch` | `SCHEMA_DRIFT` |

Severity escalation rules:
- `REFERENTIAL_INTEGRITY` or `SCHEMA_DRIFT` → CRITICAL (not actionable for SCHEMA_DRIFT)
- Column name in `{customer_id, order_id, user_id, id, amount, email, status}` → HIGH
- Multiple categories → MEDIUM
- Everything else → base severity from webhook

`SCHEMA_DRIFT` and all-`UNKNOWN` categories set `is_actionable=False` → skip LLM, go directly to escalation.

### 4.3 Profiler Detection Logic

The profiler connects directly to target Postgres via asyncpg. Runs the following checks per column:

| Check | Triggered when | Method |
|---|---|---|
| Null rate | All columns | COUNT WHERE col IS NULL |
| High null rate warning | nullable=true and rate > 80% | Same query |
| Uniqueness | Column name contains any of: `id`, `uuid`, `email`, `username`, `phone`, `ssn`, etc. | GROUP BY HAVING COUNT > 1 |
| Range/outlier | Numeric types (int, bigint, numeric, etc.) | IQR method (Q1 - 1.5×IQR, Q3 + 1.5×IQR) |
| Negative values | Numeric + column name contains: `amount`, `price`, `cost`, `fee`, `total`, `balance`, `quantity` | WHERE col < 0 |
| Email format | Text types + column name in: `email`, `email_address`, `mail` | Regex `^[^@\s]+@[^@\s]+\.[^@\s]+$` |
| Referential integrity | Columns with FK constraints (from `information_schema`) | LEFT JOIN check |

Row cap: Tables with > 100,000 rows are sampled. `_aegisdb_*` tables are excluded.

### 4.4 OpenMetadata Integration

**Authentication:** OM 1.6 requires base64-encoded password in login body.  
Login endpoint: `POST /api/v1/users/login`  
Token field in response: `accessToken` (not `jwtToken` as in older versions)  
Token validation: On each request, a `GET /api/v1/system/version` ping verifies token is still valid before proceeding. Cache is invalidated on 401/non-200.

**Table FQN format in OM:** `{service_name}.{database}.{schema}.{table}`  
Example: `aegisDB.aegisdb.public.orders`

**URL encoding:** Dots in FQN are encoded as `%2E` when used in GET path params.

**In-process ingestion:** Uses `MetadataWorkflow.create(config).execute()` from `openmetadata-ingestion` package. Runs entirely in-process via `asyncio.to_thread`. Does not use Airflow or the OM deploy/trigger API endpoints (which have a known race condition per GitHub issue #23985).

**OM service registration note:** The OM service name must match the FQN prefix used in webhook payloads. In the current test setup, the FQN prefix is `aegisDB` (capital B) due to how the service was originally registered. The service registered programmatically via `POST /connect` uses the auto-generated name `aegisdb_{host}_{database}_{uuid8}`.

### 4.5 Sandbox Execution

- Image: `postgres:16-alpine`
- Managed by: `testcontainers-python 4.14.2` (ryuk sidecar for cleanup)
- Run in: `asyncio.to_thread` to avoid blocking the event loop
- Timeout: 120 seconds (configurable via `SANDBOX_TIMEOUT_SECONDS`)
- Retries: Up to 3 attempts (configurable via `SANDBOX_MAX_RETRIES`)
- Connection: psycopg2 uses `postgresql://` scheme. testcontainers returns `postgresql+psycopg2://` — stripped with `_to_psycopg2_url()` helper.
- Assertions: Run in a new event loop created inside the thread via `asyncio.new_event_loop()`
- Seed data: Schema reconstructed from `information_schema`, up to 500 rows sampled from production (configurable via `SANDBOX_SAMPLE_ROWS`)
- Postgres readiness: Retried up to 10 times with 2-second sleep between attempts

### 4.6 Apply Agent — Critical Detail

`{table}` placeholder in `fix_sql` is substituted in `apply.py` before execution:

```python
fix_sql = decision.fix_sql.replace("{table}", f'"{table_name}"')
```

This substitution is done in both `_apply_live()` and `_dry_run()`. The placeholder is preserved in storage (proposals, repair stream messages) and only resolved at execution time.

### 4.7 Knowledge Base (ChromaDB)

- On startup: `seed_bootstrap_fixes()` seeds 5 generic fix templates if collection is empty
- On successful sandbox pass (repair agent): Fix is stored with `was_successful="True"`
- Retrieval: Top-3 similar fixes by cosine similarity (threshold: 0.3 minimum similarity)
- Used by: Diagnosis agent as RAG context in the LLM prompt

---

## 5. Environment Variables

All variables loaded from `.env` in project root via `pydantic-settings`.

| Variable | Default | Required | Notes |
|---|---|---|---|
| `OM_HOST` | `http://localhost:8585` | Yes | OpenMetadata server URL |
| `OM_ADMIN_EMAIL` | `admin@open-metadata.org` | Yes | |
| `OM_ADMIN_PASSWORD` | — | Yes | Plain text — base64-encoded at runtime in `om_client.py` |
| `REDIS_HOST` | `localhost` | Yes | |
| `REDIS_PORT` | `6379` | Yes | |
| `REDIS_STREAM_NAME` | `aegisdb:events` | Yes | |
| `REDIS_REPAIR_STREAM` | `aegisdb:repair` | Yes | |
| `REDIS_ESCALATION_STREAM` | `aegisdb:escalation` | Yes | |
| `REDIS_APPLY_STREAM` | `aegisdb:apply` | Yes | |
| `REDIS_CONSUMER_GROUP` | `aegisdb-agents` | Yes | Shared across all agents |
| `REDIS_CONSUMER_NAME` | `diagnosis-agent-1` | Yes | |
| `APP_HOST` | `0.0.0.0` | No | |
| `APP_PORT` | `8000` | No | |
| `WEBHOOK_SECRET` | — | No | Currently unused — no signature validation |
| `GROQ_API_KEY` | — | Yes | Groq API key |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | Yes | |
| `LLM_MAX_TOKENS` | `4096` | No | |
| `CHROMA_PERSIST_DIR` | `./data/chromadb` | No | |
| `CHROMA_COLLECTION` | `aegisdb_fixes` | No | |
| `CONFIDENCE_THRESHOLD` | `0.70` | No | Min confidence to mark as repairable |
| `TARGET_DB_HOST` | `localhost` | Yes | |
| `TARGET_DB_PORT` | `5433` | Yes | External port mapping |
| `TARGET_DB_NAME` | `aegisdb` | Yes | |
| `TARGET_DB_USER` | `aegisdb_user` | Yes | |
| `TARGET_DB_PASSWORD` | `aegisdb_pass` | Yes | |
| `SANDBOX_MAX_RETRIES` | `3` | No | |
| `SANDBOX_SAMPLE_ROWS` | `500` | No | |
| `SANDBOX_TIMEOUT_SECONDS` | `120` | No | |
| `DRY_RUN` | `true` | No | Runtime-togglable via `POST /dry-run/toggle` |
| `APPLY_STATEMENT_TIMEOUT_MS` | `30000` | No | |
| `POST_APPLY_VERIFY` | `true` | No | Run assertions after live apply |

---

## 6. Boot Sequence

The FastAPI lifespan function initialises components in this order. Each step is independent — failure is non-fatal except where noted.

```
1. vector_store.connect()          → ChromaDB (sync, must be first)
   vector_store.seed_bootstrap_fixes()

2. init_audit_table()              → _aegisdb_audit
   init_event_store()              → _aegisdb_events
   init_profiling_store()          → _aegisdb_profiling_reports
   init_connection_registry()      → _aegisdb_connections
   init_proposal_store()           → _aegisdb_proposals

3. event_bus.connect()             → Redis ping

4. stream_consumer.connect()       → Create consumer group (idempotent)
   asyncio.create_task(stream_consumer.start())

5. repair_agent.connect()          → Create consumer group
   asyncio.create_task(repair_agent.start())

6. apply_agent.connect()           → Create consumer group
   asyncio.create_task(apply_agent.start())
```

Expected boot log (all components healthy):
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

---

## 7. Known Gaps and Constraints

### 7.1 No Authentication Layer

No API authentication is implemented. All endpoints are publicly accessible. There is no JWT, API key, or session validation.

### 7.2 No Credential Storage

Database credentials submitted via `POST /connect` are not persisted. Only `host:port/db` is stored as `connection_hint`. Re-profiling an existing connection requires re-submitting credentials.

### 7.3 `WEBHOOK_SECRET` Unused

The `WEBHOOK_SECRET` env variable is defined but OpenMetadata webhook payloads are not signature-validated.

### 7.4 OM FQN Mismatch

The FQN prefix in webhook payloads is `aegisDB` (capital B) due to the service being registered with that capitalisation during initial setup. The `POST /connect` endpoint generates a different service name pattern (`aegisdb_{host}_{db}_{uuid}`). These two registration paths produce different FQN prefixes. Frontend must handle both.

### 7.5 Repair Agent Event Reconstruction Fallback

When the repair agent looks up the enriched event from `_aegisdb_events` by `event_id` and the record is missing (e.g. event store was unavailable at write time), it falls back to `_fallback_event_from_diagnosis()` which reconstructs column names from a hardcoded category-to-column mapping. This fallback produces incorrect column names for tables that are not `orders`.

### 7.6 Apply Agent `_extract_failed_tests` Limitation

`_extract_failed_tests()` in `apply.py` reconstructs `FailedTest` objects from the serialized `diagnosis_json` using a hardcoded `_col_map`. This is only used for post-apply assertion mapping. The mapping is specific to the test schema (`customer_id`, `amount`, `email`) and will produce wrong assertions for arbitrary schemas.

### 7.7 Validator Assertion Coverage

`validator.py` supports: null checks, between/range checks, uniqueness, and email regex. The `format_violation` and `schema_drift` categories have no implemented assertions and return `passed=True` with a `skipped` note. Bounds for range assertions are hardcoded to `[0, 9_999_999]`.

### 7.8 Sandbox Windows Compatibility

testcontainers on Windows (Docker Desktop with named pipes) occasionally produces Docker API 500 errors when container removal coincides with the event loop. Retry logic (3 attempts, 2-second gap) mitigates this. The `TESTCONTAINERS_HOST_OVERRIDE=localhost` env var may be required on some Windows Docker Desktop configurations.

### 7.9 Profiling Row Limit

Large tables (> 100,000 rows) are sampled for per-column queries but the row count itself is exact. The `row_sample_limit` parameter defaults to 100,000. IQR-based outlier detection may be less accurate on sampled data.

### 7.10 Unicode in Sample Values

`_get_dupe_samples()` in `profiler.py` uses `×` (multiplication sign U+00D7) in the format string `f"{val} (×{count})"`. This character may not render correctly in some terminals or JSON parsers. Non-blocking cosmetic issue.

### 7.11 ChromaDB File Lock on Windows

ChromaDB holds file locks on its binary store files while the server is running. `Remove-Item` fails on these files without stopping the server first. Required cleanup procedure: stop server → delete `./data/chromadb/` → restart.

### 7.12 List Connections Bug

`list_connections()` in `connection_registry.py` has a scoping bug — the `out.append(d)` call is indented inside the `for f in (...)` loop rather than outside it, causing only the last item to be appended per row. This means the list endpoint returns connections with only the last datetime field processed correctly. Non-critical for single-connection scenarios.

---

## 8. Complete API Route Summary

| Method | Route | Phase | Description |
|---|---|---|---|
| POST | `/api/v1/webhook/om-test-failure` | 2 | Receive OM test failure |
| GET | `/api/v1/health` | 2 | Basic health check |
| GET | `/api/v1/status` | 5 | Full pipeline status |
| GET | `/api/v1/audit` | 5 | Audit log |
| GET | `/api/v1/streams` | 5 | Redis stream lengths |
| GET | `/api/v1/escalations` | 5 | Escalation queue |
| POST | `/api/v1/dry-run/toggle` | 5 | Toggle live/safe mode |
| POST | `/api/v1/profile` | B | Profile a database |
| GET | `/api/v1/profile/{report_id}` | B | Full profiling report |
| GET | `/api/v1/profile/{report_id}/anomalies` | B | Filtered anomaly list |
| GET | `/api/v1/profiles` | B | List profiling reports |
| POST | `/api/v1/connect` | C | Connect and onboard a database |
| GET | `/api/v1/connections/{connection_id}` | C | Poll onboarding status |
| GET | `/api/v1/connections` | C | List all connections |
| POST | `/api/v1/connections/{id}/re-profile` | C | PARTIAL — returns 400, use /connect |
| GET | `/api/v1/proposals/pending` | D | Pending approval queue |
| GET | `/api/v1/proposals` | D | All proposals (filterable by status) |
| GET | `/api/v1/proposals/{proposal_id}` | D | Full proposal with diff |
| POST | `/api/v1/proposals/{proposal_id}/approve` | D | Approve and execute fix |
| POST | `/api/v1/proposals/{proposal_id}/reject` | D | Reject fix |
| POST | `/api/v1/proposals/{proposal_id}/re-sandbox` | D | Refresh sandbox preview |
