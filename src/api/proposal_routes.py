import json
import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import decimal
from src.core.models import ProposalStatus, DiagnosisResult, EnrichedFailureEvent
from src.core.config import settings
from src.db.proposal_store import (
    get_proposal,
    list_proposals,
    update_proposal_status,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class DecisionRequest(BaseModel):
    reason: str | None = None   # required for rejection, optional for approval
    decided_by: str = "user"


def _json_default(obj):
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
@router.get("/proposals")
async def list_all_proposals(
    status: str | None = Query(
        default=None,
        description="Filter by status: pending_approval | approved | rejected | completed | failed"
    ),
    limit: int = Query(default=20, le=100),
):
    """
    List repair proposals. Default returns all.
    The UI polls this to show the approval queue.
    """
    proposals = await list_proposals(status=status, limit=limit)
    return {
        "count": len(proposals),
        "proposals": proposals,
    }


@router.get("/proposals/pending")
async def list_pending_proposals():
    """Shortcut — returns only proposals waiting for approval."""
    proposals = await list_proposals(
        status=ProposalStatus.PENDING_APPROVAL.value, limit=100
    )
    return {
        "count": len(proposals),
        "proposals": proposals,
    }


@router.get("/proposals/{proposal_id}")
async def get_proposal_detail(proposal_id: str):
    """
    Full proposal detail including sandbox preview data.
    Shows the user exactly what will change before they approve.
    """
    proposal = await get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(
            status_code=404,
            detail=f"Proposal {proposal_id} not found",
        )
    
    # Convert to dict for manipulation (get_proposal returns dict from DB)
    if not isinstance(proposal, dict):
        # If it's a model, convert to dict first
        proposal = proposal.model_dump() if hasattr(proposal, "model_dump") else dict(proposal)
    
    # Enhance with display fields derived from existing data
    import re as _re

    fix_sql_lower = (proposal.get("fix_sql") or "").lower()
    categories    = proposal.get("failure_categories") or []

    # fix_type
    if fix_sql_lower.strip().startswith("delete"):
        fix_type = "DELETE"
    elif fix_sql_lower.strip().startswith("update"):
        fix_type = "UPDATE"
    elif fix_sql_lower.strip().startswith("insert"):
        fix_type = "INSERT"
    else:
        fix_type = "OTHER"

    # highlighted_columns from fix_sql
    _kw = {
        "null","not","and","or","where","set","from",
        "table","select","true","false","is","in",
        "like","between","exists",
    }
    _cols: list[str] = []
    _cols += _re.findall(r"where\s+(\w+)\s+is\s+(?:not\s+)?null", fix_sql_lower)
    _cols += _re.findall(r"where\s+(\w+)\s*[<>=!]",               fix_sql_lower)
    _cols += _re.findall(r"set\s+(\w+)\s*=",                       fix_sql_lower)
    _cols += _re.findall(r"and\s+(\w+)\s+is\s+(?:not\s+)?null",   fix_sql_lower)
    highlighted_columns = list(dict.fromkeys(c for c in _cols if c not in _kw))

    # anomaly_type_label
    _label_map = {
        "null_violation":        "NULL Values",
        "range_violation":       "Out-of-Range Values",
        "uniqueness_violation":  "Duplicate Values",
        "format_violation":      "Invalid Format",
        "referential_integrity": "Referential Integrity",
        "schema_drift":          "Schema Drift",
    }
    primary_cat        = categories[0].lower() if categories else "unknown"
    anomaly_type_label = _label_map.get(primary_cat, primary_cat.replace("_", " ").title())

    proposal["fix_type"]            = fix_type
    proposal["highlighted_columns"] = highlighted_columns
    proposal["anomaly_type_label"]  = anomaly_type_label
    proposal["fix_sql_display"]     = (proposal.get("fix_sql") or "").replace(
        "{table}", f'"{proposal.get("table_name", "table")}"'
    )
    
    return proposal


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, body: DecisionRequest = DecisionRequest()):
    """
    Approve a repair proposal.
    This publishes the DiagnosisResult to aegisdb:repair, which
    triggers the existing repair → sandbox → apply pipeline.
    The apply agent respects DRY_RUN mode — set to false to actually
    write to production.
    """
    proposal = await get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != ProposalStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Proposal is already {proposal.status.value} — cannot approve",
        )

    if not proposal.diagnosis_json:
        raise HTTPException(
            status_code=422,
            detail="Proposal has no diagnosis data — cannot execute",
        )

    # Update status first
    await update_proposal_status(
        proposal_id=proposal_id,
        status=ProposalStatus.APPROVED,
        decision_by=body.decided_by,
    )

    # Publish DiagnosisResult to aegisdb:repair stream
    # The existing repair agent picks it up and runs the full pipeline
    try:
        import redis.asyncio as aioredis
        r = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )

        diagnosis = DiagnosisResult.model_validate_json(proposal.diagnosis_json)

        payload = {
            "event_id":      proposal.event_id,
            "table_fqn":     proposal.table_fqn,
            "confidence":    str(proposal.confidence),
            "is_repairable": "True",
            "data":          proposal.diagnosis_json,
        }
        msg_id = await r.xadd(
            settings.redis_repair_stream, payload, maxlen=500
        )
        await r.aclose()

        logger.info(
            f"[ProposalRoutes] Approved proposal={proposal_id} "
            f"→ repair stream msg={msg_id}"
        )

        # Mark as executing
        await update_proposal_status(
            proposal_id=proposal_id,
            status=ProposalStatus.EXECUTING,
            decision_by=body.decided_by,
        )

        return {
            "proposal_id":  proposal_id,
            "status":       ProposalStatus.EXECUTING.value,
            "message":      (
                f"Approved. Fix is executing. "
                f"DRY_RUN={'ON' if settings.dry_run else 'OFF'}. "
                f"Check GET /api/v1/audit for results."
            ),
            "repair_msg_id": msg_id,
            "dry_run":      settings.dry_run,
        }

    except Exception as e:
        # Roll back status if publish failed
        await update_proposal_status(
            proposal_id=proposal_id,
            status=ProposalStatus.FAILED,
            decision_by="system",
            rejection_reason=f"Publish to repair stream failed: {e}",
        )
        logger.error(f"[ProposalRoutes] Approve failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to queue repair: {e}",
        )


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, body: DecisionRequest):
    """
    Reject a repair proposal. No fix is applied.
    The rejection reason is recorded in the audit trail.
    """
    if not body.reason:
        raise HTTPException(
            status_code=400,
            detail="Rejection reason is required",
        )

    proposal = await get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != ProposalStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Proposal is already {proposal.status.value}",
        )

    await update_proposal_status(
        proposal_id=proposal_id,
        status=ProposalStatus.REJECTED,
        decision_by=body.decided_by,
        rejection_reason=body.reason,
    )

    # Write to escalation stream so it shows up in monitoring
    try:
        import redis.asyncio as aioredis
        r = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
        await r.xadd(
            settings.redis_escalation_stream,
            {
                "event_id":       proposal.event_id,
                "table_fqn":      proposal.table_fqn,
                "reason":         f"Rejected by user: {body.reason}",
                "stage":          "approval",
                "proposal_id":    proposal_id,
            },
            maxlen=500,
        )
        await r.aclose()
    except Exception as e:
        logger.warning(f"[ProposalRoutes] Escalation stream publish failed: {e}")

    logger.info(
        f"[ProposalRoutes] Rejected proposal={proposal_id} "
        f"reason='{body.reason}'"
    )

    return {
        "proposal_id": proposal_id,
        "status":      ProposalStatus.REJECTED.value,
        "reason":      body.reason,
        "message":     "Proposal rejected. No changes made to the database.",
    }


