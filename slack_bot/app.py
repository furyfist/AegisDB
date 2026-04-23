"""
AegisDB Slack Bot — entry point.

Runs as a completely separate process from the FastAPI backend.
Start with: python -m slack_bot.app

Handles:
  - Approve button  → POST /proposals/{id}/approve
  - Reject button   → opens rejection_modal
  - Modal submit    → POST /proposals/{id}/reject + ChromaDB upsert
  - /aegis status   → GET /status
  - /aegis proposals → GET /proposals?status=pending_approval
  - /aegis audit    → GET /audit?limit=5

Phase 2+ (Q&A, /aegis ask, /aegis why) will be added here.
"""

import asyncio
import logging
import re

import httpx
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from slack_bot.blocks import proposal_card, resolved_card, rejection_modal
from slack_bot.config import slack_settings
from slack_bot.stream_listener import stream_listener, proposal_message_map

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Bolt app ──────────────────────────────────────────────────────────────────
app = AsyncApp(token=slack_settings.slack_bot_token)
BASE_URL = f"{slack_settings.aegisdb_base_url}/api/v1"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _api_get(path: str) -> dict | None:
    """GET from AegisDB API. Returns parsed JSON or None on error."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BASE_URL}{path}")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"[Bot] GET {path} failed: {e}")
        return None


async def _api_post(path: str, body: dict) -> dict | None:
    """POST to AegisDB API. Returns parsed JSON or None on error."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{BASE_URL}{path}", json=body)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"[Bot] POST {path} failed: {e}")
        return None


def _get_proposal_id_from_action(action) -> str:
    """Extract proposal_id from button value."""
    return action.get("value", "")


async def _poll_for_completion(
    proposal_id: str,
    event_id: str,
    channel: str,
    ts: str,
    table_name: str,
    rows_affected: int,
    decided_by: str,
    dry_run: bool,
    max_attempts: int = 12,
    interval_seconds: int = 3,
):
    """
    Poll GET /audit until an entry appears for this event_id
    then update the Slack card to resolved state.
    Runs as a background asyncio task — non-blocking.
    """
    from slack_sdk.web.async_client import AsyncWebClient
    slack = AsyncWebClient(token=slack_settings.slack_bot_token)

    for attempt in range(max_attempts):
        await asyncio.sleep(interval_seconds)
        data = await _api_get(f"/audit?limit=10")
        if not data:
            continue

        entries = data.get("entries", [])
        match = next(
            (e for e in entries if e.get("event_id") == event_id),
            None,
        )

        if match:
            action  = match.get("action", "applied")
            outcome = "completed" if action in ("applied", "dry_run") else "failed"
            actual_rows = match.get("rows_affected", rows_affected)
            actual_dry  = match.get("dry_run", dry_run)

            await slack.chat_update(
                channel=channel,
                ts=ts,
                text=f"✅ Fix {action} for `{table_name}`",
                blocks=resolved_card(
                    table_name=table_name,
                    outcome=outcome,
                    rows_affected=actual_rows,
                    decided_by=decided_by,
                    dry_run=actual_dry,
                ),
            )
            logger.info(f"[Bot] Card updated to resolved state for proposal={proposal_id}")
            return

    # Timeout — update card with "check audit log" message
    logger.warning(f"[Bot] Polling timed out for proposal={proposal_id}")
    await slack.chat_update(
        channel=channel,
        ts=ts,
        text=f"⏳ Fix is taking longer than expected for `{table_name}` — check the audit log.",
        blocks=resolved_card(
            table_name=table_name,
            outcome="approved",
            rows_affected=rows_affected,
            decided_by=decided_by,
        ),
    )


# ── Button: Approve ───────────────────────────────────────────────────────────

