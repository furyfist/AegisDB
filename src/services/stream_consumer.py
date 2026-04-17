import asyncio
import json
import logging
import redis.asyncio as redis

from src.core.config import settings
from src.core.models import EnrichedFailureEvent, DiagnosisResult
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
        """Publish diagnosis result to the appropriate downstream stream."""
        payload = {
            "event_id": result.event_id,
            "table_fqn": event.table_fqn,
            "confidence": str(result.confidence),
            "is_repairable": str(result.is_repairable),
            "data": result.model_dump_json(),
        }

        if result.is_repairable:
            stream = settings.redis_repair_stream
            logger.info(
                f"[Router] → REPAIR stream | confidence={result.confidence:.2f} | "
                f"fix={result.repair_proposal.fix_sql[:60] if result.repair_proposal else 'none'}..."
            )
        else:
            stream = settings.redis_escalation_stream
            logger.info(
                f"[Router] → ESCALATION stream | reason={result.escalation_reason}"
            )

        await self._redis.xadd(stream, payload, maxlen=500)

    def stop(self):
        self._running = False

    async def close(self):
        self.stop()
        if self._redis:
            await self._redis.aclose()


stream_consumer = StreamConsumer()