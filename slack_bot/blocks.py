# slack_bot/blocks.py
"""
Block Kit builders for AegisDB Slack cards.

Card states:
  detecting_card   — posted immediately when event arrives
  proposal_card    — replaces detecting_card once proposal is ready
  resolved_card    — replaces proposal_card after approve/reject/complete

Visual improvements (V1-V4):
  V1 — Before/after data diff table in proposal_card
  V2 — Dynamic severity header based on confidence + failure_categories
  V3 — resolved_card as receipt with fields grid
  V4 — (in app.py _cmd_status) — this file is blocks only
"""

from datetime import datetime, timezone


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _confidence_bar(confidence: float) -> str:
    """10-block unicode progress bar."""
    filled = round(confidence * 10)
    bar    = "█" * filled + "░" * (10 - filled)
    pct    = int(confidence * 100)
    return f"{bar}  {pct}%"


def _header_style(
    failure_categories: list[str],
    confidence: float,
    sandbox_passed: bool,
) -> tuple[str, str]:
    """
    V2: Return (emoji, label) based on failure type + confidence + sandbox.
    Used to make the proposal card header dynamic instead of always 🔴.

    Priority order:
      1. Sandbox failed → always ⚠️ regardless of confidence
      2. Critical failure types at high confidence → 🔴
      3. High confidence → 🟠
      4. At threshold → 🟡
    """
    CRITICAL_TYPES = {"null_violation", "referential_integrity", "uniqueness_violation"}
    categories_set = set(failure_categories)

    if not sandbox_passed:
        return "⚠️", "Fix Proposal — Sandbox Failed"

    if confidence >= 0.90 and categories_set & CRITICAL_TYPES:
        return "🔴", "Critical Fix Proposal"

    if confidence >= 0.85:
        return "🟠", "High Priority Fix Proposal"

    if confidence >= 0.70:
        return "🟡", "Fix Proposal"

    # Below threshold — should rarely appear in a card but handle gracefully
    return "⚪", "Fix Proposal — Low Confidence"


def _format_value(val) -> str:
    """
    V1: Format a single cell value for the diff table.
    Handles None (NULL), strings, ints, floats, dicts cleanly.
    Slack mrkdwn: backticks for code, plain for values.
    """
    if val is None:
        return "`NULL`"
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        # Truncate very long strings
        if len(val) > 40:
            return f"`{val[:37]}...`"
        return f"'{val}'"
    if isinstance(val, dict):
        return "`{...}`"
    return str(val)


def _build_diff_section(
    sample_before: list[dict],
    sample_after: list[dict],
    failure_categories: list[str],
    rows_affected: int,
) -> list[dict]:
    if not sample_before:
        return [{
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "📊 _Sandbox preview not available._",
            }],
        }]

    # Find ID column
    all_cols = list(sample_before[0].keys()) if sample_before else []
    id_col = next(
        (c for c in all_cols if c == "id" or c.endswith("_id")),
        None,
    )

    # Find the column that is NULL in before sample
    # This is the column the fix targets — only show this column
    null_cols: list[str] = []
    for col in all_cols:
        for row in sample_before[:10]:
            if row.get(col) is None:
                null_cols.append(col)
                break

    if not null_cols:
        # Fallback — infer from failure category
        cat_hints = {
            "null_violation": None,  # will scan below
            "range_violation": "amount",
            "uniqueness_violation": "email",
        }
        null_cols = [cat_hints.get(c) for c in failure_categories if cat_hints.get(c)]

    if not null_cols:
        null_cols = all_cols[:1]  # last resort

    # Build diff lines — only null columns, max 3 rows
    display_rows = min(3, len(sample_before))
    diff_lines: list[str] = []

    for i in range(display_rows):
        row = sample_before[i]
        pk_val = row.get(id_col) if id_col else i + 1
        id_prefix = f"row {pk_val}: "

        for col in null_cols:
            if row.get(col) is None:
                # Infer after value from fix SQL if possible
                after_val = "'Unknown'"  # default
                if sample_after and i < len(sample_after):
                    matched = sample_after[i].get(col)
                    if matched is not None:
                        after_val = _format_value(matched)
                diff_lines.append(
                    f"{id_prefix}*{col}*:  `NULL`  →  {after_val}"
                )

    if not diff_lines:
        return [{
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "📊 _No null values found in sandbox preview sample._",
            }],
        }]

    remaining = rows_affected - display_rows
    diff_text = "\n".join(diff_lines)
    if remaining > 0:
        diff_text += f"\n_...and {remaining} more row(s) not shown_"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📊 Sandbox Preview*  _(showing {display_rows} of {rows_affected} rows)_",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": diff_text,
            },
        },
    ]
    