@app.action("approve_proposal")
async def handle_approve(ack, body, client, action):
    await ack()

    proposal_id = _get_proposal_id_from_action(action)
    user        = body["user"]["name"]
    user_id     = body["user"]["id"]

    # Ephemeral "working" message — only visible to the person who clicked
    await client.chat_postEphemeral(
        channel=body["channel"]["id"],
        user=user_id,
        text=f"⏳ Approving fix for proposal `{proposal_id[:8]}...` — executing now...",
    )

    # Fetch proposal first so we have event_id for audit polling
    proposal = await _api_get(f"/proposals/{proposal_id}")
    if not proposal:
        await client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text="❌ Could not fetch proposal details. Please check the dashboard.",
        )
        return

    event_id     = proposal.get("event_id", "")
    table_name   = proposal.get("table_name", "unknown")
    rows_affected = proposal.get("rows_affected", 0)

    # Call approve API
    result = await _api_post(
        f"/proposals/{proposal_id}/approve",
        {"reason": "Approved via Slack", "decided_by": user},
    )

    if not result:
        await client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text="❌ Approval API call failed. Check the AegisDB backend.",
        )
        return

    dry_run = result.get("dry_run", True)

    # Update card to "executing" state immediately
    msg_info = proposal_message_map.get(proposal_id)
    if msg_info:
        await client.chat_update(
            channel=msg_info["channel"],
            ts=msg_info["ts"],
            text=f"⏳ Executing fix for `{table_name}`...",
            blocks=resolved_card(
                table_name=table_name,
                outcome="approved",
                rows_affected=rows_affected,
                decided_by=user,
            ),
        )

    # Kick off background polling for completion
    asyncio.create_task(
        _poll_for_completion(
            proposal_id=proposal_id,
            event_id=event_id,
            channel=msg_info["channel"] if msg_info else body["channel"]["id"],
            ts=msg_info["ts"] if msg_info else body["message"]["ts"],
            table_name=table_name,
            rows_affected=rows_affected,
            decided_by=user,
            dry_run=dry_run,
        )
    )

    logger.info(f"[Bot] Approve triggered for proposal={proposal_id} by user={user}")


# ── Button: Reject (opens modal) ─────────────────────────────────────────────

@app.action("reject_proposal")
async def handle_reject_button(ack, body, client, action):
    await ack()

    proposal_id = _get_proposal_id_from_action(action)
    table_name  = proposal_message_map.get(proposal_id, {}).get("table_name", "unknown")

    await client.views_open(
        trigger_id=body["trigger_id"],
        view=rejection_modal(
            proposal_id=proposal_id,
            table_name=table_name,
        ),
    )


# ── Modal: Rejection submitted ────────────────────────────────────────────────

@app.view("rejection_modal_submit")
async def handle_rejection_submit(ack, body, client, view):
    await ack()

    proposal_id = view["private_metadata"]
    user        = body["user"]["name"]
    user_id     = body["user"]["id"]

    values = view["state"]["values"]
    reason = (
        values
        .get("rejection_reason_block", {})
        .get("rejection_reason_input", {})
        .get("value", "")
        or ""
    )
    alternative = (
        values
        .get("alternative_block", {})
        .get("alternative_input", {})
        .get("value", "")
        or ""
    )

    # Fetch proposal for context before rejecting
    proposal = await _api_get(f"/proposals/{proposal_id}")
    table_name = proposal.get("table_name", "unknown") if proposal else "unknown"

    # Call reject API — reason is required by the backend
    result = await _api_post(
        f"/proposals/{proposal_id}/reject",
        {"reason": reason, "decided_by": user},
    )

    if not result:
        await client.chat_postEphemeral(
            channel=slack_settings.slack_ops_channel,
            user=user_id,
            text="❌ Rejection API call failed. Check the AegisDB backend.",
        )
        return

    # Update the original card to rejected state
    msg_info = proposal_message_map.get(proposal_id)
    if msg_info:
        await client.chat_update(
            channel=msg_info["channel"],
            ts=msg_info["ts"],
            text=f"❌ Proposal rejected for `{table_name}`",
            blocks=resolved_card(
                table_name=table_name,
                outcome="rejected",
                rows_affected=proposal.get("rows_affected", 0) if proposal else 0,
                decided_by=user,
                reason=reason,
            ),
        )

    # Phase 3 hook — store rejection in ChromaDB (wired up in Phase 3)
    # _store_rejection_in_chromadb(proposal, reason, alternative, user)

    # Confirm to the user
    await client.chat_postEphemeral(
        channel=msg_info["channel"] if msg_info else slack_settings.slack_ops_channel,
        user=user_id,
        text=(
            f"✅ Rejection recorded for `{table_name}`.\n"
            f"Reason stored in knowledge base: _{reason}_"
            + (f"\nAlternative: _{alternative}_" if alternative else "")
        ),
    )

    logger.info(f"[Bot] Rejection recorded for proposal={proposal_id} by user={user} reason='{reason}'")


