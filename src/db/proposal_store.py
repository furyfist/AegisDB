import json
import logging
import decimal
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy import text
from src.core.config import settings
from src.core.models import RepairProposalRecord, ProposalStatus

logger = logging.getLogger(__name__)

_proposal_engine: AsyncEngine | None = None


def _build_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


async def init_proposal_store():
    global _proposal_engine
    _proposal_engine = create_async_engine(
        _build_url(),
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        echo=False,
    )
    try:
        async with _proposal_engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _aegisdb_proposals (
                    id                  SERIAL PRIMARY KEY,
                    proposal_id         VARCHAR(36)   NOT NULL UNIQUE,
                    event_id            VARCHAR(36)   NOT NULL,
                    table_fqn           TEXT          NOT NULL,
                    table_name          TEXT          NOT NULL,
                    failure_categories  TEXT[]        DEFAULT ARRAY[]::TEXT[],
                    root_cause          TEXT          DEFAULT '',
                    confidence          FLOAT         DEFAULT 0.0,
                    fix_sql             TEXT          DEFAULT '',
                    fix_description     TEXT          DEFAULT '',
                    rollback_sql        TEXT,
                    estimated_rows      INTEGER,
                    sandbox_passed      BOOLEAN       DEFAULT FALSE,
                    rows_before         INTEGER       DEFAULT 0,
                    rows_after          INTEGER       DEFAULT 0,
                    rows_affected       INTEGER       DEFAULT 0,
                    sample_before       JSONB         DEFAULT '[]'::jsonb,
                    sample_after        JSONB         DEFAULT '[]'::jsonb,
                    status              VARCHAR(30)   DEFAULT 'pending_approval',
                    created_at          TIMESTAMPTZ   DEFAULT NOW(),
                    decided_at          TIMESTAMPTZ,
                    decision_by         TEXT          DEFAULT 'system',
                    rejection_reason    TEXT,
                    diagnosis_json      TEXT          DEFAULT '',
                    event_json          TEXT          DEFAULT ''
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_proposals_proposal_id
                ON _aegisdb_proposals (proposal_id)
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_proposals_status
                ON _aegisdb_proposals (status)
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_proposals_created_at
                ON _aegisdb_proposals (created_at DESC)
            """))
        logger.info("[ProposalStore] Table _aegisdb_proposals ready")
    except Exception as e:
        logger.error(f"[ProposalStore] Init failed: {e}")
        raise


async def create_proposal(proposal: RepairProposalRecord) -> bool:
    if not _proposal_engine:
        return False
    try:
        async with _proposal_engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO _aegisdb_proposals (
                        proposal_id, event_id, table_fqn, table_name,
                        failure_categories, root_cause, confidence,
                        fix_sql, fix_description, rollback_sql,
                        estimated_rows, sandbox_passed,
                        rows_before, rows_after, rows_affected,
                        sample_before, sample_after,
                        status, created_at, diagnosis_json, event_json
                    ) VALUES (
                        :proposal_id, :event_id, :table_fqn, :table_name,
                        :failure_categories, :root_cause, :confidence,
                        :fix_sql, :fix_description, :rollback_sql,
                        :estimated_rows, :sandbox_passed,
                        :rows_before, :rows_after, :rows_affected,
                        :sample_before::jsonb, :sample_after::jsonb,
                        :status, :created_at, :diagnosis_json, :event_json
                    )
                    ON CONFLICT (proposal_id) DO NOTHING
                """),
                {
                    "proposal_id":        proposal.proposal_id,
                    "event_id":           proposal.event_id,
                    "table_fqn":          proposal.table_fqn,
                    "table_name":         proposal.table_name,
                    "failure_categories": proposal.failure_categories,
                    "root_cause":         proposal.root_cause,
                    "confidence":         proposal.confidence,
                    "fix_sql":            proposal.fix_sql,
                    "fix_description":    proposal.fix_description,
                    "rollback_sql":       proposal.rollback_sql,
                    "estimated_rows":     proposal.estimated_rows,
                    "sandbox_passed":     proposal.sandbox_passed,
                    "rows_before":        proposal.rows_before,
                    "rows_after":         proposal.rows_after,
                    "rows_affected":      proposal.rows_affected,
                    "sample_before":  json.dumps(proposal.sample_before, default=_json_default),
                    "sample_after":   json.dumps(proposal.sample_after, default=_json_default),
                    "status":             proposal.status.value,
                    "created_at":         proposal.created_at,
                    "diagnosis_json":     proposal.diagnosis_json,
                    "event_json":         proposal.event_json,
                },
            )
        logger.info(
            f"[ProposalStore] Created proposal={proposal.proposal_id} "
            f"table={proposal.table_name} confidence={proposal.confidence:.2f}"
        )
        return True
    except Exception as e:
        logger.error(f"[ProposalStore] Create failed: {e}")
        return False


