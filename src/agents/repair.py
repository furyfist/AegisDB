import asyncio
import logging
import redis.asyncio as redis

from src.core.config import settings
from src.core.models import (
    DiagnosisResult,
    EnrichedFailureEvent,
    RepairDecision,
    SandboxResult,
)
from src.sandbox.executor import run_sandbox
from src.db.vector_store import vector_store

logger = logging.getLogger(__name__)


class RepairAgent:
    """
    Reads the aegisdb:repair stream.
    For each repairable diagnosis:
      1. Run sandbox (with retries)
      2. If passes → publish to aegisdb:apply
      3. If all retries fail → publish to aegisdb:escalation
    """

    def __init__(self):
        self._redis: redis.Redis | None = None
        self._running = False
        self._consumer_name = "repair-agent-1"
        self._group = settings.redis_consumer_group

    async def connect(self):
        self._redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
        await self._redis.ping()

        for stream in [settings.redis_repair_stream, settings.redis_apply_stream]:
            try:
                await self._redis.xgroup_create(
                    stream, self._group, id="0", mkstream=True
                )
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise
        logger.info("[RepairAgent] Connected and consumer groups ready")

    async def start(self):
        self._running = True
        logger.info(
            f"[RepairAgent] Listening on '{settings.redis_repair_stream}'"
        )

        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams={settings.redis_repair_stream: ">"},
                    count=1,
                    block=2000,
                )

                if not messages:
                    continue

                for _, stream_messages in messages:
                    for msg_id, fields in stream_messages:
                        await self._handle_message(msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RepairAgent] Loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _handle_message(self, msg_id: str, fields: dict):
        event_id = fields.get("event_id", "unknown")
        logger.info(f"[RepairAgent] Processing msg={msg_id} event={event_id}")

        try:
            diagnosis = DiagnosisResult.model_validate_json(fields["data"])

            # We need the original event for context — reconstruct minimal version
            # In production you'd store events in Redis or a DB for lookup
            # For now build a minimal EnrichedFailureEvent from diagnosis data
            event = _minimal_event_from_diagnosis(diagnosis, fields)

            decision = await self._run_with_retries(event, diagnosis)
            await self._route_decision(decision)

            await self._redis.xack(
                settings.redis_repair_stream, self._group, msg_id
            )
            logger.info(
                f"[RepairAgent] ACK msg={msg_id} approved={decision.approved}"
            )

        except Exception as e:
            logger.error(
                f"[RepairAgent] Failed to process msg={msg_id}: {e}",
                exc_info=True,
            )

    async def _run_with_retries(
        self,
        event: EnrichedFailureEvent,
        diagnosis: DiagnosisResult,
    ) -> RepairDecision:
        """
        Run sandbox up to SANDBOX_MAX_RETRIES times.
        Each retry uses the same fix SQL — if it keeps failing,
        the fix is genuinely wrong and needs human review.
        """
        last_result: SandboxResult | None = None

        for attempt in range(1, settings.sandbox_max_retries + 1):
            logger.info(
                f"[RepairAgent] Sandbox attempt {attempt}/{settings.sandbox_max_retries}"
            )
            result = await run_sandbox(event, diagnosis, attempt=attempt)
            last_result = result

            if result.sandbox_passed:
                logger.info(f"[RepairAgent] Sandbox PASSED on attempt {attempt}")
                break

            if attempt < settings.sandbox_max_retries:
                logger.warning(
                    f"[RepairAgent] Sandbox failed attempt {attempt}: {result.error} "
                    f"— retrying in 2s"
                )
                await asyncio.sleep(2)
        else:
            logger.error(
                f"[RepairAgent] All {settings.sandbox_max_retries} sandbox attempts failed"
            )

        proposal = diagnosis.repair_proposal
        approved = last_result.sandbox_passed if last_result else False

        decision = RepairDecision(
            event_id=event.event_id,
            table_fqn=event.table_fqn,
            table_name=last_result.table_name if last_result else "",
            fix_sql=proposal.fix_sql if proposal else "",
            rollback_sql=proposal.rollback_sql if proposal else None,
            fix_description=proposal.fix_description if proposal else "",
            sandbox_result=last_result,
            diagnosis_result_json=diagnosis.model_dump_json(),
            approved=approved,
            rejection_reason=(
                None if approved
                else f"Sandbox failed after {settings.sandbox_max_retries} attempts: "
                     f"{last_result.error if last_result else 'unknown'}"
            ),
            dry_run=settings.dry_run,
        )

        # Win or lose — store in KB so future diagnoses learn from it
        if approved and proposal:
            try:
                vector_store.store_fix(
                    table_fqn=event.table_fqn,
                    failure_category=diagnosis.failure_categories[0].value,
                    problem_description=diagnosis.root_cause,
                    fix_sql=proposal.fix_sql,
                    was_successful=True,
                    diagnosis_result=diagnosis,
                )
                logger.info("[RepairAgent] Fix stored in knowledge base")
            except Exception as e:
                logger.warning(f"[RepairAgent] KB store failed (non-critical): {e}")

        return decision

    async def _route_decision(self, decision: RepairDecision):
        payload = {
            "event_id": decision.event_id,
            "table_fqn": decision.table_fqn,
            "approved": str(decision.approved),
            "dry_run": str(decision.dry_run),
            "data": decision.model_dump_json(),
        }

        if decision.approved:
            stream = settings.redis_apply_stream
            logger.info(
                f"[RepairAgent] → APPLY stream "
                f"dry_run={decision.dry_run} "
                f"sql={decision.fix_sql[:60]}..."
            )
        else:
            stream = settings.redis_escalation_stream
            logger.warning(
                f"[RepairAgent] → ESCALATION stream: {decision.rejection_reason}"
            )

        await self._redis.xadd(stream, payload, maxlen=500)

    def stop(self):
        self._running = False

    async def close(self):
        self.stop()
        if self._redis:
            await self._redis.aclose()


def _minimal_event_from_diagnosis(
    diagnosis: DiagnosisResult,
    fields: dict,
) -> EnrichedFailureEvent:
    """
    Reconstruct enough of EnrichedFailureEvent to run the sandbox.
    In Phase 5 we'll add an event store for proper lookup.
    """
    from src.core.models import EnrichedFailureEvent, FailedTest, TestStatus

    table_fqn = fields.get("table_fqn", "unknown")

    failed_tests = []
    for cat in diagnosis.failure_categories:
        failed_tests.append(FailedTest(
            test_name=cat.value,
            test_fqn=f"{table_fqn}.{cat.value}",
            column_name=_guess_column(cat.value, table_fqn),
            failure_reason=diagnosis.root_cause,
            status=TestStatus.FAILED,
        ))

    return EnrichedFailureEvent(
        event_id=diagnosis.event_id,
        table_fqn=table_fqn,
        failed_tests=failed_tests,
    )


def _guess_column(category: str, table_fqn: str) -> str:
    """Heuristic column guess from category — good enough for sandbox."""
    mapping = {
        "null_violation": "customer_id",
        "range_violation": "amount",
        "uniqueness_violation": "email",
        "referential_integrity": "customer_id",
        "format_violation": "email",
    }
    return mapping.get(category, "id")


repair_agent = RepairAgent()