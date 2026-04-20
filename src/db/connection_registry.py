import json
import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy import text
from src.core.config import settings
from src.core.models import DatabaseConnection, ConnectionStatus

logger = logging.getLogger(__name__)

_registry_engine: AsyncEngine | None = None


def _build_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


async def init_connection_registry():
    global _registry_engine
    _registry_engine = create_async_engine(
        _build_url(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        echo=False,
    )
    try:
        async with _registry_engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _aegisdb_connections (
                    id                   SERIAL PRIMARY KEY,
                    connection_id        VARCHAR(36)  NOT NULL UNIQUE,
                    service_name         TEXT         NOT NULL,
                    connection_hint      TEXT         NOT NULL,
                    db_name              TEXT         NOT NULL,
                    schema_names         TEXT[]       DEFAULT ARRAY['public'],
                    status               VARCHAR(20)  DEFAULT 'pending',
                    om_service_fqn       TEXT         DEFAULT '',
                    profiling_report_id  TEXT,
                    tables_found         INTEGER      DEFAULT 0,
                    total_anomalies      INTEGER      DEFAULT 0,
                    critical_count       INTEGER      DEFAULT 0,
                    error                TEXT,
                    registered_at        TIMESTAMPTZ  DEFAULT NOW(),
                    last_profiled_at     TIMESTAMPTZ
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_connections_connection_id
                ON _aegisdb_connections (connection_id)
            """))
        logger.info("[Registry] Table _aegisdb_connections ready")
    except Exception as e:
        logger.error(f"[Registry] Init failed: {e}")
        raise


async def save_connection(conn_obj: DatabaseConnection) -> bool:
    if not _registry_engine:
        return False
    try:
        async with _registry_engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO _aegisdb_connections (
                        connection_id, service_name, connection_hint,
                        db_name, schema_names, status, om_service_fqn,
                        profiling_report_id, tables_found,
                        total_anomalies, critical_count, error,
                        registered_at, last_profiled_at
                    ) VALUES (
                        :connection_id, :service_name, :connection_hint,
                        :db_name, :schema_names, :status, :om_service_fqn,
                        :profiling_report_id, :tables_found,
                        :total_anomalies, :critical_count, :error,
                        :registered_at, :last_profiled_at
                    )
                    ON CONFLICT (connection_id) DO UPDATE SET
                        status               = EXCLUDED.status,
                        om_service_fqn       = EXCLUDED.om_service_fqn,
                        profiling_report_id  = EXCLUDED.profiling_report_id,
                        tables_found         = EXCLUDED.tables_found,
                        total_anomalies      = EXCLUDED.total_anomalies,
                        critical_count       = EXCLUDED.critical_count,
                        error                = EXCLUDED.error,
                        last_profiled_at     = EXCLUDED.last_profiled_at
                """),
                {
                    "connection_id":       conn_obj.connection_id,
                    "service_name":        conn_obj.service_name,
                    "connection_hint":     conn_obj.connection_hint,
                    "db_name":             conn_obj.db_name,
                    "schema_names":        conn_obj.schema_names,
                    "status":              conn_obj.status.value,
                    "om_service_fqn":      conn_obj.om_service_fqn,
                    "profiling_report_id": conn_obj.profiling_report_id,
                    "tables_found":        conn_obj.tables_found,
                    "total_anomalies":     conn_obj.total_anomalies,
                    "critical_count":      conn_obj.critical_count,
                    "error":               conn_obj.error,
                    "registered_at":       conn_obj.registered_at,
                    "last_profiled_at":    conn_obj.last_profiled_at,
                },
            )
        logger.info(
            f"[Registry] Saved connection={conn_obj.connection_id} "
            f"status={conn_obj.status.value}"
        )
        return True
    except Exception as e:
        logger.error(f"[Registry] Save failed: {e}")
        return False


async def get_connection(connection_id: str) -> DatabaseConnection | None:
    if not _registry_engine:
        return None
    try:
        async with _registry_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT * FROM _aegisdb_connections
                    WHERE connection_id = :connection_id
                """),
                {"connection_id": connection_id},
            )
            row = result.fetchone()
            if not row:
                return None
            return _row_to_model(dict(zip(result.keys(), row)))
    except Exception as e:
        logger.error(f"[Registry] Get failed: {e}")
        return None


async def list_connections() -> list[dict]:
    if not _registry_engine:
        return []
    try:
        async with _registry_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT connection_id, service_name, connection_hint,
                           db_name, status, tables_found, total_anomalies,
                           critical_count, registered_at, last_profiled_at
                    FROM _aegisdb_connections
                    ORDER BY registered_at DESC
                """)
            )
            rows = result.fetchall()
            keys = result.keys()
            out = []
            for row in rows:
                d = dict(zip(keys, row))
                for f in ("registered_at", "last_profiled_at"):
                    if d.get(f) and hasattr(d[f], "isoformat"):
                        d[f] = d[f].isoformat()
            out.append(d)
            return out
    except Exception as e:
        logger.error(f"[Registry] List failed: {e}")
        return []


def _row_to_model(row: dict) -> DatabaseConnection:
    return DatabaseConnection(
        connection_id=row["connection_id"],
        service_name=row["service_name"],
        connection_hint=row["connection_hint"],
        db_name=row["db_name"],
        schema_names=row.get("schema_names") or ["public"],
        status=ConnectionStatus(row["status"]),
        om_service_fqn=row.get("om_service_fqn") or "",
        profiling_report_id=row.get("profiling_report_id"),
        tables_found=row.get("tables_found") or 0,
        total_anomalies=row.get("total_anomalies") or 0,
        critical_count=row.get("critical_count") or 0,
        error=row.get("error"),
        registered_at=row["registered_at"],
        last_profiled_at=row.get("last_profiled_at"),
    )


async def close_connection_registry():
    if _registry_engine:
        await _registry_engine.dispose()