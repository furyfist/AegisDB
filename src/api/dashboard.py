import logging
from fastapi import APIRouter, HTTPException, Query
import redis.asyncio as aioredis

from src.core.config import settings
from src.db.audit_log import fetch_recent_audit

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/audit")
async def get_audit_log(limit: int = Query(default=20, le=100)):
    """
    Returns the most recent audit log rows from the target DB.
    Shows the full decision trail: dry_run → applied → rolled_back.
    """
    rows = await fetch_recent_audit(limit=limit)

    # Serialize datetime for JSON
    for row in rows:
        if "applied_at" in row and hasattr(row["applied_at"], "isoformat"):
            row["applied_at"] = row["applied_at"].isoformat()
        if "failure_categories" in row and row["failure_categories"] is None:
            row["failure_categories"] = []

    return {
        "count": len(rows),
        "entries": rows,
    }


@router.get("/streams")
async def get_stream_stats():
    """
    Returns message counts across all Redis streams.
    Shows pipeline throughput at a glance.
    """
    try:
        r = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
        streams = {
            "aegisdb:events": settings.redis_stream_name,
            "aegisdb:repair": settings.redis_repair_stream,
            "aegisdb:apply": settings.redis_apply_stream,
            "aegisdb:escalation": settings.redis_escalation_stream,
        }

        stats = {}
        for label, stream_key in streams.items():
            try:
                length = await r.xlen(stream_key)
                # Peek at the last message timestamp
                last = await r.xrevrange(stream_key, count=1)
                last_event_id = last[0][0] if last else None
            except Exception:
                length = 0
                last_event_id = None
            stats[label] = {
                "length": length,
                "last_message_id": last_event_id,
            }

        await r.aclose()
        return {"streams": stats}

    except Exception as e:
        logger.error(f"[Dashboard] Stream stats error: {e}")
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")


@router.get("/escalations")
async def get_escalations(limit: int = Query(default=20, le=100)):
    """
    Returns items currently in the escalation stream.
    These are events that AegisDB couldn't auto-heal — need human review.
    """
    try:
        r = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
        messages = await r.xrevrange(
            settings.redis_escalation_stream, count=limit
        )
        await r.aclose()

        results = []
        for msg_id, fields in messages:
            results.append({
                "message_id": msg_id,
                "event_id": fields.get("event_id"),
                "table_fqn": fields.get("table_fqn"),
                "reason": fields.get("reason"),
                "stage": fields.get("stage"),
                "escalated_at": fields.get("escalated_at"),
            })

        return {"count": len(results), "escalations": results}

    except Exception as e:
        logger.error(f"[Dashboard] Escalations error: {e}")
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")


@router.post("/dry-run/toggle")
async def toggle_dry_run():
    """
    Runtime dry-run toggle without restart.
    In production this would be gated behind auth.
    For demo: flip DRY_RUN at runtime.
    """
    settings.dry_run = not settings.dry_run
    mode = "DRY RUN" if settings.dry_run else "LIVE"
    logger.warning(f"[Dashboard] Dry-run toggled → {mode}")
    return {
        "dry_run": settings.dry_run,
        "message": f"Apply agent now in {mode} mode",
    }

