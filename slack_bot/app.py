"""
AegisDB Slack Bot — complete app.

Run from project root:
    python -m slack_bot.app

Phases implemented:
    Phase 1 — Proposal card, Approve/Reject, /aegis status/proposals/audit/help
    Phase 2 — In-thread Q&A, /aegis ask
    Phase 3 — Rejection → ChromaDB, /aegis why
    Phase 4-6 — Ephemeral feedback, card state machine, boot hardening
"""

import asyncio
import logging
from functools import partial

import httpx
from groq import AsyncGroq
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from slack_bot.blocks import proposal_card, resolved_card, rejection_modal
from slack_bot.config import slack_settings
from slack_bot.qa_engine import (
    answer_question,
    answer_global_question,
    answer_why_table,
    build_system_prompt,
)
from slack_bot.rejection_store import rejection_store
from slack_bot.stream_listener import stream_listener, proposal_message_map

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = f"{slack_settings.aegisdb_base_url}/api/v1"

# ── Bolt app ──────────────────────────────────────────────────────────────────
app = AsyncApp(token=slack_settings.slack_bot_token)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _api_get(path: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BASE_URL}{path}")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"[Bot] GET {path} failed: {e}")
        return None


async def _api_post(path: str, body: dict) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{BASE_URL}{path}", json=body)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"[Bot] POST {path} failed: {e}")
        return None