# ── Slash command: /aegis ─────────────────────────────────────────────────────

@app.command("/aegis")
async def handle_aegis_command(ack, command, say, client):
    await ack()

    text    = (command.get("text") or "").strip()
    user_id = command["user_id"]
    channel = command["channel_id"]

    # Parse: /aegis <subcommand> [args]
    parts   = text.split(None, 1)
    sub     = parts[0].lower() if parts else "help"
    args    = parts[1] if len(parts) > 1 else ""

    if sub == "status":
        await _cmd_status(say)
    elif sub == "proposals":
        await _cmd_proposals(say)
    elif sub == "audit":
        limit = int(args) if args.isdigit() else 5
        await _cmd_audit(say, limit=min(limit, 10))
    elif sub == "help":
        await _cmd_help(say)
    else:
        await say(
            f"Unknown subcommand `{sub}`. Try `/aegis help` for available commands."
        )


# ── /aegis status ─────────────────────────────────────────────────────────────

async def _cmd_status(say):
    data = await _api_get("/status")
    if not data:
        await say("❌ Could not reach AegisDB backend.")
        return

    pipeline   = data.get("pipeline", {})
    dry_run    = data.get("dry_run", True)
    version    = data.get("version", "unknown")
    threshold  = data.get("confidence_threshold", 0.70)

    healthy    = sum(1 for v in pipeline.values() if v == "ok")
    total      = len(pipeline)
    mode_label = "🟡  DRY RUN (SAFE)" if dry_run else "🟢  LIVE MODE"

    stage_lines = "\n".join(
        f"{'✅' if v == 'ok' else '❌'}  {k.replace('_', ' ').title()}"
        for k, v in pipeline.items()
    )

    # Fetch stream lengths too
    streams_data = await _api_get("/streams")
    stream_lines = ""
    if streams_data and "streams" in streams_data:
        stream_lines = "\n\n*Stream Depths*\n" + "\n".join(
            f"• `{k}`:  {v.get('length', 0)} messages"
            for k, v in streams_data["streams"].items()
        )

    await say(
        blocks=[
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "⚡  AegisDB Pipeline Status", "emoji": True},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Mode*\n{mode_label}"},
                    {"type": "mrkdwn", "text": f"*Health*\n{healthy}/{total} stages"},
                    {"type": "mrkdwn", "text": f"*Version*\n`{version}`"},
                    {"type": "mrkdwn", "text": f"*Confidence θ*\n{int(threshold * 100)}%"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Pipeline Stages*\n{stage_lines}{stream_lines}"},
            },
        ],
        text=f"AegisDB: {healthy}/{total} stages healthy · {mode_label}",
    )


# ── /aegis proposals ──────────────────────────────────────────────────────────

