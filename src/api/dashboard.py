import logging
from fastapi import APIRouter, HTTPException, Query
import redis.asyncio as aioredis

from src.core.config import settings
from src.db.audit_log import fetch_recent_audit, fetch_audit_entry
from src.db.reports_store import (
    fetch_reports,
    fetch_report_by_id,
    fetch_report_stats,
)
import json

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/audit")
async def get_audit_log(limit: int = Query(default=20, le=100)):
    entries = await fetch_recent_audit(limit)
    
    serialized = []
    for e in entries:
        # post_apply_json is stored as a JSON string — parse it for the response
        post_apply = e.get("post_apply_json")
        if isinstance(post_apply, str):
            try:
                post_apply = json.loads(post_apply)
            except Exception:
                post_apply = []
        elif post_apply is None:
            post_apply = []

        serialized.append({
            "id":                  e.get("id"),
            "event_id":            e.get("event_id"),
            "table_fqn":           e.get("table_fqn"),
            "table_name":          e.get("table_name"),
            "action":              e.get("action"),
            "rows_affected":       e.get("rows_affected"),
            "dry_run":             e.get("dry_run"),
            "sandbox_passed":      e.get("sandbox_passed"),
            "confidence":          e.get("confidence"),
            "failure_categories":  e.get("failure_categories") or [],
            "applied_at":          e.get("applied_at"),
            "error":               e.get("error"),
            # New detail fields
            "fix_sql":             e.get("fix_sql"),
            "rollback_sql":        e.get("rollback_sql"),
            "post_apply_assertions": post_apply,
        })

    return {"count": len(serialized), "entries": serialized}


@router.get("/audit/{event_id}")
async def get_audit_entry(event_id: str):
    """
    Full detail for a single audit entry by event_id.
    Used by the frontend expandable row / detail view.
    """
    entry = await fetch_audit_entry(event_id)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"Audit entry for event_id={event_id} not found",
        )

    post_apply = entry.get("post_apply_json")
    if isinstance(post_apply, str):
        try:
            post_apply = json.loads(post_apply)
        except Exception:
            post_apply = []
    elif post_apply is None:
        post_apply = []

    return {
        "id":                    entry.get("id"),
        "event_id":              entry.get("event_id"),
        "table_fqn":             entry.get("table_fqn"),
        "table_name":            entry.get("table_name"),
        "action":                entry.get("action"),
        "rows_affected":         entry.get("rows_affected"),
        "dry_run":               entry.get("dry_run"),
        "sandbox_passed":        entry.get("sandbox_passed"),
        "confidence":            entry.get("confidence"),
        "failure_categories":    entry.get("failure_categories") or [],
        "applied_at":            entry.get("applied_at"),
        "error":                 entry.get("error"),
        "fix_sql":               entry.get("fix_sql"),
        "rollback_sql":          entry.get("rollback_sql"),
        "post_apply_assertions": post_apply,
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

@router.get("/connections/{connection_id}/health")
async def get_connection_health(connection_id: str):
    """
    Real-time health score for a connected database.
    Score computed from profiling report anomaly counts.
    """
    from src.db.connection_registry import get_connection
    from src.db.profiling_store import get_report

    conn_record = await get_connection(connection_id)
    if not conn_record:
        raise HTTPException(status_code=404, detail="Connection not found")

    critical = conn_record.critical_count or 0
    total = conn_record.total_anomalies or 0
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
    if conn_record.profiling_report_id:
        try:
            report = await get_report(conn_record.profiling_report_id)
            if report:
                for t in (report.tables or []):
                    anomaly_count = len(t.anomalies or [])
                    tables_summary.append({
                        "table_name":    t.table_name,
                        "schema":        t.schema_name,
                        "anomaly_count": anomaly_count,
                        "status":        "dirty" if anomaly_count > 0 else "clean",
                    })
        except Exception as e:
            logger.warning(f"[Health] Could not load profiling report: {e}")

    return {
        "connection_id":    connection_id,
        "health_score":     health_score,
        "status":           status_label,
        "total_anomalies":  total,
        "critical_count":   critical,
        "warning_count":    warnings,
        "tables_found":     conn_record.tables_found or 0,
        "tables":           tables_summary,
        "last_profiled_at": conn_record.last_profiled_at.isoformat() if conn_record.last_profiled_at else None,
        "db_name":          conn_record.db_name,
        "connection_hint":  conn_record.connection_hint,
    }

@router.get("/reports")
async def get_reports(
    limit: int = Query(default=50, le=200),
    table_name: str | None = Query(default=None),
):
    """
    List all fix reports, newest first.
    Optionally filter by ?table_name=orders
    Used by the frontend Incidents page.
    """
    reports = await fetch_reports(limit=limit, table_name=table_name)

    serialized = []
    for r in reports:
        serialized.append({
            "report_id":         r.get("report_id"),
            "event_id":          r.get("event_id"),
            "table_fqn":         r.get("table_fqn"),
            "table_name":        r.get("table_name"),
            "column_name":       r.get("column_name"),
            "anomaly_type":      r.get("anomaly_type"),
            "anomaly_severity":  r.get("anomaly_severity"),
            "fix_type":          r.get("fix_type"),
            "fix_sql":           r.get("fix_sql"),
            "rows_affected":     r.get("rows_affected"),
            "confidence":        r.get("confidence"),
            "sandbox_passed":    r.get("sandbox_passed"),
            "post_apply_passed": r.get("post_apply_passed"),
            "assertions_passed": r.get("assertions_passed"),
            "assertions_total":  r.get("assertions_total"),
            "recurrence_count":  r.get("recurrence_count"),
            "downstream_tables": r.get("downstream_tables") or [],
            "approver":          r.get("approver"),
            "created_at":        r.get("created_at"),
        })

    return {"count": len(serialized), "reports": serialized}


@router.get("/reports/stats")
async def get_report_stats():
    """
    Aggregate stats for the Incidents page header cards.
    Returns: total_incidents, total_rows_healed, avg_confidence,
             tables_touched, recurrence_rate.
    """
    return await fetch_report_stats()


@router.get("/reports/{report_id}")
async def get_report(report_id: str):
    """Single fix report detail by UUID. Used by the deep-link incident page."""
    report = await fetch_report_by_id(report_id)
    if not report:
        raise HTTPException(
            status_code=404,
            detail=f"Report {report_id} not found",
        )
    return {
        "report_id":         report.get("report_id"),
        "event_id":          report.get("event_id"),
        "table_fqn":         report.get("table_fqn"),
        "table_name":        report.get("table_name"),
        "column_name":       report.get("column_name"),
        "anomaly_type":      report.get("anomaly_type"),
        "anomaly_severity":  report.get("anomaly_severity"),
        "fix_type":          report.get("fix_type"),
        "fix_sql":           report.get("fix_sql"),
        "rows_affected":     report.get("rows_affected"),
        "confidence":        report.get("confidence"),
        "sandbox_passed":    report.get("sandbox_passed"),
        "post_apply_passed": report.get("post_apply_passed"),
        "assertions_passed": report.get("assertions_passed"),
        "assertions_total":  report.get("assertions_total"),
        "recurrence_count":  report.get("recurrence_count"),
        "downstream_tables": report.get("downstream_tables") or [],
        "approver":          report.get("approver"),
        "created_at":        report.get("created_at"),
    }
    