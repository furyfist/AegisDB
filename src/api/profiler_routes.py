import logging
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel

from src.agents.profiler import profiler_agent
from src.db.profiling_store import save_report, get_report, list_reports

logger = logging.getLogger(__name__)
router = APIRouter()


class ProfileRequest(BaseModel):
    """
    POST /api/v1/profile body.
    connection_url: full postgres connection string including credentials.
    Credentials are never stored — only host:port/db is logged.
    """
    connection_url: str
    schemas: list[str] = ["public"]
    table_limit: int = 50


class ProfileResponse(BaseModel):
    report_id:       str
    connection_hint: str
    status:          str
    tables_scanned:  int
    total_anomalies: int
    critical_count:  int
    warning_count:   int
    duration_ms:     int
    message:         str


@router.post("/profile", response_model=ProfileResponse)
async def run_profile(request: ProfileRequest, background_tasks: BackgroundTasks):
    """
    Profile a Postgres database. Returns immediately with a report_id.
    The full report is available at GET /api/v1/profile/{report_id}.

    For databases with < 20 tables this completes in seconds.
    For larger databases use the report_id to poll for completion.
    """
    # Validate connection_url format
    if not request.connection_url.startswith(
        ("postgresql://", "postgres://")
    ):
        raise HTTPException(
            status_code=400,
            detail="connection_url must start with postgresql:// or postgres://",
        )

    logger.info(f"[ProfilerRoute] Starting profile schemas={request.schemas}")

    # Run synchronously for small DBs (returns in <30s typically)
    # For large DBs move to background_tasks and poll pattern
    try:
        report = await profiler_agent.profile(
            connection_url=request.connection_url,
            schemas=request.schemas,
            table_limit=request.table_limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Save in background — don't block the response
    background_tasks.add_task(save_report, report)

    return ProfileResponse(
        report_id=report.report_id,
        connection_hint=report.connection_hint,
        status=report.status,
        tables_scanned=report.tables_scanned,
        total_anomalies=report.total_anomalies,
        critical_count=report.critical_count,
        warning_count=report.warning_count,
        duration_ms=report.duration_ms,
        message=(
            f"Found {report.total_anomalies} anomalies across "
            f"{report.tables_scanned} tables "
            f"({report.critical_count} critical, "
            f"{report.warning_count} warnings)"
        ),
    )


@router.get("/profile/{report_id}")
async def get_profile_report(report_id: str):
    """
    Get the full profiling report including all anomalies per table.
    """
    report = await get_report(report_id)
    if not report:
        raise HTTPException(
            status_code=404,
            detail=f"Report {report_id} not found",
        )
    return report


@router.get("/profiles")
async def list_profile_reports(limit: int = Query(default=10, le=50)):
    """
    List recent profiling reports — summary only, no full data.
    """
    reports = await list_reports(limit)
    return {"count": len(reports), "reports": reports}


@router.get("/profile/{report_id}/anomalies")
async def get_anomalies(
    report_id: str,
    severity: str | None = None,
    table_name: str | None = None,
):
    """
    Get filtered anomalies from a report.
    severity: critical | warning | info
    table_name: filter to specific table
    """
    report = await get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    results = []
    for table in report.tables:
        if table_name and table.table_name != table_name:
            continue
        for anomaly in table.anomalies:
            if severity and anomaly.severity.value != severity:
                continue
            results.append({
                "table":        table.table_name,
                "schema":       table.schema_name,
                "column":       anomaly.column_name,
                "anomaly_type": anomaly.anomaly_type.value,
                "severity":     anomaly.severity.value,
                "affected_rows": anomaly.affected_rows,
                "total_rows":   anomaly.total_rows,
                "rate":         anomaly.rate,
                "description":  anomaly.description,
                "sample_values": anomaly.sample_values,
            })

    return {
        "report_id":     report_id,
        "total_matched": len(results),
        "anomalies":     results,
    }