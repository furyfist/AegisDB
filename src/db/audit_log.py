import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy import text
from src.core.config import settings
from src.core.models import AuditEntry, ApplyResult

logger = logging.getLogger(__name__)

# Separate engine for audit — avoids competing with read operations
# pool_size=1: audit writes are low-frequency, no need for a pool
_audit_engine: AsyncEngine | None = None

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _aegisdb_audit (
    id                   SERIAL PRIMARY KEY,
    event_id             VARCHAR(36)  NOT NULL,
    table_fqn            TEXT         NOT NULL,
    table_name           TEXT         NOT NULL,
    action               VARCHAR(20)  NOT NULL,
    fix_sql              TEXT         DEFAULT '',
    rollback_sql         TEXT,
    rows_affected        INTEGER      DEFAULT 0,
    dry_run              BOOLEAN      DEFAULT TRUE,
    sandbox_passed       BOOLEAN      DEFAULT FALSE,
    confidence           FLOAT        DEFAULT 0.0,
    failure_categories   TEXT[]       DEFAULT ARRAY[]::TEXT[],
    applied_at           TIMESTAMPTZ  DEFAULT NOW(),
    post_apply_json      JSONB        DEFAULT '[]'::jsonb,
    error                TEXT,
    metadata_json        JSONB        DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_event_id
    ON _aegisdb_audit (event_id);
CREATE INDEX IF NOT EXISTS idx_audit_applied_at
    ON _aegisdb_audit (applied_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_table_fqn
    ON _aegisdb_audit (table_fqn);
"""


def _build_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


async def init_audit_table():
    """Create audit table + indexes if they don't exist. Called on startup."""
    global _audit_engine
    _audit_engine = create_async_engine(
        _build_url(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        echo=False,
    )
    try:
        async with _audit_engine.begin() as conn:
            # asyncpg requires each statement in its own execute call
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _aegisdb_audit (
                    id                   SERIAL PRIMARY KEY,
                    event_id             VARCHAR(36)  NOT NULL,
                    table_fqn            TEXT         NOT NULL,
                    table_name           TEXT         NOT NULL,
                    action               VARCHAR(20)  NOT NULL,
                    fix_sql              TEXT         DEFAULT '',
                    rollback_sql         TEXT,
                    rows_affected        INTEGER      DEFAULT 0,
                    dry_run              BOOLEAN      DEFAULT TRUE,
                    sandbox_passed       BOOLEAN      DEFAULT FALSE,
                    confidence           FLOAT        DEFAULT 0.0,
                    failure_categories   TEXT[]       DEFAULT ARRAY[]::TEXT[],
                    applied_at           TIMESTAMPTZ  DEFAULT NOW(),
                    post_apply_json      JSONB        DEFAULT '[]'::jsonb,
                    error                TEXT,
                    metadata_json        JSONB        DEFAULT '{}'::jsonb
                )
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_audit_event_id
                ON _aegisdb_audit (event_id)
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_audit_applied_at
                ON _aegisdb_audit (applied_at DESC)
            """))

            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_audit_table_fqn
                ON _aegisdb_audit (table_fqn)
            """))

        logger.info("[AuditLog] Table _aegisdb_audit ready")
    except Exception as e:
        logger.error(f"[AuditLog] Failed to create audit table: {e}")
        raise


async def write_audit(entry: AuditEntry) -> int | None:
    """
    Insert one audit row. Returns the generated id.
    Never raises — audit failure must not block the main pipeline.
    """
    if not _audit_engine:
        logger.warning("[AuditLog] Engine not initialized — skipping audit write")
        return None
    try:
        import json
        async with _audit_engine.begin() as conn:
            result = await conn.execute(
                text("""
                    INSERT INTO _aegisdb_audit (
                        event_id, table_fqn, table_name, action,
                        fix_sql, rollback_sql, rows_affected,
                        dry_run, sandbox_passed, confidence,
                        failure_categories, applied_at,
                        post_apply_json, error
                    ) VALUES (
                        :event_id, :table_fqn, :table_name, :action,
                        :fix_sql, :rollback_sql, :rows_affected,
                        :dry_run, :sandbox_passed, :confidence,
                        :failure_categories, :applied_at,
                        :post_apply_json, :error
                    )
                    RETURNING id
                """),
                {
                    "event_id": entry.event_id,
                    "table_fqn": entry.table_fqn,
                    "table_name": entry.table_name,
                    "action": entry.action.value,
                    "fix_sql": entry.fix_sql,
                    "rollback_sql": entry.rollback_sql,
                    "rows_affected": entry.rows_affected,
                    "dry_run": entry.dry_run,
                    "sandbox_passed": entry.sandbox_passed,
                    "confidence": entry.confidence,
                    "failure_categories": entry.failure_categories,
                    "applied_at": entry.applied_at,
                    "post_apply_json": json.dumps(
                        [a for a in entry.post_apply_assertions]
                    ),
                    "error": entry.error,
                },
            )
            audit_id = result.scalar_one()
            logger.info(
                f"[AuditLog] Wrote audit row id={audit_id} "
                f"event={entry.event_id} action={entry.action.value}"
            )
            return audit_id
    except Exception as e:
        logger.error(f"[AuditLog] Write failed (non-critical): {e}")
        return None


async def fetch_recent_audit(limit: int = 50) -> list[dict]:
    """Fetch recent audit log rows for the dashboard API."""
    if not _audit_engine:
        return []
    try:
        async with _audit_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT
                        id, event_id, table_fqn, table_name,
                        action, rows_affected, dry_run,
                        sandbox_passed, confidence,
                        failure_categories, applied_at, error
                    FROM _aegisdb_audit
                    ORDER BY applied_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()
            keys = result.keys()
            return [dict(zip(keys, row)) for row in rows]
    except Exception as e:
        logger.error(f"[AuditLog] Fetch failed: {e}")
        return []


async def close_audit():
    if _audit_engine:
        await _audit_engine.dispose()