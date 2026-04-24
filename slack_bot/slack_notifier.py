"""
Thin Redis publisher called by stream_consumer._save_proposal().
Publishes a lightweight notification to aegisdb:slack stream so the
Slack bot's stream_listener can pick it up and post the proposal card.

This is the ONLY file in slack_bot/ that gets imported by the main backend.
Everything else in slack_bot/ is isolated to the bot process.
"""
import logging
import redis.asyncio as redis

logger = logging.getLogger(__name__)

_redis_client: redis.Redis | None = None

SLACK_STREAM = "aegisdb:slack"


async def connect(host: str = "localhost", port: int = 6379):
    global _redis_client
    _redis_client = redis.Redis(host=host, port=port, decode_responses=True)
    await _redis_client.ping()
    logger.info("[SlackNotifier] Redis connected")


async def notify_proposal(proposal) -> None:
    """
    Called after create_proposal() succeeds in stream_consumer._save_proposal().
    Publishes minimal payload — the bot fetches full details via API.
    """
    if not _redis_client:
        logger.warning("[SlackNotifier] Redis not connected — skipping Slack notify")
        return

    try:
        payload = {
            "proposal_id":         proposal.proposal_id,
            "table_name":          proposal.table_name,
            "table_fqn":           proposal.table_fqn,
            "failure_categories":  ",".join(proposal.failure_categories),
            "confidence":          str(proposal.confidence),
            "rows_affected":       str(proposal.rows_affected),
            "sandbox_passed":      str(proposal.sandbox_passed),
            "event_type":          "new_proposal",
        }
        msg_id = await _redis_client.xadd(SLACK_STREAM, payload, maxlen=200)
        logger.info(f"[SlackNotifier] Published proposal {proposal.proposal_id} → {msg_id}")
    except Exception as e:
        logger.error(f"[SlackNotifier] Failed to publish: {e}")
        # Non-fatal — bot notification failure must never break the pipeline


async def close():
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None


# Module singleton used by stream_consumer
slack_notifier_instance = None