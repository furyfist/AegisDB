# AegisDB API Reference

This document covers the public HTTP endpoints currently exposed by the AegisDB FastAPI service.

## Base URL

All routes are mounted under:

```text
/api/v1
```

Example local server:

```text
http://localhost:8000/api/v1
```

## Conventions

- Content type: `application/json`
- Authentication: none implemented yet
- Database support: onboarding and direct profiling currently validate Postgres connections
- IDs such as `connection_id`, `report_id`, and `proposal_id` are UUID strings
- Timestamps are returned as ISO 8601 strings where applicable

## Common Error Shapes

Most validation and not-found failures use FastAPI's standard error payload:

```json
{
  "detail": "Human-readable error message"
}
```

Some operational dashboard endpoints may return:

```json
{
  "error": "Backend dependency failure details"
}
```

## Status Values

### Connection Status

- `pending`
- `ingesting`
- `profiling`
- `ready`
- `failed`

### Proposal Status

- `pending_approval`
- `approved`
- `rejected`
- `executing`
- `completed`
- `failed`

### Profiling Anomaly Severity

- `info`
- `warning`
- `critical`

## Endpoints

### POST `/connect`

Onboard a database into AegisDB. The API validates the credentials immediately, then runs metadata registration, ingestion, and profiling in the background.

#### Request Body

```json
{
  "host": "localhost",
  "port": 5432,
  "database": "sales",
  "username": "postgres",
  "password": "secret",
  "schemas": ["public"],
  "service_name": "sales-prod"
}
```

#### Success Response `200`

```json
{
  "connection_id": "4f28f365-6f0b-4582-b2f2-0d892c16d8fb",
  "service_name": "sales-prod",
  "connection_hint": "localhost:5432/sales",
  "status": "pending",
  "tables_found": 0,
  "total_anomalies": 0,
  "critical_count": 0,
  "profiling_report_id": null,
  "message": "Connection validated. Onboarding started in background. Poll GET /api/v1/connections/4f28f365-6f0b-4582-b2f2-0d892c16d8fb for status."
}
```

#### Error Responses

- `400` if the database cannot be reached or credentials are invalid

#### Notes

- Credentials are used for validation and background onboarding but are not persisted.
- Use `GET /api/v1/connections/{connection_id}` to track progress.

### GET `/connections`

List onboarded databases.

#### Success Response `200`

```json
{
  "count": 1,
  "connections": [
    {
      "connection_id": "4f28f365-6f0b-4582-b2f2-0d892c16d8fb",
      "service_name": "sales-prod",
      "connection_hint": "localhost:5432/sales",
      "db_name": "sales",
      "status": "ready",
      "tables_found": 12,
      "total_anomalies": 7,
      "critical_count": 2,
      "registered_at": "2026-04-20T11:20:05.120000+00:00",
      "last_profiled_at": "2026-04-20T11:21:11.420000+00:00"
    }
  ]
}
```

### GET `/connections/{connection_id}`

Poll onboarding state for a single database.

#### Path Parameters

- `connection_id`: UUID returned by `POST /connect`

#### Success Response `200`

```json
{
  "connection_id": "4f28f365-6f0b-4582-b2f2-0d892c16d8fb",
  "service_name": "sales-prod",
  "connection_hint": "localhost:5432/sales",
  "db_name": "sales",
  "schema_names": ["public"],
  "status": "ready",
  "om_service_fqn": "databaseService.sales-prod",
  "profiling_report_id": "b0be6842-f0fd-49a1-9f6e-cf743fc8979b",
  "tables_found": 12,
  "total_anomalies": 7,
  "critical_count": 2,
  "registered_at": "2026-04-20T11:20:05.120000+00:00",
  "last_profiled_at": "2026-04-20T11:21:11.420000+00:00",
  "error": null
}
```

#### Error Responses

- `404` if the connection ID does not exist

### POST `/profile`

Run an on-demand profile against a database connection string.

#### Request Body

```json
{
  "connection_url": "postgresql://postgres:secret@localhost:5432/sales",
  "schemas": ["public"],
  "table_limit": 50
}
```

#### Success Response `200`

```json
{
  "report_id": "b0be6842-f0fd-49a1-9f6e-cf743fc8979b",
  "connection_hint": "localhost:5432/sales",
  "status": "completed",
  "tables_scanned": 12,
  "total_anomalies": 7,
  "critical_count": 2,
  "warning_count": 5,
  "duration_ms": 8432,
  "message": "Found 7 anomalies across 12 tables (2 critical, 5 warnings)"
}
```

#### Error Responses

- `400` if `connection_url` does not start with `postgresql://` or `postgres://`
- `500` if profiling fails

#### Notes