# ── Card builders ─────────────────────────────────────────────────────────────

def detecting_card(
    event_id: str,
    table_fqn: str,
    table_name: str,
    severity: str,
) -> list[dict]:
    """
    Phase 1 card — posted the moment the event is received.
    Pipeline is still running. Self-updates to proposal_card when ready.
    """
    SEVERITY_EMOJI = {
        "critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢",
    }
    emoji    = SEVERITY_EMOJI.get(severity.lower(), "⚪")
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
                    "diagnosing failure and running sandbox preview...\n"
                    "_This card updates automatically when the proposal is ready._"
                ),
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"AegisDB · {_utc_now()}"}],
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
    # V1 additions — default to empty list so old call sites don't break
    sample_before: list[dict] | None = None,
    sample_after: list[dict] | None = None,
) -> list[dict]:
    """
    Full proposal card.
    V1: Adds before/after data diff section.
    V2: Dynamic severity header based on confidence + failure type.
    """
    sample_before = sample_before or []
    sample_after  = sample_after  or []

    # V2: Dynamic header
    emoji, label = _header_style(failure_categories, confidence, sandbox_passed)

    # Format category names for display
    categories_str = ", ".join(
        c.replace("_", " ").title() for c in failure_categories
    ) or "Unknown"

    sandbox_status = "✅  Passed" if sandbox_passed else "❌  Failed"

    history_note = (
        f"📚  _{similar_fix_count} similar fix(es) in knowledge base — factored into diagnosis_"
        if similar_fix_count > 0
        else "📚  _No prior fixes for this table — first occurrence_"
    )

    # ── Build blocks ──────────────────────────────────────────────────────
    blocks: list[dict] = []

    # Header
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"{emoji}  {label} · {table_name}",
            "emoji": True,
        },
    })

    # Stats row: table, issue, rows, sandbox
    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*Table*\n`{table_name}`"},
            {"type": "mrkdwn", "text": f"*Issue*\n{categories_str}"},
            {
                "type": "mrkdwn",
                "text": f"*Rows Affected*\n{rows_affected}  ({rows_before} → {rows_after})",
            },
            {"type": "mrkdwn", "text": f"*Sandbox*\n{sandbox_status}"},
        ],
    })

    blocks.append({"type": "divider"})

    # Root cause
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Root Cause*\n{root_cause}",
        },
    })

    # Fix description
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Proposed Fix*\n{fix_description}",
        },
    })

    # V2: Confidence + sandbox on same row as fields
    blocks.append({
        "type": "section",
        "fields": [
            {
                "type": "mrkdwn",
                "text": f"*Confidence*\n{_confidence_bar(confidence)}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Sandbox Result*\n{sandbox_status}",
            },
        ],
    })

    # Fix SQL — collapsed in a code block
    # Keep SQL but make it secondary to the diff
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Fix SQL*\n```{fix_sql}```",
        },
    })

    blocks.append({"type": "divider"})

    # V1: Before/after data diff
    diff_blocks = _build_diff_section(
        sample_before=sample_before,
        sample_after=sample_after,
        failure_categories=failure_categories,
        rows_affected=rows_affected,
    )
    blocks.extend(diff_blocks)

    blocks.append({"type": "divider"})

    # Knowledge base history note
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": history_note}],
    })

    # Action buttons
    blocks.append({
        "type": "actions",
        "block_id": f"proposal_actions_{proposal_id}",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "✅  Approve Fix",
                    "emoji": True,
                },
                "style": "primary",
                "action_id": "approve_proposal",
                "value": proposal_id,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Approve this fix?"},
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"This will apply the fix to *{table_name}* "
                            f"affecting *{rows_affected} rows*.\n"
                            f"Sandbox: {sandbox_status}"
                        ),
                    },
                    "confirm": {"type": "plain_text", "text": "Yes, apply it"},
                    "deny":    {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌  Reject",
                    "emoji": True,
                },
                "style": "danger",
                "action_id": "reject_proposal",
                "value": proposal_id,
            },
        ],
    })

    # Footer
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"Proposal ID: `{proposal_id[:8]}...`  ·  "
                f"AegisDB · {_utc_now()}"
            ),
        }],
    })

    return blocks


