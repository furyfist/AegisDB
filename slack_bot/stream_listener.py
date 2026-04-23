"""
Redis Streams consumer for the Slack bot.
Listens on aegisdb:slack (published by slack_notifier.py after proposal save).

Two responsibilities:
  1. Post the proposal card to #aegis-ops when a new proposal arrives
  2. Store thread_ts → proposal_id mapping so app.py can update cards
     and so in-thread Q&A knows which proposal a message belongs to

Runs as a standalone asyncio loop — separate OS process from FastAPI.
"""

import asyncio
import json
import logging

import httpx
import redis.asyncio as redis
from slack_sdk.web.async_client import AsyncWebClient

from slack_bot.blocks import detecting_card, proposal_card
from slack_bot.config import slack_settings

logger = logging.getLogger(__name__)

SLACK_STREAM      = "aegisdb:slack"
CONSUMER_GROUP    = "slack-bot"
CONSUMER_NAME     = "slack-bot-listener-1"

# In-memory state — maps proposal_id → Slack thread ts + channel
# Structure: { proposal_id: {"ts": "...", "channel": "..."} }
# Shared with app.py via module-level dict (single process)
proposal_message_map: dict[str, dict] = {}


class SlackStreamListener:

    def __init__(self):
        self._redis: redis.Redis | None = None
        self._slack: AsyncWebClient | None = None
        self._running = False

    async def connect(self):
        self._redis = redis.Redis(
            host=slack_settings.redis_host,
            port=slack_settings.redis_port,
            decode_responses=True,
        )
        await self._redis.ping()

        self._slack = AsyncWebClient(token=slack_settings.slack_bot_token)

        # Create consumer group — idempotent, same pattern as stream_consumer.py
        try:
            await self._redis.xgroup_create(
                SLACK_STREAM,
                CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
            logger.info(f"[SlackListener] Consumer group '{CONSUMER_GROUP}' created")
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.info("[SlackListener] Consumer group already exists — continuing")
            else:
                raise

        logger.info("[SlackListener] Connected to Redis and Slack")

    def stop(self):
        self._running = False

    async def close(self):
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def start(self):
        """Main listener loop — mirrors StreamConsumer.start() pattern exactly."""
        self._running = True
        logger.info(f"[SlackListener] Listening on '{SLACK_STREAM}'")

        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={SLACK_STREAM: ">"},
                    count=1,
                    block=2000,
                )

                if not messages:
                    continue

                for _stream, stream_messages in messages:
                    for msg_id, fields in stream_messages:
                        await self._process_message(msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SlackListener] Loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

        logger.info("[SlackListener] Stopped")

    async def _process_message(self, msg_id: str, fields: dict):
        """
        Handle a single aegisdb:slack stream message.
        ACK only on success — same guarantee as the main pipeline.
        """
        proposal_id = fields.get("proposal_id", "unknown")
        event_type  = fields.get("event_type", "new_proposal")
        logger.info(f"[SlackListener] Processing msg={msg_id} proposal={proposal_id} type={event_type}")

        try:
            if event_type == "new_proposal":
                await self._handle_new_proposal(fields)
            else:
                logger.warning(f"[SlackListener] Unknown event_type={event_type} — skipping")

            await self._redis.xack(SLACK_STREAM, CONSUMER_GROUP, msg_id)
            logger.info(f"[SlackListener] ACK msg={msg_id}")

        except Exception as e:
            logger.error(f"[SlackListener] Failed msg={msg_id}: {e}", exc_info=True)
            # Do NOT ACK — Redis will redeliver

    async def _handle_new_proposal(self, fields: dict):
        """
        1. Post detecting card immediately (pipeline already done, but card
           sets the visual anchor for the thread)
        2. Fetch full proposal from AegisDB API
        3. Update card to full proposal card with Approve/Reject buttons
        """
        proposal_id        = fields["proposal_id"]
        table_name         = fields.get("table_name", "unknown")
        table_fqn          = fields.get("table_fqn", "")
        failure_categories = [
            c for c in fields.get("failure_categories", "").split(",") if c
        ]
        confidence  = float(fields.get("confidence", "0"))
        rows_affected = int(fields.get("rows_affected", "0"))

        channel = slack_settings.slack_ops_channel

        # ── Step 1: Post the "Detected" placeholder card ──────────────────
        # We already have a proposal at this point (slack_notifier fires after
        # create_proposal), but we show the detecting card for 1-2s while we
        # fetch the full detail — gives the card a "live" feel.
        post_resp = await self._slack.chat_postMessage(
            channel=channel,
            text=f"🔴 AegisDB detected an anomaly on `{table_name}` — building proposal...",
            blocks=detecting_card(
                event_id=proposal_id,  # use proposal_id as display ID
                table_fqn=table_fqn,
                table_name=table_name,
                severity=self._infer_severity(confidence),
            ),
        )

        ts      = post_resp["ts"]
        channel_id = post_resp["channel"]

        # Store mapping immediately so button handlers can find this message
        proposal_message_map[proposal_id] = {
            "ts":         ts,
            "channel":    channel_id,
            "table_name": table_name,
            "table_fqn":  table_fqn,
            "event_id":   proposal.get("event_id", "") if proposal else "",
        }
        logger.info(f"[SlackListener] Posted detecting card ts={ts} for proposal={proposal_id}")

        # ── Step 2: Fetch full proposal from API ──────────────────────────
        proposal = await self._fetch_proposal(proposal_id)
        if not proposal:
            logger.error(f"[SlackListener] Could not fetch proposal {proposal_id} — card left in detecting state")
            return

        # ── Step 3: Count similar fixes from proposal's diagnosis_json ────
        similar_fix_count = self._extract_similar_fix_count(proposal)

        # ── Step 4: Update card to full proposal (V1: pass sample diff data) ─────
        # Parse sample arrays — API returns them as lists of dicts
        raw_before = proposal.get("sample_before") or []
        raw_after  = proposal.get("sample_after")  or []

        # Defensive: ensure they are lists, not strings (API serialization edge case)
        if isinstance(raw_before, str):
            import json as _json
            try:
                raw_before = _json.loads(raw_before)
            except Exception:
                raw_before = []
        if isinstance(raw_after, str):
            import json as _json
            try:
                raw_after = _json.loads(raw_after)
            except Exception:
                raw_after = []

        await self._slack.chat_update(
            channel=channel_id,
            ts=ts,
            text=f"🔴 Fix proposal ready for `{table_name}` — {rows_affected} rows affected",
            blocks=proposal_card(
                proposal_id=proposal_id,
                table_name=proposal.get("table_name", table_name),
                table_fqn=proposal.get("table_fqn", table_fqn),
                failure_categories=proposal.get("failure_categories", failure_categories),
                root_cause=proposal.get("root_cause", ""),
                fix_description=proposal.get("fix_description", ""),
                fix_sql=proposal.get("fix_sql", ""),
                confidence=proposal.get("confidence", confidence),
                sandbox_passed=proposal.get("sandbox_passed", False),
                rows_affected=proposal.get("rows_affected", rows_affected),
                rows_before=proposal.get("rows_before", 0),
                rows_after=proposal.get("rows_after", 0),
                similar_fix_count=similar_fix_count,
                sample_before=raw_before,   # V1 — diff table
                sample_after=raw_after,     # V1 — diff table
            ),
        )

        logger.info(f"[SlackListener] Updated to proposal card for proposal={proposal_id}")

        # ── Step 5: DM the table owner if mapped ──────────────────────────
        owner_uid = slack_settings.table_owner_map.get(table_name.lower())
        if owner_uid and owner_uid != "U00000000":
            try:
                await self._slack.chat_postMessage(
                    channel=owner_uid,
                    text=(
                        f"👋 Hey — AegisDB generated a fix proposal for "
                        f"`{table_name}` that needs your review.\n"
                        f"Check <#{channel_id}> to approve or reject."
                    ),
                )
                logger.info(f"[SlackListener] DM sent to owner {owner_uid}")
            except Exception as e:
                logger.warning(f"[SlackListener] DM to owner failed (non-fatal): {e}")

    async def _fetch_proposal(self, proposal_id: str) -> dict | None:
        """
        Fetch full proposal detail from AegisDB API.
        GET /api/v1/proposals/{proposal_id}
        """
        url = f"{slack_settings.aegisdb_base_url}/api/v1/proposals/{proposal_id}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
                logger.error(f"[SlackListener] API returned {resp.status_code} for proposal {proposal_id}")
                return None
        except Exception as e:
            logger.error(f"[SlackListener] API fetch failed: {e}")
            return None

    def _infer_severity(self, confidence: float) -> str:
        """Map confidence to severity label for the detecting card."""
        if confidence >= 0.90:
            return "critical"
        if confidence >= 0.75:
            return "high"
        if confidence >= 0.60:
            return "medium"
        return "low"

    def _extract_similar_fix_count(self, proposal: dict) -> int:
        """
        Parse diagnosis_json to count similar fixes used by the LLM.
        Returns 0 on any failure — purely cosmetic field.
        """
        try:
            diag = json.loads(proposal.get("diagnosis_json", "{}"))
            return len(diag.get("similar_fixes_used", []))
        except Exception:
            return 0


# Module singleton
stream_listener = SlackStreamListener()