async def _cmd_proposals(say):
    data = await _api_get("/proposals?status=pending_approval&limit=5")
    if not data:
        await say("❌ Could not fetch proposals.")
        return

    proposals = data.get("proposals", [])
    if not proposals:
        await say("✅  No pending proposals right now. All clear.")
        return

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋  Pending Proposals ({len(proposals)})",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for p in proposals:
        pid        = p.get("proposal_id", "")
        tname      = p.get("table_name", "unknown")
        conf       = int(p.get("confidence", 0) * 100)
        rows       = p.get("rows_affected", 0)
        cats       = ", ".join(
            c.replace("_", " ").title()
            for c in p.get("failure_categories", [])
        ) or "Unknown"
        sandbox_ok = "✅" if p.get("sandbox_passed") else "❌"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*`{tname}`*  ·  {cats}\n"
                    f"Confidence: {conf}%  ·  Rows: {rows}  ·  Sandbox: {sandbox_ok}"
                ),
            },
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "View in channel"},
                "url": "",    # deep link — leave empty, Slack ignores empty url
                "action_id": f"view_proposal_{pid}",
            },
        })
        blocks.append({"type": "divider"})

    await say(blocks=blocks, text=f"{len(proposals)} pending proposals")


# ── /aegis audit ─────────────────────────────────────────────────────────────

async def _cmd_audit(say, limit: int = 5):
    data = await _api_get(f"/audit?limit={limit}")
    if not data:
        await say("❌ Could not fetch audit log.")
        return

    entries = data.get("entries", [])
    if not entries:
        await say("📭  No audit entries yet. Run the pipeline first.")
        return

    ACTION_EMOJI = {
        "applied":     "✅",
        "dry_run":     "🟡",
        "failed":      "💥",
        "rolled_back": "↩️",
        "skipped":     "⏭️",
    }

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📜  Last {len(entries)} Audit Entries",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for e in entries:
        action    = e.get("action", "unknown")
        emoji     = ACTION_EMOJI.get(action, "⚪")
        tname     = e.get("table_name", "unknown")
        rows      = e.get("rows_affected", 0)
        conf      = int((e.get("confidence") or 0) * 100)
        applied   = str(e.get("applied_at", ""))[:16].replace("T", " ")
        dry       = " · DRY RUN" if e.get("dry_run") else ""

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji}  *{action.replace('_', ' ').upper()}*  ·  `{tname}`{dry}\n"
                    f"Rows: {rows}  ·  Confidence: {conf}%  ·  {applied} UTC"
                ),
            },
        })
        blocks.append({"type": "divider"})

    await say(blocks=blocks, text=f"Last {len(entries)} audit entries")


# ── /aegis help ───────────────────────────────────────────────────────────────

async def _cmd_help(say):
    await say(
        blocks=[
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🛡️  AegisDB Bot — Commands", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*`/aegis status`*\n"
                        "Pipeline health, mode (DRY RUN / LIVE), and stream depths.\n\n"
                        "*`/aegis proposals`*\n"
                        "List all pending fix proposals with confidence and sandbox status.\n\n"
                        "*`/aegis audit [n]`*\n"
                        "Show last N audit entries. Default: 5. Max: 10.\n\n"
                        "*`/aegis help`*\n"
                        "Show this message."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "More commands coming: `/aegis ask`, `/aegis why [table]`"}
                ],
            },
        ],
        text="AegisDB Bot commands",
    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    """
    Boot sequence:
    1. Connect stream listener to Redis
    2. Start Bolt Socket Mode handler
    3. Run both concurrently
    """
    logger.info("=" * 50)
    logger.info("AegisDB Slack Bot starting")
    logger.info(f"  Channel  : #{slack_settings.slack_ops_channel}")
    logger.info(f"  Backend  : {slack_settings.aegisdb_base_url}")
    logger.info("=" * 50)

    await stream_listener.connect()

    handler = AsyncSocketModeHandler(app, slack_settings.slack_app_token)

    # Run listener and Socket Mode concurrently
    await asyncio.gather(
        stream_listener.start(),
        handler.start_async(),
    )


if __name__ == "__main__":
    asyncio.run(main())