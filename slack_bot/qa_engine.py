"""
Phase 2 — In-thread Q&A and global ask engine.

answer_question()       — for thread replies on a specific proposal
answer_global_question() — for /aegis ask (no proposal context)
answer_why_table()      — for /aegis why (rejection memory synthesis)
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

def build_system_prompt() -> str:
    """Public so app.py can import without duplication."""
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
4. When asked "is it safe to approve?", give a clear YES or NO first, then reasoning.
5. Format numbers clearly — rows, percentages, confidence scores.
6. Never reproduce full SQL unless the engineer explicitly asks for it.
7. Output plain text with minimal markdown — Slack renders *bold* and `code` only."""


# ── Groq caller (shared) ──────────────────────────────────────────────────────

async def _call_groq(prompt: str, max_tokens: int = 512) -> str:
    """Single Groq call. Raises on failure — callers catch and format."""
    llm = AsyncGroq(api_key=slack_settings.groq_api_key)
    response = await llm.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=max_tokens,
        temperature=0.2,
        messages=[
            {"role": "system", "content": build_system_prompt()},
            {"role": "user",   "content": prompt},
        ],
    )
    return response.choices[0].message.content.strip()


# ── Context builders ──────────────────────────────────────────────────────────

def _build_context_prompt(
    question: str,
    proposal: dict,
    similar_fixes: list[dict],
    rejections: list[dict],
    profiling_summary: str,
) -> str:
    categories = ", ".join(
        c.replace("_", " ") for c in proposal.get("failure_categories", [])
    )

    # Pull llm_reasoning from diagnosis_json if not top-level
    llm_reasoning = proposal.get("llm_reasoning", "")
    if not llm_reasoning:
        try:
            diag = json.loads(proposal.get("diagnosis_json", "{}"))
            llm_reasoning = diag.get("llm_reasoning", "Not available")
        except Exception:
            llm_reasoning = "Not available"

    prop_section = (
        f"## Current Proposal\n"
        f"Table: {proposal.get('table_fqn', 'unknown')}\n"
        f"Issue type: {categories}\n"
        f"Root cause (LLM): {proposal.get('root_cause', 'N/A')}\n"
        f"Fix: {proposal.get('fix_description', 'N/A')}\n"
        f"Confidence: {int(proposal.get('confidence', 0) * 100)}%\n"
        f"Rows: {proposal.get('rows_affected', 0)} "
        f"({proposal.get('rows_before', 0)} → {proposal.get('rows_after', 0)})\n"
        f"Sandbox: {'PASSED' if proposal.get('sandbox_passed') else 'FAILED'}\n"
        f"LLM chain-of-thought: {llm_reasoning}"
    )

    fixes_section = (
        "## Similar Past Fixes\n" + "\n".join(
            f"  [{i+1}] sim={f.get('similarity_score', 0):.2f} | "
            f"success={f.get('was_successful')} | "
            f"{f.get('fix_sql', '')[:80]}"
            for i, f in enumerate(similar_fixes)
        ) if similar_fixes
        else "## Similar Past Fixes\nNone — first occurrence."
    )

    rejections_section = (
        "## Rejection History\n" + "\n".join(
            f"  [{i+1}] {r.get('rejected_at', '')[:10]} by "
            f"{r.get('decided_by', '?')} | "
            f"reason: {r.get('rejection_reason', '')} | "
            f"alt: {r.get('alternative', 'none')}"
            for i, r in enumerate(rejections)
        ) if rejections
        else "## Rejection History\nNo prior rejections for this table."
    )

    return (
        f"{prop_section}\n\n"
        f"{fixes_section}\n\n"
        f"{rejections_section}\n\n"
        f"## Profiling Trend\n{profiling_summary}\n\n"
        f"## Engineer's Question\n{question}"
    )


# ── Data fetchers ─────────────────────────────────────────────────────────────

async def _fetch_proposal(proposal_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BASE_URL}/proposals/{proposal_id}")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"[QA] fetch_proposal failed: {e}")
        return {}


