import json
import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy import text
from src.core.config import settings
from src.core.models import ProfilingReport

logger = logging.getLogger(__name__)

_profiling_engine: AsyncEngine | None = None


def _build_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


async def init_profiling_store():
    global _profiling_engine
    _profiling_engine = create_async_engine(
        _build_url(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        echo=False,
    )
    try:
        async with _profiling_engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _aegisdb_profiling_reports (
                    id               SERIAL PRIMARY KEY,
                    report_id        VARCHAR(36)  NOT NULL UNIQUE,
                    connection_hint  TEXT         NOT NULL,
                    status           VARCHAR(20)  DEFAULT 'completed',
                    tables_scanned   INTEGER      DEFAULT 0,
                    total_anomalies  INTEGER      DEFAULT 0,
                    critical_count   INTEGER      DEFAULT 0,
                    warning_count    INTEGER      DEFAULT 0,
                    duration_ms      INTEGER      DEFAULT 0,
                    report_data      JSONB        NOT NULL,
                    created_at       TIMESTAMPTZ  DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_profiling_report_id
                ON _aegisdb_profiling_reports (report_id)
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_profiling_created_at
                ON _aegisdb_profiling_reports (created_at DESC)
            """))
        logger.info("[ProfilingStore] Table _aegisdb_profiling_reports ready")
    except Exception as e:
        logger.error(f"[ProfilingStore] Init failed: {e}")
        raise


async def save_report(report: ProfilingReport) -> bool:
    if not _profiling_engine:
        return False
    try:
        async with _profiling_engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO _aegisdb_profiling_reports (
                        report_id, connection_hint, status,
                        tables_scanned, total_anomalies,
                        critical_count, warning_count,
                        duration_ms, report_data
                    ) VALUES (
                        :report_id, :connection_hint, :status,
                        :tables_scanned, :total_anomalies,
                        :critical_count, :warning_count,
                        :duration_ms, :report_data::jsonb
                    )
                    ON CONFLICT (report_id) DO UPDATE SET
                        status          = EXCLUDED.status,
                        total_anomalies = EXCLUDED.total_anomalies,
                        report_data     = EXCLUDED.report_data
                """),
                {
                    "report_id":       report.report_id,
                    "connection_hint": report.connection_hint,
                    "status":          report.status,
                    "tables_scanned":  report.tables_scanned,
                    "total_anomalies": report.total_anomalies,
                    "critical_count":  report.critical_count,
                    "warning_count":   report.warning_count,
                    "duration_ms":     report.duration_ms,
                    "report_data":     json.dumps(
                        report.model_dump(mode="json"), default=str
                    ),
                },
            )
        logger.info(
            f"[ProfilingStore] Saved report={report.report_id} "
            f"anomalies={report.total_anomalies}"
        )
        return True
    except Exception as e:
        logger.error(f"[ProfilingStore] Save failed: {e}")
        return False


async def get_report(report_id: str) -> ProfilingReport | None:
    if not _profiling_engine:
        return None
    try:
        async with _profiling_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT report_data FROM _aegisdb_profiling_reports
                    WHERE report_id = :report_id
                """),
                {"report_id": report_id},
            )
            row = result.fetchone()
            if not row:
                return None
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)
            return ProfilingReport.model_validate(data)
    except Exception as e:
        logger.error(f"[ProfilingStore] Get failed: {e}")
        return None


async def list_reports(limit: int = 20) -> list[dict]:
    if not _profiling_engine:
        return []
    try:
        async with _profiling_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT
                        report_id, connection_hint, status,
                        tables_scanned, total_anomalies,
                        critical_count, warning_count,
                        duration_ms, created_at
                    FROM _aegisdb_profiling_reports
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()
            keys = result.keys()
            out = []
            for row in rows:
                d = dict(zip(keys, row))
                if "created_at" in d and hasattr(d["created_at"], "isoformat"):
                    d["created_at"] = d["created_at"].isoformat()
                out.append(d)
            return out
    except Exception as e:
        logger.error(f"[ProfilingStore] List failed: {e}")
        return []


async def close_profiling_store():
    if _profiling_engine:
        await _profiling_engine.dispose()