- The current implementation performs profiling during the request, then stores the full report in the background.
- Use `GET /api/v1/profile/{report_id}` for the complete report.

### GET `/profiles`

List recent profiling reports.

#### Query Parameters

- `limit` optional integer, default `10`, maximum `50`

#### Success Response `200`

```json
{
  "count": 1,
  "reports": [
    {
      "report_id": "b0be6842-f0fd-49a1-9f6e-cf743fc8979b",
      "connection_hint": "localhost:5432/sales",
      "status": "completed",
      "tables_scanned": 12,
      "total_anomalies": 7,
      "critical_count": 2,
      "warning_count": 5,
      "duration_ms": 8432,
      "created_at": "2026-04-20T11:21:11.420000+00:00"
    }
  ]
}
```

### GET `/profile/{report_id}`

Fetch the full stored profiling report.

#### Path Parameters

- `report_id`: profiling report UUID

#### Success Response `200`

```json
{
  "report_id": "b0be6842-f0fd-49a1-9f6e-cf743fc8979b",
  "connection_hint": "localhost:5432/sales",
  "profiled_at": "2026-04-20T11:21:11.420000+00:00",
  "tables_scanned": 12,
  "total_anomalies": 7,
  "critical_count": 2,
  "warning_count": 5,
  "status": "completed",
  "error": null,
  "duration_ms": 8432,
  "tables": [
    {
      "table_name": "orders",
      "schema_name": "public",
      "total_rows": 1024,
      "total_columns": 8,
      "profiled_at": "2026-04-20T11:21:03.210000+00:00",
      "profiling_duration_ms": 752,
      "anomalies": [
        {
          "column_name": "customer_email",
          "anomaly_type": "format_violation",
          "severity": "warning",
          "affected_rows": 14,
          "total_rows": 1024,
          "rate": 0.0137,
          "description": "Invalid email format detected",
          "sample_values": ["abc", "missing-at-sign"]
        }
      ]
    }
  ]
}
```

#### Error Responses

- `404` if the report does not exist

### GET `/profile/{report_id}/anomalies`

Fetch anomalies from a profiling report with optional filters.

#### Path Parameters

- `report_id`: profiling report UUID

#### Query Parameters

- `severity` optional: `critical`, `warning`, or `info`
- `table_name` optional exact table name filter

#### Success Response `200`

```json
{
  "report_id": "b0be6842-f0fd-49a1-9f6e-cf743fc8979b",
  "total_matched": 1,
  "anomalies": [
    {
      "table": "orders",
      "schema": "public",
      "column": "customer_email",
      "anomaly_type": "format_violation",
      "severity": "warning",
      "affected_rows": 14,
      "total_rows": 1024,
      "rate": 0.0137,
      "description": "Invalid email format detected",
      "sample_values": ["abc", "missing-at-sign"]
    }
  ]
}
```

#### Error Responses

- `404` if the report does not exist

### GET `/proposals/pending`

Return repair proposals waiting for user approval.

#### Success Response `200`

```json
{
  "count": 1,
  "proposals": [
    {
      "proposal_id": "72e2a94d-5fc0-4a63-87f7-fda7d0dab92d",
      "event_id": "02110c8c-2ed3-44f4-9fe4-98ebf6b5d482",
      "table_fqn": "sales.public.orders",
      "table_name": "orders",
      "failure_categories": ["format_violation"],
      "root_cause": "Malformed email addresses were inserted without validation.",
      "confidence": 0.94,
      "fix_sql": "UPDATE public.orders SET customer_email = NULL WHERE customer_email !~* '^[^@]+@[^@]+\\.[^@]+$';",
      "fix_description": "Null out malformed email values so downstream checks pass.",
      "sandbox_passed": true,
      "rows_affected": 14,
      "status": "pending_approval",
      "created_at": "2026-04-20T11:28:34.118000+00:00",
      "decided_at": null,
      "rejection_reason": null
    }
  ]
}
```

### GET `/proposals/{proposal_id}`

Fetch the full proposal detail, including sandbox preview information and serialized pipeline context.

#### Path Parameters

- `proposal_id`: proposal UUID

#### Success Response `200`

Returns the full `RepairProposalRecord` object. In addition to summary fields, this includes:

- `rollback_sql`
- `estimated_rows`
- `rows_before`
- `rows_after`
- `sample_before`
- `sample_after`
- `decision_by`
- `diagnosis_json`
- `event_json`

#### Error Responses

- `404` if the proposal does not exist

### POST `/proposals/{proposal_id}/approve`

Approve a pending proposal and queue it for repair execution.

#### Path Parameters

- `proposal_id`: proposal UUID

#### Request Body