async def _fetch_profiling_trend(table_name: str) -> str:
    """Pull last 3 profiling reports, compute anomaly trend for this table."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BASE_URL}/profiles?limit=3")
            resp.raise_for_status()
            reports = resp.json().get("reports", [])

        if not reports:
            return "No profiling history available."

        snapshots = []
        for report in reports:
            rid = report.get("report_id")
            if not rid:
                continue
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    ar = await client.get(
                        f"{BASE_URL}/profile/{rid}/anomalies",
                        params={"table_name": table_name},
                    )
                    ar.raise_for_status()
                    count = ar.json().get("total_matched", 0)
                snapshots.append((report.get("created_at", "")[:10], count))
            except Exception:
                continue

        if not snapshots:
            return f"No profiling data for `{table_name}`."

        lines = "\n".join(f"  {d}: {c} anomaly/anomalies" for d, c in snapshots)
        if len(snapshots) >= 2:
            delta = snapshots[0][1] - snapshots[-1][1]
            trend = (
                f"⬆️  Worsening (+{delta})" if delta > 0
                else f"⬇️  Improving ({delta})" if delta < 0
                else "➡️  Stable"
            )
        else:
            trend = "Only one run — no trend."
        return f"{lines}\nTrend: {trend}"

    except Exception as e:
        logger.error(f"[QA] profiling trend failed: {e}")
        return "Profiling trend unavailable."


def _sync_similar_fixes(proposal: dict) -> list[dict]:
    """Synchronous — run via executor."""
    try:
        # Import here to avoid circular import at module level
        # Works because bot process runs from project root
        from src.db.vector_store import vector_store

        cats = proposal.get("failure_categories", [])
        fixes = vector_store.find_similar_fixes(
            table_fqn=proposal.get("table_fqn", ""),
            failure_category=cats[0] if cats else "unknown",
            problem_description=proposal.get("root_cause", ""),
            top_k=3,
        )
        return [f.model_dump() for f in fixes]
    except Exception as e:
        logger.error(f"[QA] similar_fixes sync failed: {e}")
        return []


def _sync_rejections(table_fqn: str, failure_category: str) -> list[dict]:
    """Synchronous — run via executor."""
    try:
        return rejection_store.find_rejections_for_table(
            table_fqn=table_fqn,
            failure_category=failure_category,
            top_k=3,
        )
    except Exception as e:
        logger.error(f"[QA] rejections sync failed: {e}")
        return []


# ── Public entry points ───────────────────────────────────────────────────────

async def answer_question(question: str, proposal_id: str) -> str:
    """
    Thread Q&A for a specific proposal.
    Assembles full context then calls Groq.
    """
    logger.info(f"[QA] Thread question proposal={proposal_id}: '{question[:80]}'")

    proposal = await _fetch_proposal(proposal_id)
    if not proposal:
        return "❌ Could not retrieve proposal details. Check the AegisDB dashboard."

    table_fqn  = proposal.get("table_fqn", "")
    table_name = proposal.get("table_name", "unknown")
    cats       = proposal.get("failure_categories", [])
    category   = cats[0] if cats else ""

    profiling_summary, similar_fixes, rejections = await asyncio.gather(
        _fetch_profiling_trend(table_name),
        asyncio.get_running_loop().run_in_executor(
            None, partial(_sync_similar_fixes, proposal)
        ),
        asyncio.get_running_loop().run_in_executor(
            None, partial(_sync_rejections, table_fqn, category)
        ),
    )

    prompt = _build_context_prompt(
        question=question,
        proposal=proposal,
        similar_fixes=similar_fixes,
        rejections=rejections,
        profiling_summary=profiling_summary,
    )

    try:
        return await _call_groq(prompt, max_tokens=512)
    except Exception as e:
        logger.error(f"[QA] Groq failed: {e}")
        return f"❌ LLM call failed: `{e}`"


async def answer_global_question(question: str) -> str:
    """/aegis ask — no specific proposal context."""
    logger.info(f"[QA] Global question: '{question[:80]}'")

    context_parts: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            audit_resp = await client.get(f"{BASE_URL}/audit?limit=10")
            audit_resp.raise_for_status()
            entries = audit_resp.json().get("entries", [])
        if entries:
            lines = "\n".join(
                f"  {e.get('applied_at', '')[:16]} | {e.get('action')} | "
                f"{e.get('table_name')} | {e.get('rows_affected', 0)} rows"
                for e in entries[:5]
            )
            context_parts.append(f"## Recent Fix History\n{lines}")
    except Exception as e:
        logger.error(f"[QA] audit fetch failed: {e}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            prop_resp = await client.get(
                f"{BASE_URL}/proposals?status=pending_approval&limit=5"
            )
            prop_resp.raise_for_status()
            pending = prop_resp.json().get("proposals", [])
        if pending:
            lines = "\n".join(
                f"  {p.get('table_name')} | "
                f"{', '.join(p.get('failure_categories', []))} | "
                f"confidence={int(p.get('confidence', 0) * 100)}%"
                for p in pending
            )
            context_parts.append(f"## Pending Proposals\n{lines}")
    except Exception as e:
        logger.error(f"[QA] proposals fetch failed: {e}")

    context = "\n\n".join(context_parts) if context_parts else "No system data available."
    prompt  = f"{context}\n\n## Engineer's Question\n{question}"

    try:
        return await _call_groq(prompt, max_tokens=512)
    except Exception as e:
        logger.error(f"[QA] Global Groq failed: {e}")
        return f"❌ LLM call failed: `{e}`"


async def answer_why_table(table_name: str) -> str:
    """/aegis why — rejection memory synthesis for a table."""
    logger.info(f"[QA] Why query for table='{table_name}'")

    rejections = await asyncio.get_running_loop().run_in_executor(
        None,
        partial(
            rejection_store.find_rejections_for_table,
            table_fqn=table_name,   # bare name — embedding finds the match
            failure_category="",
            top_k=5,
        ),
    )

    if not rejections:
        return (
            f"No rejection history found for `{table_name}`. "
            f"Either no proposals have been rejected for this table, "
            f"or it's the first time AegisDB has seen it."
        )

    rej_text = "\n".join(
        f"  [{i+1}] {r.get('rejected_at', '')[:10]} by {r.get('decided_by', '?')} | "
        f"categories: {r.get('failure_categories', '')} | "
        f"reason: {r.get('rejection_reason', '')} | "
        f"alt: {r.get('alternative', 'none')}"
        for i, r in enumerate(rejections)
    )

    prompt = (
        f"An engineer asked: 'Why has `{table_name}` been having recurring issues?'\n\n"
        f"Rejection history:\n{rej_text}\n\n"
        f"Synthesise:\n"
        f"1. What the underlying data quality problem seems to be\n"
        f"2. Why previous fixes were rejected\n"
        f"3. What the right long-term solution might be\n\n"
        f"4-6 sentences. Plain text."
    )

    try:
        return await _call_groq(prompt, max_tokens=400)
    except Exception as e:
        logger.error(f"[QA] Why Groq failed: {e}")
        return f"❌ LLM synthesis failed: `{e}`\n\nRaw history:\n{rej_text}"