@router.post("/proposals/{proposal_id}/re-sandbox")
async def re_sandbox_proposal(proposal_id: str):
    """
    Re-run the sandbox on a proposal to get a fresh preview.
    Useful if the data changed since the proposal was created.
    """
    proposal = await get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status not in (
        ProposalStatus.PENDING_APPROVAL, ProposalStatus.FAILED
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot re-sandbox a {proposal.status.value} proposal",
        )

    if not proposal.event_json or not proposal.diagnosis_json:
        raise HTTPException(
            status_code=422,
            detail="Proposal missing event or diagnosis data for re-sandbox",
        )

    try:
        from src.sandbox.executor import run_sandbox
        from src.db.proposal_store import create_proposal
        from src.core.models import RepairProposalRecord

        event     = EnrichedFailureEvent.model_validate_json(proposal.event_json)
        diagnosis = DiagnosisResult.model_validate_json(proposal.diagnosis_json)

        sandbox_result = await run_sandbox(event, diagnosis, attempt=1)

        rows_before = rows_after = rows_affected = 0
        sample_before: list[dict] = []
        sample_after:  list[dict] = []

        if sandbox_result.data_diff:
            rows_before   = sandbox_result.data_diff.rows_before
            rows_after    = sandbox_result.data_diff.rows_after
            rows_affected = (
                sandbox_result.data_diff.rows_deleted
                + sandbox_result.data_diff.rows_updated
            )
            sample_before = sandbox_result.data_diff.sample_before
            sample_after  = sandbox_result.data_diff.sample_after

        # Update proposal with fresh sandbox data
        proposal.sandbox_passed = sandbox_result.sandbox_passed
        proposal.rows_before    = rows_before
        proposal.rows_after     = rows_after
        proposal.rows_affected  = rows_affected
        proposal.sample_before  = sample_before
        proposal.sample_after   = sample_after

        await create_proposal(proposal)  # ON CONFLICT DO NOTHING skips — need update
        # Direct update for sandbox fields
        from sqlalchemy import text
        from src.db.proposal_store import _proposal_engine
        if _proposal_engine:
            async with _proposal_engine.begin() as conn:
                await conn.execute(
                    text("""
                        UPDATE _aegisdb_proposals SET
                            sandbox_passed = :sandbox_passed,
                            rows_before    = :rows_before,
                            rows_after     = :rows_after,
                            rows_affected  = :rows_affected,
                            sample_before  = CAST(:sample_before AS jsonb),
                            sample_after   = CAST(:sample_after AS jsonb)
                        WHERE proposal_id = :proposal_id
                    """),
                    {
                        "proposal_id":   proposal_id,
                        "sandbox_passed": sandbox_result.sandbox_passed,
                        "rows_before":    rows_before,
                        "rows_after":     rows_after,
                        "rows_affected":  rows_affected,
                        "sample_before":  json.dumps(sample_before, default=_json_default),
                        "sample_after":   json.dumps(sample_after, default=_json_default),
                    },
                )

        return {
            "proposal_id":   proposal_id,
            "sandbox_passed": sandbox_result.sandbox_passed,
            "rows_before":    rows_before,
            "rows_after":     rows_after,
            "rows_affected":  rows_affected,
            "sample_before":  sample_before[:settings.sandbox_diff_rows],
            "sample_after":   sample_after[:settings.sandbox_diff_rows],
            "message":        "Sandbox refreshed with current data",
        }

    except Exception as e:
        logger.error(f"[ProposalRoutes] Re-sandbox failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))