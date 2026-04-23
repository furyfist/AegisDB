"""
Block Kit builders for AegisDB Slack cards.
Three states:
  1. detecting_card  — posted immediately when event arrives (pipeline still running)
  2. proposal_card   — replaces detecting card once proposal is ready
  3. resolved_card   — replaces proposal card after approve/reject completes
"""

from datetime import datetime


def _confidence_bar(confidence: float) -> str:
    """Render a 10-block confidence bar using unicode blocks."""
    filled = round(confidence * 10)
    bar = "█" * filled + "░" * (10 - filled)
    pct = int(confidence * 100)
    return f"{bar}  {pct}%"


def _severity_emoji(severity: str) -> str:
    return {
        "critical": "🔴",
        "high":     "🟠",
        "medium":   "🟡",
        "low":      "🟢",
    }.get(severity.lower(), "⚪")


def detecting_card(
    event_id: str,
    table_fqn: str,
    table_name: str,
    severity: str,
) -> list[dict]:
    """
    Phase 1 card — posted the moment the event is detected.
    Pipeline is still running. No proposal yet.
    """
    emoji = _severity_emoji(severity)
    short_id = event_id[:8]

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji}  AegisDB · Anomaly Detected",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Table*\n`{table_name}`"},
                {"type": "mrkdwn", "text": f"*Severity*\n{severity.capitalize()}"},
                {"type": "mrkdwn", "text": f"*Full FQN*\n`{table_fqn}`"},
                {"type": "mrkdwn", "text": f"*Event ID*\n`{short_id}...`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":hourglass_flowing_sand:  *Pipeline running* — "
                    "detecting failure category, running diagnosis & sandbox preview...\n"
                    "_This card will update automatically when the fix proposal is ready._"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"AegisDB · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                }
            ],
        },
    ]


def proposal_card(
    proposal_id: str,
    table_name: str,
    table_fqn: str,
    failure_categories: list[str],
    root_cause: str,
    fix_description: str,
    fix_sql: str,
    confidence: float,
    sandbox_passed: bool,
    rows_affected: int,
    rows_before: int,
    rows_after: int,
    similar_fix_count: int = 0,
) -> list[dict]:
    """
    Full proposal card — replaces detecting_card once proposal is ready.
    Shows all context needed to make an approve/reject decision.
    Includes Approve / Reject buttons and an expandable SQL section.
    """
    sandbox_status = "✅  Passed" if sandbox_passed else "❌  Failed"
    categories_str = ", ".join(
        c.replace("_", " ").title() for c in failure_categories
    ) or "Unknown"

    history_note = (
        f"📚  _{similar_fix_count} similar fix(es) in knowledge base_"
        if similar_fix_count > 0
        else "📚  _No prior fixes for this table — first occurrence_"
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🔴  Fix Proposal Ready · " + table_name,
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Table*\n`{table_name}`"},
                {"type": "mrkdwn", "text": f"*Issue Type*\n{categories_str}"},
                {"type": "mrkdwn", "text": f"*Rows Affected*\n{rows_affected}  ({rows_before} → {rows_after})"},
                {"type": "mrkdwn", "text": f"*Sandbox*\n{sandbox_status}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Root Cause*\n{root_cause}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Proposed Fix*\n{fix_description}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Confidence*\n{_confidence_bar(confidence)}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{fix_sql}```",
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": history_note}],
        },
        {
            "type": "actions",
            "block_id": f"proposal_actions_{proposal_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅  Approve Fix", "emoji": True},
                    "style": "primary",
                    "action_id": "approve_proposal",
                    "value": proposal_id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve this fix?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"This will apply the fix to *{table_name}* affecting *{rows_affected} rows*.\nSandbox: {sandbox_status}",
                        },
                        "confirm": {"type": "plain_text", "text": "Yes, apply it"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌  Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "reject_proposal",
                    "value": proposal_id,
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Proposal ID: `{proposal_id[:8]}...`  ·  AegisDB · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                }
            ],
        },
    ]
    return blocks


def resolved_card(
    table_name: str,
    outcome: str,           # "approved" | "rejected" | "completed" | "failed"
    rows_affected: int,
    decided_by: str,
    reason: str = "",
    dry_run: bool = True,
) -> list[dict]:
    """
    Final card state — replaces proposal card after decision is made.
    """
    if outcome == "completed":
        mode = "DRY RUN" if dry_run else "LIVE"
        header = f"✅  Fix Applied · {table_name}"
        body = (
            f"*{rows_affected} rows* updated successfully.  "
            f"Post-apply verification passed.\n"
            f"Mode: `{mode}`  ·  Approved by: {decided_by}"
        )
    elif outcome == "approved":
        header = f"⏳  Executing Fix · {table_name}"
        body = f"Fix approved by {decided_by}. Apply agent is running..."
    elif outcome == "rejected":
        header = f"❌  Proposal Rejected · {table_name}"
        body = (
            f"Rejected by {decided_by}.\n"
            f"*Reason:* {reason or 'No reason provided.'}\n"
            "_Rejection reason stored in knowledge base for future reference._"
        )
    elif outcome == "failed":
        header = f"💥  Fix Failed · {table_name}"
        body = "The fix could not be applied. Check the audit log for details."
    else:
        header = f"ℹ️  {table_name} · {outcome}"
        body = reason or ""

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header, "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"AegisDB · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                }
            ],
        },
    ]


def rejection_modal(proposal_id: str, table_name: str) -> dict:
    """
    Slack modal for capturing rejection reason.
    Opened when engineer clicks Reject.
    """
    return {
        "type": "modal",
        "callback_id": "rejection_modal_submit",
        "private_metadata": proposal_id,
        "title": {"type": "plain_text", "text": "Reject Proposal"},
        "submit": {"type": "plain_text", "text": "Confirm Reject"},
        "close":  {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Rejecting fix proposal for *{table_name}*.\nYour reason will be stored in the knowledge base to improve future proposals.",
                },
            },
            {
                "type": "input",
                "block_id": "rejection_reason_block",
                "label": {"type": "plain_text", "text": "Why are you rejecting this?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "rejection_reason_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. This column feeds billing reports — NULL→Unknown breaks aggregations",
                    },
                    "multiline": True,
                },
            },
            {
                "type": "input",
                "block_id": "alternative_block",
                "label": {"type": "plain_text", "text": "What should happen instead? (optional)"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "alternative_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. Fix the ETL pipeline source instead of patching the data",
                    },
                    "multiline": True,
                },
            },
        ],
    }