```json
{
  "reason": "Looks safe",
  "decided_by": "ops-user"
}
```

`reason` is optional for approval. `decided_by` defaults to `"user"` if omitted.

#### Success Response `200`

```json
{
  "proposal_id": "72e2a94d-5fc0-4a63-87f7-fda7d0dab92d",
  "status": "executing",
  "message": "Approved. Fix is executing. DRY_RUN=ON. Check GET /api/v1/audit for results.",
  "repair_msg_id": "1745148412345-0",
  "dry_run": true
}
```

#### Error Responses

- `404` if the proposal does not exist
- `409` if the proposal is not in `pending_approval`
- `422` if the proposal does not have diagnosis data
- `500` if publishing the repair job fails

### POST `/proposals/{proposal_id}/reject`

Reject a pending proposal and publish an escalation event for human review tracking.

#### Path Parameters

- `proposal_id`: proposal UUID

#### Request Body

```json
{
  "reason": "This needs a manual data fix instead.",
  "decided_by": "ops-user"
}
```

#### Success Response `200`

```json
{
  "proposal_id": "72e2a94d-5fc0-4a63-87f7-fda7d0dab92d",
  "status": "rejected",
  "reason": "This needs a manual data fix instead.",
  "message": "Proposal rejected. No changes made to the database."
}
```

#### Error Responses

- `400` if `reason` is missing
- `404` if the proposal does not exist
- `409` if the proposal is not in `pending_approval`

### GET `/audit`

Return recent fix execution history from the audit log.

#### Query Parameters

- `limit` optional integer, default `20`, maximum `100`

#### Success Response `200`

```json
{
  "count": 1,
  "entries": [
    {
      "id": 101,
      "event_id": "02110c8c-2ed3-44f4-9fe4-98ebf6b5d482",
      "table_fqn": "sales.public.orders",
      "table_name": "orders",
      "action": "dry_run",
      "rows_affected": 14,
      "dry_run": true,
      "sandbox_passed": true,
      "confidence": 0.94,
      "failure_categories": ["format_violation"],
      "applied_at": "2026-04-20T11:30:01.991000+00:00",
      "error": null
    }
  ]
}
```

### GET `/streams`

Return Redis stream metrics for pipeline monitoring.

#### Success Response `200`

```json
{
  "streams": {
    "aegisdb:events": {
      "length": 12,
      "last_message_id": "1745148412345-0"
    },
    "aegisdb:repair": {
      "length": 3,
      "last_message_id": "1745148420021-0"
    },
    "aegisdb:apply": {
      "length": 3,
      "last_message_id": "1745148428315-0"
    },
    "aegisdb:escalation": {
      "length": 1,
      "last_message_id": "1745148399012-0"
    }
  }
}
```

#### Failure Mode

If Redis cannot be reached, the endpoint returns:

```json
{
  "error": "connection failure details"
}
```

### GET `/status`

Return service version, dry-run mode, confidence threshold, and pipeline health.

#### Success Response `200`

```json
{
  "version": "0.4.0",
  "dry_run": true,
  "confidence_threshold": 0.85,
  "pipeline": {
    "1_webhook": "ok",
    "2_event_bus": "ok",
    "3_diagnosis_consumer": "ok",
    "4_repair_agent": "ok",
    "5_apply_agent": "ok"
  }
}
```

### GET `/escalations`

Return recent items from the escalation stream that need human review.

#### Query Parameters

- `limit` optional integer, default `20`, maximum `100`

#### Success Response `200`

```json
{
  "count": 1,
  "escalations": [
    {
      "message_id": "1745148399012-0",
      "event_id": "02110c8c-2ed3-44f4-9fe4-98ebf6b5d482",
      "table_fqn": "sales.public.orders",
      "reason": "Rejected by user: This needs a manual data fix instead.",
      "stage": "approval",
      "escalated_at": null
    }
  ]
}
```

#### Failure Mode

If Redis cannot be reached, the endpoint returns:

```json
{
  "error": "connection failure details"
}
```

### POST `/dry-run/toggle`

Toggle the apply pipeline between safe dry-run mode and live mode at runtime.

#### Request Body

No body required.

#### Success Response `200`

```json
{
  "dry_run": false,
  "message": "Apply agent now in LIVE mode"
}
```

#### Notes

- This endpoint flips the current in-memory setting each time it is called.
- There is no authentication guard yet, so it should be protected before production use.

## Related Endpoints

These are implemented as well, even though they are outside the core list above:

- `GET /api/v1/proposals` to list all proposals with optional `status` and `limit`
- `POST /api/v1/connections/{connection_id}/re-profile` to request a fresh profile, which currently returns a guidance error because credentials are not stored
- FastAPI OpenAPI docs at `/docs`