@router.get("/tables/live")
async def get_live_table_data(
    connection_id: str,
    table_name: str,
    schema: str = "public",
    limit: int = 100,
):
    """
    Returns live rows from a connected table.
    Anomaly columns sourced from latest profiling report.
    Column metadata from information_schema.
    """
    from src.db.connection_registry import get_connection
    from src.db.profiling_store import get_report as get_profiling_report
    import asyncpg

    if limit > 500:
        limit = 500

    # Get connection record (has connection_hint + profiling_report_id)
    conn_record = await get_connection(connection_id)
    if not conn_record:
        raise HTTPException(status_code=404, detail="Connection not found")

    # We need credentials — not stored, so we read from env as fallback
    # For connected target DB, use TARGET_DB_* env vars
    from src.core.config import settings
    connection_url = (
        f"postgresql://{settings.target_db_user}:{settings.target_db_password}"
        f"@{settings.target_db_host}:{settings.target_db_port}/{conn_record['db_name']}"
    )

    try:
        conn = await asyncpg.connect(connection_url, timeout=10)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {str(e)}")

    try:
        # 1. Column metadata from information_schema
        col_rows = await conn.fetch("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position
        """, schema, table_name)

        columns = [
            {
                "name":     r["column_name"],
                "type":     r["data_type"],
                "nullable": r["is_nullable"] == "YES",
            }
            for r in col_rows
        ]

        if not columns:
            raise HTTPException(
                status_code=404,
                detail=f"Table '{schema}.{table_name}' not found"
            )

        # 2. Total row count
        total_rows = await conn.fetchval(
            f'SELECT COUNT(*) FROM "{schema}"."{table_name}"'
        )

        # 3. Live rows
        rows_raw = await conn.fetch(
            f'SELECT * FROM "{schema}"."{table_name}" LIMIT $1', limit
        )
        rows = [dict(r) for r in rows_raw]

        # Serialize non-JSON-safe types (dates, decimals, etc.)
        import datetime, decimal
        def _serialize(v):
            if isinstance(v, (datetime.date, datetime.datetime)):
                return v.isoformat()
            if isinstance(v, decimal.Decimal):
                return float(v)
            return v

        rows = [
            {k: _serialize(v) for k, v in row.items()}
            for row in rows
        ]

    finally:
        await conn.close()

    # 4. Anomaly columns from latest profiling report
    anomaly_columns: list[str] = []
    if conn_record.get("profiling_report_id"):
        try:
            report = await get_profiling_report(conn_record["profiling_report_id"])
            if report:
                for t in (report.get("tables") or []):
                    if t.get("table_name") == table_name:
                        anomaly_columns = [
                            a["column_name"]
                            for a in (t.get("anomalies") or [])
                        ]
                        break
        except Exception:
            pass  # Non-fatal — just won't highlight columns

    return {
        "table_name":      table_name,
        "schema":          schema,
        "columns":         columns,
        "rows":            rows,
        "total_rows":      total_rows,
        "returned_rows":   len(rows),
        "anomaly_columns": anomaly_columns,
        "last_profiled_at": conn_record.get("last_profiled_at"),
    }

@router.get("/connections/{connection_id}/health")
async def get_connection_health(connection_id: str):
    """
    Real-time health score for a connected database.
    Score computed from profiling report anomaly counts.
    """
    from src.db.connection_registry import get_connection
    from src.db.profiling_store import get_report as get_profiling_report
    import asyncpg
    from src.core.config import settings

    conn_record = await get_connection(connection_id)
    if not conn_record:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Compute health score
    critical = conn_record.get("critical_count") or 0
    warnings = (conn_record.get("total_anomalies") or 0) - critical
    warnings = max(0, warnings)

    if (conn_record.get("total_anomalies") or 0) == 0:
        health_score = 100
        status_label = "clean"
    elif critical == 0:
        health_score = max(50, 100 - (warnings * 3))
        status_label = "partial"
    else:
        health_score = max(0, 100 - (critical * 10) - (warnings * 3))
        status_label = "dirty"

    # Per-table breakdown from profiling report
    tables_summary = []
    if conn_record.get("profiling_report_id"):
        try:
            report = await get_profiling_report(conn_record["profiling_report_id"])
            if report:
                for t in (report.get("tables") or []):
                    anomaly_count = len(t.get("anomalies") or [])
                    tables_summary.append({
                        "table_name":    t.get("table_name"),
                        "schema":        t.get("schema_name", "public"),
                        "anomaly_count": anomaly_count,
                        "status":        "dirty" if anomaly_count > 0 else "clean",
                    })
        except Exception:
            pass

    return {
        "connection_id":     connection_id,
        "health_score":      health_score,
        "status":            status_label,
        "total_anomalies":   conn_record.get("total_anomalies") or 0,
        "critical_count":    critical,
        "warning_count":     warnings,
        "tables_found":      conn_record.get("tables_found") or 0,
        "tables":            tables_summary,
        "last_profiled_at":  conn_record.get("last_profiled_at"),
        "db_name":           conn_record.get("db_name"),
        "connection_hint":   conn_record.get("connection_hint"),
    }

@router.get("/connections/{connection_id}/health")
async def get_connection_health(connection_id: str):
    from src.db.connection_registry import get_connection
    from src.db.profiling_store import get_report as get_profiling_report

    conn_record = await get_connection(connection_id)
    if not conn_record:
        raise HTTPException(status_code=404, detail="Connection not found")

    critical = conn_record.get("critical_count") or 0
    total    = conn_record.get("total_anomalies") or 0
    warnings = max(0, total - critical)

    if total == 0:
        health_score = 100
        status_label = "clean"
    elif critical == 0:
        health_score = max(50, 100 - (warnings * 3))
        status_label = "partial"
    else:
        health_score = max(0, 100 - (critical * 10) - (warnings * 3))
        status_label = "dirty"

    tables_summary = []
    if conn_record.get("profiling_report_id"):
        try:
            report = await get_profiling_report(conn_record["profiling_report_id"])
            if report:
                for t in (report.get("tables") or []):
                    anomaly_count = len(t.get("anomalies") or [])
                    tables_summary.append({
                        "table_name":    t.get("table_name"),
                        "schema":        t.get("schema_name", "public"),
                        "anomaly_count": anomaly_count,
                        "status":        "dirty" if anomaly_count > 0 else "clean",
                    })
        except Exception:
            pass

    return {
        "connection_id":    connection_id,
        "health_score":     health_score,
        "status":           status_label,
        "total_anomalies":  total,
        "critical_count":   critical,
        "warning_count":    warnings,
        "tables_found":     conn_record.get("tables_found") or 0,
        "tables":           tables_summary,
        "last_profiled_at": conn_record.get("last_profiled_at"),
        "db_name":          conn_record.get("db_name"),
        "connection_hint":  conn_record.get("connection_hint"),
    }