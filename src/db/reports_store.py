import json
import logging
import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import settings
from src.core.models import FixReport, FixType

logger = logging.getLogger(__name__)

_reports_engine: AsyncEngine | None = None


def _build_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


async def init_reports_store():
    """Create _aegisdb_reports table + indexes if they don't exist. Called on startup."""
    global _reports_engine
    _reports_engine = create_async_engine(
        _build_url(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        echo=False,
    )
    try:
        async with _reports_engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _aegisdb_reports (
                    id                  SERIAL PRIMARY KEY,
                    report_id           VARCHAR(36)   NOT NULL UNIQUE,
                    event_id            VARCHAR(36)   NOT NULL,
                    table_fqn           TEXT          NOT NULL,
                    table_name          TEXT          NOT NULL,
                    column_name         TEXT          NOT NULL DEFAULT '',
                    anomaly_type        TEXT          NOT NULL DEFAULT '',
                    anomaly_severity    TEXT          NOT NULL DEFAULT 'low',
                    fix_type            TEXT          NOT NULL DEFAULT 'other',
                    fix_sql             TEXT          NOT NULL DEFAULT '',
                    rows_affected       INTEGER       DEFAULT 0,
                    confidence          FLOAT         DEFAULT 0.0,
                    sandbox_passed      BOOLEAN       DEFAULT FALSE,
                    post_apply_passed   BOOLEAN       DEFAULT FALSE,
                    assertions_passed   INTEGER       DEFAULT 0,
                    assertions_total    INTEGER       DEFAULT 0,
                    recurrence_count    INTEGER       DEFAULT 0,
                    downstream_tables   TEXT[]        DEFAULT ARRAY[]::TEXT[],
                    approver            TEXT          DEFAULT 'human',
                    created_at          TIMESTAMPTZ   DEFAULT NOW()
                )
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_reports_event_id
                ON _aegisdb_reports (event_id)
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_reports_table_fqn
                ON _aegisdb_reports (table_fqn)
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_reports_created_at
                ON _aegisdb_reports (created_at DESC)
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_reports_column_anomaly
                ON _aegisdb_reports (table_name, column_name, anomaly_type)
            """))

        logger.info("[ReportsStore] Table _aegisdb_reports ready")
    except Exception as e:
        logger.error(f"[ReportsStore] Failed to create reports table: {e}")
        raise


async def write_report(report: FixReport) -> int | None:
    """
    Insert one fix report row. Returns the generated id.
    Never raises — report failure must not block the apply pipeline.
    """
    if not _reports_engine:
        logger.warning("[ReportsStore] Engine not initialized — skipping report write")
        return None
    try:
        async with _reports_engine.begin() as conn:
            result = await conn.execute(
                text("""
                    INSERT INTO _aegisdb_reports (
                        report_id, event_id, table_fqn, table_name,
                        column_name, anomaly_type, anomaly_severity,
                        fix_type, fix_sql, rows_affected, confidence,
                        sandbox_passed, post_apply_passed,
                        assertions_passed, assertions_total,
                        recurrence_count, downstream_tables,
                        approver, created_at
                    ) VALUES (
                        :report_id, :event_id, :table_fqn, :table_name,
                        :column_name, :anomaly_type, :anomaly_severity,
                        :fix_type, :fix_sql, :rows_affected, :confidence,
                        :sandbox_passed, :post_apply_passed,
                        :assertions_passed, :assertions_total,
                        :recurrence_count, :downstream_tables,
                        :approver, :created_at
                    )
                    RETURNING id
                """),
                {
                    "report_id":         report.report_id,
                    "event_id":          report.event_id,
                    "table_fqn":         report.table_fqn,
                    "table_name":        report.table_name,
                    "column_name":       report.column_name,
                    "anomaly_type":      report.anomaly_type,
                    "anomaly_severity":  report.anomaly_severity,
                    "fix_type":          report.fix_type.value,
                    "fix_sql":           report.fix_sql,
                    "rows_affected":     report.rows_affected,
                    "confidence":        report.confidence,
                    "sandbox_passed":    report.sandbox_passed,
                    "post_apply_passed": report.post_apply_passed,
                    "assertions_passed": report.assertions_passed,
                    "assertions_total":  report.assertions_total,
                    "recurrence_count":  report.recurrence_count,
                    "downstream_tables": report.downstream_tables,
                    "approver":          report.approver,
                    "created_at":        report.created_at,
                },
            )
            report_db_id = result.scalar_one()
            logger.info(
                f"[ReportsStore] Wrote report id={report_db_id} "
                f"event={report.event_id} table={report.table_name} "
                f"col={report.column_name} recurrence={report.recurrence_count}"
            )
            return report_db_id
    except Exception as e:
        logger.error(f"[ReportsStore] Write failed (non-critical): {e}")
        return None


async def fetch_reports(
    limit: int = 50,
    table_name: str | None = None,
) -> list[dict]:
    """
    Fetch fix reports, newest first.
    Optionally filter by table_name.
    Used by GET /api/v1/reports.
    """
    if not _reports_engine:
        return []
    try:
        async with _reports_engine.connect() as conn:
            if table_name:
                result = await conn.execute(
                    text("""
                        SELECT * FROM _aegisdb_reports
                        WHERE table_name = :table_name
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {"table_name": table_name, "limit": limit},
                )
            else:
                result = await conn.execute(
                    text("""
                        SELECT * FROM _aegisdb_reports
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {"limit": limit},
                )
            rows = result.fetchall()
            keys = result.keys()
            return [dict(zip(keys, row)) for row in rows]
    except Exception as e:
        logger.error(f"[ReportsStore] fetch_reports failed: {e}")
        return []


async def fetch_report_by_id(report_id: str) -> dict | None:
    """Single report by report_id UUID. Used by GET /api/v1/reports/{report_id}."""
    if not _reports_engine:
        return None
    try:
        async with _reports_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT * FROM _aegisdb_reports
                    WHERE report_id = :report_id
                    LIMIT 1
                """),
                {"report_id": report_id},
            )
            row = result.fetchone()
            if not row:
                return None
            return dict(zip(result.keys(), row))
    except Exception as e:
        logger.error(f"[ReportsStore] fetch_report_by_id failed: {e}")
        return None