def _json_default(obj):
    """Handle types that json.dumps can't serialize by default."""
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)

async def update_proposal_status(
    proposal_id: str,
    status: ProposalStatus,
    decision_by: str = "user",
    rejection_reason: str | None = None,
) -> bool:
    if not _proposal_engine:
        return False
    try:
        async with _proposal_engine.begin() as conn:
            await conn.execute(
                text("""
                    UPDATE _aegisdb_proposals
                    SET status           = :status,
                        decided_at       = :decided_at,
                        decision_by      = :decision_by,
                        rejection_reason = :rejection_reason
                    WHERE proposal_id = :proposal_id
                """),
                {
                    "proposal_id":     proposal_id,
                    "status":          status.value,
                    "decided_at":      datetime.now(),
                    "decision_by":     decision_by,
                    "rejection_reason": rejection_reason,
                },
            )
        return True
    except Exception as e:
        logger.error(f"[ProposalStore] Update status failed: {e}")
        return False


async def get_proposal(proposal_id: str) -> RepairProposalRecord | None:
    if not _proposal_engine:
        return None
    try:
        async with _proposal_engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT * FROM _aegisdb_proposals
                    WHERE proposal_id = :proposal_id
                """),
                {"proposal_id": proposal_id},
            )
            row = result.fetchone()
            if not row:
                return None
            return _row_to_model(dict(zip(result.keys(), row)))
    except Exception as e:
        logger.error(f"[ProposalStore] Get failed: {e}")
        return None


async def list_proposals(
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if not _proposal_engine:
        return []
    try:
        async with _proposal_engine.connect() as conn:
            if status:
                result = await conn.execute(
                    text("""
                        SELECT proposal_id, event_id, table_fqn, table_name,
                               failure_categories, root_cause, confidence,
                               fix_sql, fix_description, sandbox_passed,
                               rows_affected, status, created_at, decided_at,
                               rejection_reason
                        FROM _aegisdb_proposals
                        WHERE status = :status
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {"status": status, "limit": limit},
                )
            else:
                result = await conn.execute(
                    text("""
                        SELECT proposal_id, event_id, table_fqn, table_name,
                               failure_categories, root_cause, confidence,
                               fix_sql, fix_description, sandbox_passed,
                               rows_affected, status, created_at, decided_at,
                               rejection_reason
                        FROM _aegisdb_proposals
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """),
                    {"limit": limit},
                )
            rows = result.fetchall()
            keys = list(result.keys())
            out = []
            for row in rows:
                d = dict(zip(keys, row))
                for f in ("created_at", "decided_at"):
                    if d.get(f) and hasattr(d[f], "isoformat"):
                        d[f] = d[f].isoformat()
                out.append(d)
            return out
    except Exception as e:
        logger.error(f"[ProposalStore] List failed: {e}")
        return []


def _row_to_model(row: dict) -> RepairProposalRecord:
    sample_before = row.get("sample_before") or []
    sample_after  = row.get("sample_after") or []
    if isinstance(sample_before, str):
        sample_before = json.loads(sample_before)
    if isinstance(sample_after, str):
        sample_after = json.loads(sample_after)

    return RepairProposalRecord(
        proposal_id=row["proposal_id"],
        event_id=row["event_id"],
        table_fqn=row["table_fqn"],
        table_name=row["table_name"],
        failure_categories=row.get("failure_categories") or [],
        root_cause=row.get("root_cause") or "",
        confidence=row.get("confidence") or 0.0,
        fix_sql=row.get("fix_sql") or "",
        fix_description=row.get("fix_description") or "",
        rollback_sql=row.get("rollback_sql"),
        estimated_rows=row.get("estimated_rows"),
        sandbox_passed=row.get("sandbox_passed") or False,
        rows_before=row.get("rows_before") or 0,
        rows_after=row.get("rows_after") or 0,
        rows_affected=row.get("rows_affected") or 0,
        sample_before=sample_before,
        sample_after=sample_after,
        status=ProposalStatus(row.get("status", "pending_approval")),
        created_at=row.get("created_at") or datetime.now(),
        decided_at=row.get("decided_at"),
        decision_by=row.get("decision_by") or "system",
        rejection_reason=row.get("rejection_reason"),
        diagnosis_json=row.get("diagnosis_json") or "",
        event_json=row.get("event_json") or "",
    )


async def close_proposal_store():
    if _proposal_engine:
        await _proposal_engine.dispose()