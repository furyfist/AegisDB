import asyncio
import json
import logging
import redis.asyncio as redis

from src.core.config import settings
from src.core.models import EnrichedFailureEvent, DiagnosisResult,RepairProposalRecord
from src.agents.detector import run_detector
from src.agents.diagnosis import diagnosis_agent

logger = logging.getLogger(__name__)


class StreamConsumer:
    """
    Redis Streams consumer using consumer groups.
    Guarantees at-least-once delivery — messages are ACK'd only
    after successful processing.
    """

    def __init__(self):
        self._redis: redis.Redis | None = None
        self._running = False

    async def connect(self):
        self._redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        await self._redis.ping()

        # Create consumer group — idempotent, MKSTREAM creates stream if missing
        try:
            await self._redis.xgroup_create(
                settings.redis_stream_name,
                settings.redis_consumer_group,
                id="0",
                mkstream=True,
            )
            logger.info(
                f"Consumer group '{settings.redis_consumer_group}' created"
            )
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.info("Consumer group already exists — continuing")
            else:
                raise

    def stop(self):
        """Signal the consumer loop to exit after the current message."""
        self._running = False

    async def close(self):
        """Close the underlying Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            logger.info("[StreamConsumer] Redis connection closed")

    async def start(self):
        """Main consumer loop. Runs until stop() is called."""
        self._running = True
        logger.info(
            f"[StreamConsumer] Starting — listening on '{settings.redis_stream_name}'"
        )

        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=settings.redis_consumer_group,
                    consumername=settings.redis_consumer_name,
                    streams={settings.redis_stream_name: ">"},
                    count=1,        # one message at a time — predictable processing
                    block=2000,     # block 2s waiting for messages
                )

                if not messages:
                    continue

                for stream_name, stream_messages in messages:
                    for msg_id, fields in stream_messages:
                        await self._process_message(msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Consumer loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

        logger.info("[StreamConsumer] Stopped")

    async def _process_message(self, msg_id: str, fields: dict):
        """Process a single message. ACK only on success."""
        event_id = fields.get("event_id", "unknown")
        logger.info(f"[StreamConsumer] Processing msg={msg_id} event={event_id}")

        try:
            # Deserialize full event from the 'data' field
            raw_data = fields.get("data", "{}")
            event = EnrichedFailureEvent.model_validate_json(raw_data)

            # Run pipeline: Detector → Diagnosis
            detector_result = run_detector(event)
            diagnosis_result = await diagnosis_agent.run(event, detector_result)

            # Route based on diagnosis outcome
            await self._route_result(event, diagnosis_result)

            # ACK — message successfully processed
            await self._redis.xack(
                settings.redis_stream_name,
                settings.redis_consumer_group,
                msg_id,
            )
            logger.info(
                f"[StreamConsumer] ACK msg={msg_id} "
                f"repairable={diagnosis_result.is_repairable} "
                f"confidence={diagnosis_result.confidence:.2f}"
            )

        except Exception as e:
            logger.error(
                f"[StreamConsumer] Failed to process msg={msg_id}: {e}",
                exc_info=True,
            )
            # Do NOT ACK on failure — Redis will redeliver for retry

    async def _route_result(
        self,
        event: EnrichedFailureEvent,
        result: DiagnosisResult,
    ):
        """
        Phase D: Instead of publishing directly to aegisdb:repair,
        create a RepairProposalRecord and wait for user approval.
        Escalation path (low confidence) stays unchanged.
        """
        if not result.is_repairable:
            # Escalation — same as before, no approval gate needed
            payload = {
                "event_id":  result.event_id,
                "table_fqn": event.table_fqn,
                "confidence": str(result.confidence),
                "is_repairable": "False",
                "data": result.model_dump_json(),
            }
            await self._redis.xadd(
                settings.redis_escalation_stream, payload, maxlen=500
            )
            logger.info(
                f"[Router] → ESCALATION stream | reason={result.escalation_reason}"
            )
            return

        # Repairable — run sandbox preview FIRST, then create proposal
        # This gives the user real before/after data to review
        proposal = await self._build_proposal(event, result)
        await self._save_proposal(proposal)
        logger.info(
            f"[Router] → PROPOSAL created | id={proposal.proposal_id} "
            f"confidence={result.confidence:.2f} "
            f"fix={result.repair_proposal.fix_sql[:60] if result.repair_proposal else 'none'}..."
        )

    async def _build_proposal(
        self,
        event: EnrichedFailureEvent,
        diagnosis: DiagnosisResult,
    ) -> "RepairProposalRecord":
        """
        Run sandbox silently to get the data diff preview.
        The user sees exactly what will change before approving.
        """
        from src.core.models import RepairProposalRecord
        from src.sandbox.executor import run_sandbox

        proposal = diagnosis.repair_proposal
        parts     = event.table_fqn.split(".")
        table_name = parts[-1] if parts else event.table_fqn

        # Default values — used if sandbox fails
        sandbox_passed = False
        rows_before    = 0
        rows_after     = 0
        rows_affected  = 0
        sample_before: list[dict] = []
        sample_after:  list[dict] = []

        if proposal:
            try:
                sandbox_result = await run_sandbox(event, diagnosis, attempt=1)
                sandbox_passed = sandbox_result.sandbox_passed
                if sandbox_result.data_diff:
                    rows_before   = sandbox_result.data_diff.rows_before
                    rows_after    = sandbox_result.data_diff.rows_after
                    rows_affected = (
                        sandbox_result.data_diff.rows_deleted
                        + sandbox_result.data_diff.rows_updated
                    )
                    sample_before = sandbox_result.data_diff.sample_before
                    sample_after  = sandbox_result.data_diff.sample_after
            except Exception as e:
                logger.warning(
                    f"[Router] Sandbox preview failed for proposal: {e} "
                    f"— creating proposal without preview"
                )

        return RepairProposalRecord(
            event_id=event.event_id,
            table_fqn=event.table_fqn,
            table_name=table_name,
            failure_categories=[c.value for c in diagnosis.failure_categories],
            root_cause=diagnosis.root_cause,
            confidence=diagnosis.confidence,
            fix_sql=proposal.fix_sql if proposal else "",
            fix_description=proposal.fix_description if proposal else "",
            rollback_sql=proposal.rollback_sql if proposal else None,
            estimated_rows=proposal.estimated_rows_affected if proposal else None,
            sandbox_passed=sandbox_passed,
            rows_before=rows_before,
            rows_after=rows_after,
            rows_affected=rows_affected,
            sample_before=sample_before,
            sample_after=sample_after,
            diagnosis_json=diagnosis.model_dump_json(),
            event_json=event.model_dump_json(),
        )

    async def _save_proposal(self, proposal: "RepairProposalRecord"):
        from src.db.proposal_store import create_proposal
        try:
            await create_proposal(proposal)
            # ── Slack notification (non-fatal if bot not running) ──
            try:
                from slack_bot.slack_notifier import notify_proposal
                await notify_proposal(proposal)
            except Exception as slack_err:
                logger.warning(f"[Router] Slack notify skipped: {slack_err}")
        except Exception as e:
            logger.error(f"[Router] Proposal save failed: {e}")

stream_consumer = StreamConsumer()