def resolved_card(
    table_name: str,
    outcome: str,           # "completed" | "approved" | "rejected" | "failed"
    rows_affected: int,
    decided_by: str,
    reason: str = "",
    dry_run: bool = True,
    confidence: float = 0.0,
    sandbox_passed: bool = True,
    applied_at: str = "",
) -> list[dict]:
    """
    V3: Final card state rendered as a receipt with fields grid.
    Each outcome (completed, executing, rejected, failed) has a distinct
    visual treatment — not just different text in the same layout.
    """
    now      = applied_at or _utc_now()
    mode_str = "DRY RUN 🟡" if dry_run else "LIVE 🟢"
    conf_str = f"{int(confidence * 100)}%" if confidence > 0 else "N/A"
    sbox_str = "✅ Passed" if sandbox_passed else "❌ Failed"

    # ── completed (fix applied) ───────────────────────────────────────────
    if outcome == "completed":
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"✅  Fix Applied · {table_name}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Rows Fixed*\n{rows_affected}"},
                    {"type": "mrkdwn", "text": f"*Mode*\n{mode_str}"},
                    {"type": "mrkdwn", "text": f"*Decided by*\n{decided_by}"},
                    {"type": "mrkdwn", "text": f"*Applied at*\n{now}"},
                    {"type": "mrkdwn", "text": f"*Confidence*\n{conf_str}"},
                    {"type": "mrkdwn", "text": f"*Sandbox*\n{sbox_str}"},
                ],
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "Post-apply verification passed · Rollback SQL preserved",
                }],
            },
        ]

    # ── approved / executing ──────────────────────────────────────────────
    if outcome == "approved":
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"⏳  Executing Fix · {table_name}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Rows Queued*\n{rows_affected}"},
                    {"type": "mrkdwn", "text": f"*Mode*\n{mode_str}"},
                    {"type": "mrkdwn", "text": f"*Approved by*\n{decided_by}"},
                    {"type": "mrkdwn", "text": f"*Started at*\n{now}"},
                ],
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "Apply agent running — card will update when complete",
                }],
            },
        ]

    # ── rejected ──────────────────────────────────────────────────────────
    if outcome == "rejected":
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"❌  Proposal Rejected · {table_name}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Rejected by*\n{decided_by}"},
                    {"type": "mrkdwn", "text": f"*At*\n{now}"},
                ],
            },
        ]
        if reason:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Reason*\n> {reason}",
                },
            })
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    "✏️  Rejection reason stored in knowledge base · "
                    "Future proposals for this table will reflect this decision"
                ),
            }],
        })
        return blocks

    # ── failed ────────────────────────────────────────────────────────────
    if outcome == "failed":
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"💥  Fix Failed · {table_name}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Attempted by*\n{decided_by}"},
                    {"type": "mrkdwn", "text": f"*At*\n{now}"},
                    {"type": "mrkdwn", "text": f"*Mode*\n{mode_str}"},
                    {"type": "mrkdwn", "text": "*Status*\nRolled back"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "⚠️  The fix could not be applied — "
                        "transaction was rolled back automatically.\n"
                        "Run `/aegis audit` for full error details."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "No production data was modified",
                }],
            },
        ]

    # ── fallback (unknown outcome) ────────────────────────────────────────
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"ℹ️  *{table_name}* · {outcome}\n{reason or ''}",
            },
        },
    ]


def rejection_modal(proposal_id: str, table_name: str) -> dict:
    """
    Slack modal for capturing rejection reason + alternative suggestion.
    Opened when engineer clicks Reject.
    callback_id must match app.py @app.view("rejection_modal_submit").
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
                    "text": (
                        f"Rejecting fix proposal for *{table_name}*.\n"
                        "Your reason will be stored in the knowledge base "
                        "to improve future proposals for this table."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "rejection_reason_block",
                "label": {
                    "type": "plain_text",
                    "text": "Why are you rejecting this?",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "rejection_reason_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": (
                            "e.g. This column feeds billing — "
                            "NULL→Unknown breaks downstream aggregations"
                        ),
                    },
                    "multiline": True,
                },
            },
            {
                "type": "input",
                "block_id": "alternative_block",
                "label": {
                    "type": "plain_text",
                    "text": "What should happen instead? (optional)",
                },
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "alternative_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": (
                            "e.g. Fix the ETL source — "
                            "don't patch the data downstream"
                        ),
                    },
                    "multiline": True,
                },
            },
        ],
    }