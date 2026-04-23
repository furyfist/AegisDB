"""
Phase 2 — In-thread Q&A engine.

When an engineer replies in a proposal thread, this module:
  1. Assembles full context (proposal, ChromaDB fixes, rejections, profiling trend)
  2. Calls Groq with a focused system prompt
  3. Returns a plain string answer to post in the thread

Kept deliberately simple — one function, one Groq call, no streaming
(Slack doesn't support token streaming in chat.postMessage).
Streaming would require a websocket layer; not worth it for a hackathon.
"""

import asyncio
import json
import logging
from functools import partial

import httpx
from groq import AsyncGroq

from slack_bot.config import slack_settings
from slack_bot.rejection_store import rejection_store

logger = logging.getLogger(__name__)

BASE_URL = f"{slack_settings.aegisdb_base_url}/api/v1"


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    return """You are AegisDB Assistant, an expert database reliability engineer embedded in Slack.

You help engineers understand data quality issues by:
- Explaining root causes in plain English
- Summarising fix history and rejection history for a table
- Assessing whether a proposed fix is safe to approve
- Answering questions about table health trends

Rules:
1. Be concise — Slack messages, not essays. 3-5 sentences max unless detail is explicitly asked.
2. Always ground your answer in the context provided — never invent facts.
3. If you don't know something from context, say so directly.
4. When asked "is it safe to approve?", give a clear YES/NO first, then reasoning.
5. Format numbers clearly — rows, percentages, confidence scores.
6. Never reproduce full SQL unless the engineer explicitly asks for it.
7. Output plain text with minimal markdown — Slack renders *bold* and `code` only."""


def _build_context_prompt(
    question: str,
    proposal: dict,
    similar_fixes: list[dict],
    rejections: list[dict],
    profiling_summary: str,
) -> str:
    """
    Assembles everything the LLM needs into a structured prompt.
    Mirrors the pattern from diagnosis.py _build_user_prompt().
    """
    # Proposal section
    categories = ", ".join(
        c.replace("_", " ") for c in proposal.get("failure_categories", [])
    )
    prop_section = (
        f"## Current Proposal\n"
        f"Table: {proposal.get('table_fqn', 'unknown')}\n"
        f"Issue type: {categories}\n"
        f"Root cause (LLM diagnosis): {proposal.get('root_cause', 'N/A')}\n"
        f"Fix description: {proposal.get('fix_description', 'N/A')}\n"
        f"Confidence: {int(proposal.get('confidence', 0) * 100)}%\n"
        f"Rows affected: {proposal.get('rows_affected', 0)} "
        f"({proposal.get('rows_before', 0)} → {proposal.get('rows_after', 0)})\n"
        f"Sandbox: {'PASSED' if proposal.get('sandbox_passed') else 'FAILED'}\n"
        f"LLM reasoning: {proposal.get('llm_reasoning', 'Not available')}"
    )

    # Attempt to parse llm_reasoning from diagnosis_json if not on proposal directly
    if not proposal.get("llm_reasoning"):
        try:
            diag = json.loads(proposal.get("diagnosis_json", "{}"))
            reasoning = diag.get("llm_reasoning", "")
            if reasoning:
                prop_section += f"\nDetailed LLM chain-of-thought: {reasoning}"
        except Exception:
            pass

    # Similar fixes section
    if similar_fixes:
        fix_lines = "\n".join(
            f"  [{i+1}] similarity={f.get('similarity_score', 0):.2f} | "
            f"was_successful={f.get('was_successful')} | "
            f"fix: {f.get('fix_sql', '')[:80]}"
            for i, f in enumerate(similar_fixes)
        )
        fixes_section = f"## Similar Past Fixes (from knowledge base)\n{fix_lines}"
    else:
        fixes_section = "## Similar Past Fixes\nNo similar fixes found — first occurrence."

    # Rejection history section
    if rejections:
        rej_lines = "\n".join(
            f"  [{i+1}] rejected by {r.get('decided_by', 'unknown')} "
            f"on {r.get('rejected_at', 'unknown')[:10]} | "
            f"reason: {r.get('rejection_reason', '')} | "
            f"alternative: {r.get('alternative', 'none')}"
            for i, r in enumerate(rejections)
        )
        rejections_section = f"## Rejection History for This Table\n{rej_lines}"
    else:
        rejections_section = (
            "## Rejection History\n"
            "No previous rejections for this table."
        )

    # Profiling trend section
    profiling_section = f"## Profiling Trend\n{profiling_summary}"

    return (
        f"{prop_section}\n\n"
        f"{fixes_section}\n\n"
        f"{rejections_section}\n\n"
        f"{profiling_section}\n\n"
        f"## Engineer's Question\n{question}"
    )


