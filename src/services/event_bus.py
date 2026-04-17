import json
import redis.asyncio as redis
from src.core.config import settings
from src.core.models import EnrichedFailureEvent
import logging

logger = logging.getLogger(__name__)


class EventBus:
    """
    Redis Streams publisher.
    XADD aegisdb:events * field value ...
    """

    def __init__(self):
        self._redis: redis.Redis | None = None

    async def connect(self):
        self._redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
        await self._redis.ping()
        logger.info(f"Redis connected at {settings.redis_host}:{settings.redis_port}")

    async def publish(self, event: EnrichedFailureEvent) -> str:
        """
        Publish enriched event to Redis Stream.
        Returns the stream message ID assigned by Redis.
        """
        if not self._redis:
            raise RuntimeError("EventBus not connected — call connect() first")

        payload = {
            "event_id": event.event_id,
            "table_fqn": event.table_fqn,
            "severity": event.severity.value,
            "failed_tests_count": str(len(event.failed_tests)),
            "enrichment_success": str(event.enrichment_success),
            "data": event.model_dump_json(),  # full event as JSON string
        }

        msg_id = await self._redis.xadd(
            settings.redis_stream_name,
            payload,
            maxlen=1000,  # keep last 1000 events, no unbounded growth
        )
        logger.info(f"Published event {event.event_id} to stream → {msg_id}")
        return msg_id

    async def close(self):
        if self._redis:
            await self._redis.aclose()


event_bus = EventBus()