# ── Post-approve audit poller ─────────────────────────────────────────────────

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
    Background task: polls GET /audit until the fix appears,
    then updates the Slack card to the final resolved state.
    Non-blocking — runs as asyncio.create_task().
    """
    from slack_sdk.web.async_client import AsyncWebClient
    slack = AsyncWebClient(token=slack_settings.slack_bot_token)

    for _ in range(max_attempts):
        await asyncio.sleep(interval_seconds)
        data = await _api_get("/audit?limit=10")
        if not data:
            continue

        match = next(
            (e for e in data.get("entries", []) if e.get("event_id") == event_id),
            None,
        )
        if match:
            action      = match.get("action", "applied")
            outcome     = "completed" if action in ("applied", "dry_run") else "failed"
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
            logger.info(f"[Bot] Card resolved for proposal={proposal_id} action={action}")
            return

    # Timeout — tell user to check audit log
    await slack.chat_update(
        channel=channel,
        ts=ts,
        text=f"⏳ Fix still running for `{table_name}` — check audit log.",
        blocks=resolved_card(
            table_name=table_name,
            outcome="approved",
            rows_affected=rows_affected,
            decided_by=decided_by,
        ),
    )
    logger.warning(f"[Bot] Polling timed out for proposal={proposal_id}")


# ── Phase 1: Button — Approve ─────────────────────────────────────────────────

@app.action("approve_proposal")
async def handle_approve(ack, body, client, action):
    await ack()

    proposal_id = action.get("value", "")
    user        = body["user"]["name"]
    user_id     = body["user"]["id"]
    channel_id  = body["channel"]["id"]

    await client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=f"⏳ Approving `{proposal_id[:8]}...` — executing now...",
    )

    proposal = await _api_get(f"/proposals/{proposal_id}")
    if not proposal:
        await client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="❌ Could not fetch proposal. Check the dashboard.",
        )
        return

    event_id      = proposal.get("event_id", "")
    table_name    = proposal.get("table_name", "unknown")
    rows_affected = proposal.get("rows_affected", 0)

    result = await _api_post(
        f"/proposals/{proposal_id}/approve",
        {"reason": "Approved via Slack", "decided_by": user},
    )
    if not result:
        await client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text="❌ Approval API call failed. Check AegisDB backend logs.",
        )
        return

    dry_run  = result.get("dry_run", True)
    msg_info = proposal_message_map.get(proposal_id, {})
    msg_ts   = msg_info.get("ts", body["message"]["ts"])
    msg_chan  = msg_info.get("channel", channel_id)

    # Immediately update card to "executing" state
    await client.chat_update(
        channel=msg_chan,
        ts=msg_ts,
        text=f"⏳ Executing fix for `{table_name}`...",
        blocks=resolved_card(
            table_name=table_name,
            outcome="approved",
            rows_affected=rows_affected,
            decided_by=user,
        ),
    )

    # Background: poll audit and update to final state
    asyncio.create_task(
        _poll_for_completion(
            proposal_id=proposal_id,
            event_id=event_id,
            channel=msg_chan,
            ts=msg_ts,
            table_name=table_name,
            rows_affected=rows_affected,
            decided_by=user,
            dry_run=dry_run,
        )
    )
    logger.info(f"[Bot] Approve triggered proposal={proposal_id} by {user}")


# ── Phase 1: Button — Reject (opens modal) ────────────────────────────────────

@app.action("reject_proposal")
async def handle_reject_button(ack, body, client, action):
    await ack()
    proposal_id = action.get("value", "")
    table_name  = proposal_message_map.get(proposal_id, {}).get("table_name", "unknown")

    await client.views_open(
        trigger_id=body["trigger_id"],
        view=rejection_modal(proposal_id=proposal_id, table_name=table_name),
    )

# ── Quick action buttons from /aegis status ───────────────────────────────────

@app.action("quick_proposals")
async def handle_quick_proposals(ack, say):
    await ack()
    await _cmd_proposals(say)


@app.action("quick_audit")
async def handle_quick_audit(ack, say):
    await ack()
    await _cmd_audit(say, limit=5)

# ── Phase 1+3: Modal — Rejection submitted ────────────────────────────────────

@app.view("rejection_modal_submit")
async def handle_rejection_submit(ack, body, client, view):
    await ack()

    proposal_id = view["private_metadata"]
    user        = body["user"]["name"]
    user_id     = body["user"]["id"]
    values      = view["state"]["values"]

    reason = (
        values
        .get("rejection_reason_block", {})
        .get("rejection_reason_input", {})
        .get("value", "") or ""
    )
    alternative = (
        values
        .get("alternative_block", {})
        .get("alternative_input", {})
        .get("value", "") or ""
    )

    proposal   = await _api_get(f"/proposals/{proposal_id}")
    table_name = proposal.get("table_name", "unknown") if proposal else "unknown"
    msg_info   = proposal_message_map.get(proposal_id, {})
    msg_chan    = msg_info.get("channel", slack_settings.slack_ops_channel)

    # Call reject API
    result = await _api_post(
        f"/proposals/{proposal_id}/reject",
        {"reason": reason, "decided_by": user},
    )
    if not result:
        await client.chat_postEphemeral(
            channel=msg_chan, user=user_id,
            text="❌ Rejection API call failed. Check AegisDB backend logs.",
        )
        return

    # Update card to rejected state
    msg_ts = msg_info.get("ts")
    if msg_ts:
        await client.chat_update(
            channel=msg_chan,
            ts=msg_ts,
            text=f"❌ Proposal rejected for `{table_name}`",
            blocks=resolved_card(
                table_name=table_name,
                outcome="rejected",
                rows_affected=proposal.get("rows_affected", 0) if proposal else 0,
                decided_by=user,
                reason=reason,
            ),
        )

    # Phase 3: Store rejection in ChromaDB (non-fatal)
    if proposal:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: rejection_store.store_rejection(
                    proposal_id=proposal_id,
                    table_fqn=proposal.get("table_fqn", ""),
                    table_name=table_name,
                    failure_categories=proposal.get("failure_categories", []),
                    fix_sql=proposal.get("fix_sql", ""),
                    fix_description=proposal.get("fix_description", ""),
                    rejection_reason=reason,
                    alternative=alternative,
                    decided_by=user,
                ),
            )
            logger.info(f"[Bot] Rejection stored in ChromaDB proposal={proposal_id}")
        except Exception as e:
            logger.error(f"[Bot] ChromaDB rejection store failed (non-fatal): {e}")

    await client.chat_postEphemeral(
        channel=msg_chan,
        user=user_id,
        text=(
            f"✅ Rejection recorded for `{table_name}`.\n"
            f"Reason stored in knowledge base: _{reason}_"
            + (f"\nAlternative: _{alternative}_" if alternative else "")
        ),
    )
    logger.info(f"[Bot] Rejection complete proposal={proposal_id} by {user}")

# Acknowledge all message subtypes Bolt would otherwise silently drop
@app.event({"type": "message", "subtype": "bot_message"})
async def handle_bot_message(ack):
    await ack()

@app.event({"type": "message", "subtype": "message_changed"})  
async def handle_message_changed(ack):
    await ack()

@app.event({"type": "message", "subtype": "message_deleted"})
async def handle_message_deleted(ack):
    await ack()

# ── Phase 2: In-thread Q&A ────────────────────────────────────────────────────

@app.event("message")
async def handle_thread_message(event, client, ack, logger):
    await ack()
    logger.info(f"[QA] message event: subtype={event.get('subtype')} thread_ts={event.get('thread_ts')}")  
    """
    Fires on every message event. Acts only when:
      1. Message is in a thread (thread_ts present)
      2. That thread belongs to a known proposal
      3. Message is from a human (not bot, not subtype event)
    """
    # Ignore bot messages and system subtypes (joins, leaves, etc.)
    if event.get("bot_id") or event.get("bot_profile") or event.get("subtype"):
        return

    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    # Find proposal for this thread
    proposal_id = next(
        (pid for pid, info in proposal_message_map.items()
         if info.get("ts") == thread_ts),
        None,
    )
    if not proposal_id:
        return

    question = (event.get("text") or "").strip()
    if not question:
        return

    channel = event["channel"]
    user_id = event.get("user", "")

    # Ephemeral thinking indicator
    await client.chat_postEphemeral(
        channel=channel,
        user=user_id,
        text="🤔 AegisDB is thinking...",
        thread_ts=thread_ts,
    )

    answer = await answer_question(question=question, proposal_id=proposal_id)

    await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=answer,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": answer},
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f"AegisDB · grounded in proposal `{proposal_id[:8]}...`, "
                        f"fix history, and rejection memory"
                    ),
                }],
            },
        ],
    )


# ── Phase 1+2: Slash command dispatcher ──────────────────────────────────────

@app.command("/aegis")
async def handle_aegis_command(ack, command, say, client):
    await ack()

    text  = (command.get("text") or "").strip()
    parts = text.split(None, 1)
    sub   = parts[0].lower() if parts else "help"
    args  = parts[1].strip() if len(parts) > 1 else ""

    dispatch = {
        "status":    lambda: _cmd_status(say),
        "proposals": lambda: _cmd_proposals(say),
        "audit":     lambda: _cmd_audit(say, int(args) if args.isdigit() else 5),
        "ask":       lambda: _cmd_ask(say, args),
        "why":       lambda: _cmd_why(say, args.lower()),
        "help":      lambda: _cmd_help(say),
    }

    handler = dispatch.get(sub)
    if handler:
        await handler()
    else:
        await say(f"Unknown command `{sub}`. Try `/aegis help`.")


# ── /aegis status ─────────────────────────────────────────────────────────────

async def _cmd_status(say):
    # Fetch both endpoints concurrently
    status_data, streams_data = await asyncio.gather(
        _api_get("/status"),
        _api_get("/streams"),
    )

    if not status_data:
        await say("❌ Could not reach AegisDB backend.")
        return

    pipeline  = status_data.get("pipeline", {})
    dry_run   = status_data.get("dry_run", True)
    version   = status_data.get("version", "unknown")
    threshold = status_data.get("confidence_threshold", 0.70)
    healthy   = sum(1 for v in pipeline.values() if v == "ok")
    total     = len(pipeline)
    mode      = "🟡  DRY RUN (SAFE)" if dry_run else "🟢  LIVE MODE"

    # ── Block 1: System header ────────────────────────────────────────────
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "⚡  AegisDB Pipeline Status",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Mode*\n{mode}"},
                {"type": "mrkdwn", "text": f"*Health*\n{healthy}/{total} stages"},
                {"type": "mrkdwn", "text": f"*Version*\n`{version}`"},
                {"type": "mrkdwn", "text": f"*Confidence θ*\n{int(threshold * 100)}%"},
            ],
        },
        {"type": "divider"},
    ]

    # ── Block 2: Pipeline stages as 2-column fields grid ─────────────────
    stage_fields: list[dict] = []
    for stage_key, stage_val in pipeline.items():
        emoji    = "✅" if stage_val == "ok" else "❌"
        label    = stage_key.replace("_", " ").title()
        stage_fields.append({
            "type": "mrkdwn",
            "text": f"{emoji}  *{label}*",
        })

    # Slack fields renders in 2 columns automatically
    blocks.append({
        "type": "section",
        "fields": stage_fields,
    })

    blocks.append({"type": "divider"})

    # ── Block 3: Stream depths as fields grid with warning signals ────────
    if streams_data and "streams" in streams_data:
        stream_fields: list[dict] = []
        for stream_key, stream_val in streams_data["streams"].items():
            length     = stream_val.get("length", 0)
            # Warning signal if backlog is building
            warn       = "⚠️ " if length > 10 else ""
            short_name = stream_key.replace("aegisdb:", "")
            stream_fields.append({
                "type": "mrkdwn",
                "text": f"{warn}*{short_name}*\n{length} messages",
            })

        if stream_fields:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Stream Depths*",
                },
            })
            blocks.append({
                "type": "section",
                "fields": stream_fields,
            })
            blocks.append({"type": "divider"})

    # ── Block 4: Quick action buttons ─────────────────────────────────────
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "📋  View Proposals",
                    "emoji": True,
                },
                "action_id": "quick_proposals",
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "📜  Audit Log",
                    "emoji": True,
                },
                "action_id": "quick_audit",
            },
        ],
    })

    # Footer
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"AegisDB · {_utc_now()}",
        }],
    })

    await say(
        blocks=blocks,
        text=f"AegisDB: {healthy}/{total} stages healthy · {mode}",
    )


# ── /aegis proposals ──────────────────────────────────────────────────────────

async def _cmd_proposals(say):
    data = await _api_get("/proposals?status=pending_approval&limit=5")
    if not data:
        await say("❌ Could not fetch proposals.")
        return

    proposals = data.get("proposals", [])
    if not proposals:
        await say("✅  No pending proposals right now.")
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
        pid   = p.get("proposal_id", "")
        tname = p.get("table_name", "unknown")
        conf  = int(p.get("confidence", 0) * 100)
        rows  = p.get("rows_affected", 0)
        cats  = ", ".join(
            c.replace("_", " ").title() for c in p.get("failure_categories", [])
        ) or "Unknown"
        sbox  = "✅" if p.get("sandbox_passed") else "❌"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*`{tname}`*  ·  {cats}\n"
                    f"Confidence: {conf}%  ·  Rows: {rows}  ·  Sandbox: {sbox}\n"
                    f"ID: `{pid[:8]}...`"
                ),
            },
        })
        blocks.append({"type": "divider"})

    await say(blocks=blocks, text=f"{len(proposals)} pending proposals")


# ── /aegis audit ─────────────────────────────────────────────────────────────

async def _cmd_audit(say, limit: int = 5):
    limit = min(limit, 10)
    data  = await _api_get(f"/audit?limit={limit}")
    if not data:
        await say("❌ Could not fetch audit log.")
        return

    entries = data.get("entries", [])
    if not entries:
        await say("📭  No audit entries yet.")
        return

    ACTION_EMOJI = {
        "applied": "✅", "dry_run": "🟡",
        "failed": "💥", "rolled_back": "↩️", "skipped": "⏭️",
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
        action  = e.get("action", "unknown")
        emoji   = ACTION_EMOJI.get(action, "⚪")
        tname   = e.get("table_name", "unknown")
        rows    = e.get("rows_affected", 0)
        conf    = int((e.get("confidence") or 0) * 100)
        applied = str(e.get("applied_at", ""))[:16].replace("T", " ")
        dry     = " · DRY RUN" if e.get("dry_run") else ""

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji}  *{action.replace('_', ' ').upper()}*  ·  "
                    f"`{tname}`{dry}\n"
                    f"Rows: {rows}  ·  Confidence: {conf}%  ·  {applied} UTC"
                ),
            },
        })
        blocks.append({"type": "divider"})

    await say(blocks=blocks, text=f"Last {len(entries)} audit entries")


# ── /aegis ask ────────────────────────────────────────────────────────────────

async def _cmd_ask(say, question: str):
    if not question:
        await say(
            "Usage: `/aegis ask [question]`\n"
            "Example: `/aegis ask which tables have worsening NULL trends?`"
        )
        return

    await say("🤔 Thinking...")
    answer = await answer_global_question(question)

    await say(
        blocks=[
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🤖  AegisDB Answer", "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Q: {question}*\n\n{answer}"},
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "Grounded in recent audit log and pending proposals",
                }],
            },
        ],
        text=answer,
    )


# ── /aegis why ────────────────────────────────────────────────────────────────

async def _cmd_why(say, table_name: str):
    if not table_name:
        await say(
            "Usage: `/aegis why [table_name]`\n"
            "Example: `/aegis why orders`"
        )
        return

    await say(f"🔍 Checking rejection memory for `{table_name}`...")
    synthesis = await answer_why_table(table_name)

    # Get raw rejection count for the context line
    loop = asyncio.get_running_loop()
    rejections = await loop.run_in_executor(
        None,
        partial(
            rejection_store.find_rejections_for_table,
            table_fqn=table_name,
            failure_category="",
            top_k=5,
        ),
    )
    count = len(rejections)

    await say(
        blocks=[
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📚  Rejection Memory · {table_name}",
                    "emoji": True,
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": synthesis},
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f"Source: `aegisdb_rejections` ChromaDB collection · "
                        f"{count} rejection(s) matched"
                    ),
                }],
            },
        ],
        text=synthesis,
    )


# ── /aegis help ───────────────────────────────────────────────────────────────

async def _cmd_help(say):
    await say(
        blocks=[
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🛡️  AegisDB Bot — Commands",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*`/aegis status`*\n"
                        "Pipeline health, mode, stream depths.\n\n"
                        "*`/aegis proposals`*\n"
                        "List all pending fix proposals.\n\n"
                        "*`/aegis audit [n]`*\n"
                        "Show last N audit entries (max 10).\n\n"
                        "*`/aegis ask [question]`*\n"
                        "Ask AegisDB anything about your data quality.\n\n"
                        "*`/aegis why [table]`*\n"
                        "Explain why a table keeps having issues — pulls rejection memory.\n\n"
                        "*Reply in any proposal thread*\n"
                        "Ask questions about that specific proposal inline."
                    ),
                },
            },
        ],
        text="AegisDB Bot commands",
    )


# ── Boot ──────────────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 55)
    logger.info("AegisDB Slack Bot starting")
    logger.info(f"  Channel  : #{slack_settings.slack_ops_channel}")
    logger.info(f"  Backend  : {slack_settings.aegisdb_base_url}")
    logger.info(f"  Groq key : {'SET' if slack_settings.groq_api_key else 'MISSING ⚠️'}")
    logger.info("=" * 55)

    # 1. Connect stream listener (Redis)
    await stream_listener.connect()
    logger.info("[Boot] Stream listener connected")

    # 2. Connect rejection store (ChromaDB — sync, use executor)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, rejection_store.connect)
        logger.info(
            f"[Boot] Rejection store connected "
            f"({rejection_store.count()} existing rejections)"
        )
    except Exception as e:
        # Non-fatal — rejections degrade gracefully
        logger.warning(f"[Boot] Rejection store failed (non-fatal): {e}")

    # 3. Start Socket Mode handler + stream listener concurrently
    handler = AsyncSocketModeHandler(app, slack_settings.slack_app_token)
    logger.info("[Boot] AegisDB Slack Bot ready ✓")

    await asyncio.gather(
        stream_listener.start(),
        handler.start_async(),
    )


if __name__ == "__main__":
    asyncio.run(main())