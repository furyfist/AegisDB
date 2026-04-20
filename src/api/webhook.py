import uuid
import logging
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks

from src.core.models import (
    OMWebhookPayload,
    EnrichedFailureEvent,
    FailedTest,
    TestStatus,
    FailureSeverity,
)
from src.services.om_client import om_client
from src.services.event_bus import event_bus

logger = logging.getLogger(__name__)
router = APIRouter()


def _extract_failed_tests(payload: OMWebhookPayload) -> list[FailedTest]:
    """
    Parse failed test cases out of the raw OM webhook payload.
    OM sends test results nested inside entity changeDescription.
    """
    failed = []
    entity = payload.entity or {}

    # OM 1.6 sends test case results directly on the entity
    test_case_result = entity.get("testCaseResult", {})
    if test_case_result.get("testCaseStatus") == "Failed":
        fqn = entity.get("fullyQualifiedName", "")
        # FQN format: service.db.schema.table.column.testName
        parts = fqn.split(".")
        column = parts[-2] if len(parts) >= 2 else None
        failed.append(FailedTest(
            test_name=entity.get("name", "unknown"),
            test_fqn=fqn,
            column_name=column,
            failure_reason=test_case_result.get("result"),
            status=TestStatus.FAILED,
        ))

    return failed


def _assign_severity(failed_tests: list[FailedTest], table_fqn: str) -> FailureSeverity:
    """Simple heuristic — upgrade to ML-based scoring in Phase 3."""
    if len(failed_tests) >= 3:
        return FailureSeverity.CRITICAL
    if any(t.column_name in ("customer_id", "order_id", "id") for t in failed_tests):
        return FailureSeverity.HIGH
    if len(failed_tests) >= 2:
        return FailureSeverity.MEDIUM
    return FailureSeverity.LOW


async def _enrich_and_publish(event: EnrichedFailureEvent):
    """
    Background task: enrich → write to event store → publish to Redis.
    Event store write happens BEFORE Redis publish so repair agent
    can always look up the full event by event_id.
    """
    from src.db.event_store import write_event

    try:
        context = await om_client.get_table_context(event.table_fqn)
        if context:
            event.table_context = context
            event.enrichment_success = True
            logger.info(
                f"Enriched {event.table_fqn} — {len(context.columns)} columns, "
                f"{len(context.upstream_tables)} upstream tables"
            )
        else:
            event.enrichment_error = "Table not found in OpenMetadata"
            logger.warning(f"Could not enrich {event.table_fqn}")

    except Exception as e:
        event.enrichment_error = str(e)
        logger.error(f"Enrichment failed for {event.table_fqn}: {e}")

    # Write to event store FIRST — repair agent needs this
    await write_event(event)

    # Then publish to Redis stream for pipeline pickup
    try:
        msg_id = await event_bus.publish(event)
        logger.info(f"Event {event.event_id} published → Redis msg {msg_id}")
    except Exception as e:
        logger.error(f"Failed to publish event {event.event_id}: {e}")

@router.post("/webhook/om-test-failure")
async def receive_om_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    OpenMetadata calls this endpoint when a test case fails.
    We return 200 immediately — enrichment happens in the background.
    OM will retry if it doesn't get 200 within 5s.
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(f"Webhook received: eventType={raw.get('eventType')} "
                f"entityType={raw.get('entityType')}")

    # Only process test case failure events
    event_type = raw.get("eventType", "")
    entity_type = raw.get("entityType", "")

    if entity_type != "testCase" or "Failed" not in str(raw):
        logger.debug(f"Skipping non-failure event: {event_type}/{entity_type}")
        return {"status": "skipped", "reason": "not a test failure event"}

    payload = OMWebhookPayload(**raw)
    failed_tests = _extract_failed_tests(payload)

    # Extract table FQN from the test case FQN
    # Format: service.db.schema.table.column.testName → service.db.schema.table
    entity_fqn = raw.get("entityFQN", "") or (
        payload.entity or {}
    ).get("fullyQualifiedName", "")
    table_fqn = ".".join(entity_fqn.split(".")[:4]) if entity_fqn else "unknown"

    event = EnrichedFailureEvent(
        event_id=str(uuid.uuid4()),
        table_fqn=table_fqn,
        failed_tests=failed_tests,
        severity=_assign_severity(failed_tests, table_fqn),
        raw_payload=raw,
        received_at=datetime.now(),
    )

    # Return 200 immediately, enrich in background
    background_tasks.add_task(_enrich_and_publish, event)

    return {
        "status": "accepted",
        "event_id": event.event_id,
        "table_fqn": table_fqn,
        "failed_tests": len(failed_tests),
    }


@router.get("/health")
async def health():
    return {"status": "ok", "service": "aegisdb-webhook"}