# ── Context fetchers ──────────────────────────────────────────────────────────

async def _fetch_proposal(proposal_id: str) -> dict:
    url = f"{BASE_URL}/proposals/{proposal_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"[QA] fetch_proposal failed: {e}")
        return {}


async def _fetch_profiling_trend(table_name: str) -> str:
    """
    Pull last 3 profiling reports, extract anomaly rates for this table,
    compute direction of trend.
    Returns a plain English summary string.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BASE_URL}/profiles?limit=3")
            resp.raise_for_status()
            reports_data = resp.json()

        reports = reports_data.get("reports", [])
        if not reports:
            return "No profiling history available."

        # Fetch anomaly detail for each report — filter to this table
        snapshots = []
        for report in reports:
            report_id = report.get("report_id")
            if not report_id:
                continue
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    detail_resp = await client.get(
                        f"{BASE_URL}/profile/{report_id}/anomalies",
                        params={"table_name": table_name},
                    )
                    detail_resp.raise_for_status()
                    anomalies_data = detail_resp.json()

                anomaly_count = anomalies_data.get("total_matched", 0)
                created_at = report.get("created_at", "")[:10]
                snapshots.append((created_at, anomaly_count))
            except Exception:
                continue

        if not snapshots:
            return f"No profiling data found for table `{table_name}`."

        # Build trend summary
        lines = [
            f"  Run {s[0]}: {s[1]} anomaly/anomalies"
            for s in snapshots
        ]
        trend_text = "\n".join(lines)

        # Simple trend direction
        if len(snapshots) >= 2:
            delta = snapshots[0][1] - snapshots[-1][1]
            if delta > 0:
                direction = f"⬆️  Worsening — anomaly count up {delta} since earliest run"
            elif delta < 0:
                direction = f"⬇️  Improving — anomaly count down {abs(delta)} since earliest run"
            else:
                direction = "➡️  Stable — anomaly count unchanged across runs"
        else:
            direction = "Only one profiling run available — no trend data."

        return f"{trend_text}\n{direction}"

    except Exception as e:
        logger.error(f"[QA] profiling trend fetch failed: {e}")
        return "Profiling trend unavailable."


def _fetch_similar_fixes_sync(proposal: dict) -> list[dict]:
    """
    Pull similar fixes from existing aegisdb_fixes collection.
    Runs synchronously (ChromaDB is not async) — called via run_in_executor.
    """
    from src.db.vector_store import vector_store

    table_fqn   = proposal.get("table_fqn", "")
    categories  = proposal.get("failure_categories", [])
    category    = categories[0] if categories else "unknown"
    description = proposal.get("root_cause", "")

    try:
        fixes = vector_store.find_similar_fixes(
            table_fqn=table_fqn,
            failure_category=category,
            problem_description=description,
            top_k=3,
        )
        return [f.model_dump() for f in fixes]
    except Exception as e:
        logger.error(f"[QA] similar_fixes fetch failed: {e}")
        return []


def _fetch_rejections_sync(table_fqn: str, failure_category: str) -> list[dict]:
    """
    Pull rejection history from aegisdb_rejections collection.
    Synchronous — called via run_in_executor.
    """
    try:
        return rejection_store.find_rejections_for_table(
            table_fqn=table_fqn,
            failure_category=failure_category,
            top_k=3,
        )
    except Exception as e:
        logger.error(f"[QA] rejections fetch failed: {e}")
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

async def answer_question(
    question: str,
    proposal_id: str,
    groq_api_key: str,
) -> str:
    """
    Full pipeline: fetch context → assemble prompt → call Groq → return answer.
    Called by app.py message handler.

    Returns plain string ready to post in Slack thread.
    """
    logger.info(f"[QA] Answering question for proposal={proposal_id}: '{question[:80]}'")

    # ── 1. Fetch proposal (async) ─────────────────────────────────────────
    proposal = await _fetch_proposal(proposal_id)
    if not proposal:
        return "❌ I couldn't retrieve the proposal details. Please check the AegisDB dashboard."

    table_fqn   = proposal.get("table_fqn", "")
    table_name  = proposal.get("table_name", "unknown")
    categories  = proposal.get("failure_categories", [])
    category    = categories[0] if categories else ""

    # ── 2. Fetch profiling trend (async) ──────────────────────────────────
    profiling_summary = await _fetch_profiling_trend(table_name)

    # ── 3. Fetch ChromaDB data (sync → executor) ──────────────────────────
    loop = asyncio.get_event_loop()

    similar_fixes = await loop.run_in_executor(
        None,
        partial(_fetch_similar_fixes_sync, proposal),
    )
    rejections = await loop.run_in_executor(
        None,
        partial(_fetch_rejections_sync, table_fqn, category),
    )

    # ── 4. Build prompt ───────────────────────────────────────────────────
    context_prompt = _build_context_prompt(
        question=question,
        proposal=proposal,
        similar_fixes=similar_fixes,
        rejections=rejections,
        profiling_summary=profiling_summary,
    )

    # ── 5. Call Groq ──────────────────────────────────────────────────────
    try:
        llm = AsyncGroq(api_key=groq_api_key)
        response = await llm.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=512,        # short — Slack messages, not essays
            temperature=0.2,       # slightly higher than diagnosis for natural language
            messages=[
                {"role": "system", "content": _build_system_prompt()},
                {"role": "user",   "content": context_prompt},
            ],
        )
        answer = response.choices[0].message.content.strip()
        logger.info(f"[QA] Groq answered in {len(answer)} chars")
        return answer

    except Exception as e:
        logger.error(f"[QA] Groq call failed: {e}")
        return (
            f"❌ LLM call failed: `{e}`\n"
            f"The proposal context is available in the card above."
        )


async def answer_global_question(question: str, groq_api_key: str) -> str:
    """
    /aegis ask — no specific proposal context.
    Pulls recent audit entries + profiling summary as context.
    """
    logger.info(f"[QA] Global question: '{question[:80]}'")

    context_parts = []

    # Recent audit entries for context
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BASE_URL}/audit?limit=10")
            resp.raise_for_status()
            audit_data = resp.json()

        entries = audit_data.get("entries", [])
        if entries:
            audit_lines = "\n".join(
                f"  {e.get('applied_at', '')[:16]} | "
                f"{e.get('action', '')} | "
                f"{e.get('table_name', '')} | "
                f"{e.get('rows_affected', 0)} rows"
                for e in entries[:5]
            )
            context_parts.append(f"## Recent Fix History\n{audit_lines}")
    except Exception as e:
        logger.error(f"[QA] audit fetch for global Q failed: {e}")

    # Pending proposals
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BASE_URL}/proposals?status=pending_approval&limit=5"
            )
            resp.raise_for_status()
            prop_data = resp.json()

        pending = prop_data.get("proposals", [])
        if pending:
            prop_lines = "\n".join(
                f"  {p.get('table_name', '')} | "
                f"{', '.join(p.get('failure_categories', []))} | "
                f"confidence={int(p.get('confidence', 0) * 100)}%"
                for p in pending
            )
            context_parts.append(f"## Pending Proposals\n{prop_lines}")
    except Exception as e:
        logger.error(f"[QA] proposals fetch for global Q failed: {e}")

    context_str = (
        "\n\n".join(context_parts)
        if context_parts
        else "No recent system data available."
    )

    global_prompt = (
        f"{context_str}\n\n"
        f"## Engineer's Question\n{question}"
    )

    try:
        llm = AsyncGroq(api_key=groq_api_key)
        response = await llm.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=512,
            temperature=0.2,
            messages=[
                {"role": "system", "content": _build_system_prompt()},
                {"role": "user",   "content": global_prompt},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[QA] Global Groq call failed: {e}")
        return f"❌ LLM call failed: `{e}`"