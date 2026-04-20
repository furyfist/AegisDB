import json
import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy import text
from src.core.config import settings
from src.core.models import EnrichedFailureEvent

logger = logging.getLogger(__name__)

_event_engine: AsyncEngine | None = None


def _build_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


async def init_event_store():
    """
    Create _aegisdb_events table on startup.
    Each enriched event is written here immediately after enrichment.
    Repair and apply agents look up events here by event_id — 
    no more guessing column names.
    """
    global _event_engine
    _event_engine = create_async_engine(
        _build_url(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        echo=False,
    )
    try:
        async with _event_engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _aegisdb_events (
                    id             SERIAL PRIMARY KEY,
                    event_id       VARCHAR(36)  NOT NULL UNIQUE,
                    table_fqn      TEXT         NOT NULL,
                    table_name     TEXT         NOT NULL,
                    enrichment_ok  BOOLEAN      DEFAULT FALSE,
                    severity       VARCHAR(20)  DEFAULT 'medium',
                    event_data     JSONB        NOT NULL,
                    created_at     TIMESTAMPTZ  DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_events_event_id
                ON _aegisdb_events (event_id)
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_events_created_at
                ON _aegisdb_events (created_at DESC)
            """))
        logger.info("[EventStore] Table _aegisdb_events ready")
    except Exception as e:
        logger.error(f"[EventStore] Init failed: {e}")
        raise


async def write_event(event: EnrichedFailureEvent) -> bool:
    if not _event_engine:
        logger.warning("[EventStore] Engine not initialized — skipping write")
        return False

    parts = event.table_fqn.split(".")
    table_name = parts[-1] if parts else event.table_fqn

    try:
        event_data_str = json.dumps(
            event.model_dump(mode="json"), default=str
        )
        async with _event_engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO _aegisdb_events
                        (event_id, table_fqn, table_name, enrichment_ok, severity, event_data)
                    VALUES
                        (:event_id, :table_fqn, :table_name, :enrichment_ok, :severity,
                         CAST(:event_data AS jsonb))
                    ON CONFLICT (event_id) DO UPDATE SET
                        enrichment_ok = EXCLUDED.enrichment_ok,
                        event_data    = EXCLUDED.event_data
                """),
                {
                    "event_id":      event.event_id,
                    "table_fqn":     event.table_fqn,
                    "table_name":    table_name,
                    "enrichment_ok": event.enrichment_success,
                    "severity":      event.severity.value,
                    "event_data":    event_data_str,
                },
            )
        logger.info(f"[EventStore] Written event={event.event_id}")
        return True
    except Exception as e:
        logger.error(f"[EventStore] Write failed (non-critical): {e}")
        return False


async def get_event(event_id: str) -> EnrichedFailureEvent | None:
    """
    Look up a stored event by event_id.
    Returns a fully hydrated EnrichedFailureEvent with real column names,
    real failed tests, real table context — no guessing.
    """
    if not _event_engine:
        return None
    try:
        async with _event_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT event_data FROM _aegisdb_events
                    WHERE event_id = :event_id
                """),
                {"event_id": event_id},
            )
            row = result.fetchone()
            if not row:
                logger.warning(f"[EventStore] Event not found: {event_id}")
                return None

            # asyncpg returns JSONB as a dict already
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)

            return EnrichedFailureEvent.model_validate(data)
    except Exception as e:
        logger.error(f"[EventStore] Lookup failed for {event_id}: {e}")
        return None


async def close_event_store():
    if _event_engine:
        await _event_engine.dispose()