async def fetch_report_stats() -> dict:
    """
    Aggregate stats for the Incidents page header cards.
    Computed in SQL — single query, no Python loops.
    """
    if not _reports_engine:
        return {
            "total_incidents": 0,
            "total_rows_healed": 0,
            "avg_confidence": 0.0,
            "tables_touched": 0,
            "recurrence_rate": 0.0,
        }
    try:
        async with _reports_engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT
                    COUNT(*)                                        AS total_incidents,
                    COALESCE(SUM(rows_affected), 0)                AS total_rows_healed,
                    COALESCE(AVG(confidence), 0.0)                 AS avg_confidence,
                    COUNT(DISTINCT table_name)                      AS tables_touched,
                    COALESCE(
                        SUM(CASE WHEN recurrence_count > 0 THEN 1 ELSE 0 END)::FLOAT
                        / NULLIF(COUNT(*), 0),
                        0.0
                    )                                               AS recurrence_rate
                FROM _aegisdb_reports
            """))
            row = result.fetchone()
            if not row:
                return {
                    "total_incidents": 0,
                    "total_rows_healed": 0,
                    "avg_confidence": 0.0,
                    "tables_touched": 0,
                    "recurrence_rate": 0.0,
                }
            return {
                "total_incidents":  int(row[0]),
                "total_rows_healed": int(row[1]),
                "avg_confidence":   round(float(row[2]), 3),
                "tables_touched":   int(row[3]),
                "recurrence_rate":  round(float(row[4]), 3),
            }
    except Exception as e:
        logger.error(f"[ReportsStore] fetch_report_stats failed: {e}")
        return {
            "total_incidents": 0,
            "total_rows_healed": 0,
            "avg_confidence": 0.0,
            "tables_touched": 0,
            "recurrence_rate": 0.0,
        }


async def close_reports_store():
    if _reports_engine:
        await _reports_engine.dispose()


# ── Helpers used by apply.py to build a FixReport ────────────────────────────

def extract_fix_type(fix_sql: str) -> FixType:
    """Determine fix type from first keyword of the SQL."""
    sql = fix_sql.strip().lower()
    if sql.startswith("update"):
        return FixType.UPDATE
    if sql.startswith("delete"):
        return FixType.DELETE
    if sql.startswith("insert"):
        return FixType.INSERT
    return FixType.OTHER


def extract_primary_column(fix_sql: str) -> str:
    """
    Extract the primary column being fixed from fix_sql.
    Reuses the same regex patterns as _extract_failed_tests in apply.py.
    Returns empty string if nothing found — never raises.
    """
    sql = fix_sql.lower()
    patterns = [
        r"set\s+(\w+)\s*=",
        r"where\s+(\w+)\s+is\s+(?:not\s+)?null",
        r"where\s+(\w+)\s*[<>=!]",
        r"and\s+(\w+)\s+is\s+(?:not\s+)?null",
    ]
    _sql_keywords = {
        "null", "not", "and", "or", "where", "set", "from",
        "table", "select", "true", "false", "is", "in",
        "like", "between", "exists",
    }
    for pattern in patterns:
        matches = re.findall(pattern, sql)
        for m in matches:
            if m not in _sql_keywords:
                return m
    return ""


async def get_recurrence_count(
    table_name: str,
    column_name: str,
    anomaly_type: str,
) -> int:
    """
    Count how many times this exact (table, column, anomaly_type) combo
    has been fixed before. Called at write time — result stored on the report.
    Returns 0 safely if store is unavailable.
    """
    if not _reports_engine:
        return 0
    try:
        async with _reports_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT COUNT(*) FROM _aegisdb_reports
                    WHERE table_name  = :table_name
                      AND column_name = :column_name
                      AND anomaly_type = :anomaly_type
                """),
                {
                    "table_name":   table_name,
                    "column_name":  column_name,
                    "anomaly_type": anomaly_type,
                },
            )
            row = result.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.error(f"[ReportsStore] get_recurrence_count failed: {e}")
        return 0