import json
import logging
from groq import AsyncGroq

from src.core.config import settings
from src.core.models import (
    EnrichedFailureEvent,
    DetectorResult,
    DiagnosisResult,
    RepairProposal,
    FailureCategory,
    SimilarFix,
)
from src.db.vector_store import vector_store

logger = logging.getLogger(__name__)


def _build_system_prompt() -> str:
    return """You are AegisDB, an autonomous database repair agent.
Your job is to analyze data quality failures and propose safe, minimal SQL fixes.

Rules you MUST follow:
1. Only propose fixes that are reversible or have a rollback plan.
2. Never DROP columns or tables.
3. Prefer UPDATE/DELETE on bad rows over schema changes.
4. If you are not confident (< 0.70), say so clearly and recommend human review.
5. Always provide estimated rows affected.
6. Output ONLY valid JSON — no markdown, no explanation outside the JSON.

Your output must match this exact schema:
{
  "root_cause": "plain English explanation of why this failed",
  "confidence": 0.0 to 1.0,
  "failure_categories": ["null_violation", ...],
  "repair_proposal": {
    "fix_sql": "SQL to fix the problem — use {table} as placeholder",
    "fix_description": "what this SQL does in plain English",
    "affected_columns": ["col1", "col2"],
    "is_reversible": true or false,
    "rollback_sql": "SQL to undo the fix, or null",
    "estimated_rows_affected": integer or null
  },
  "reasoning": "step-by-step chain of thought"
}

If confidence < 0.70, set repair_proposal to null and explain in root_cause."""


def _build_user_prompt(
    event: EnrichedFailureEvent,
    detector: DetectorResult,
    similar_fixes: list[SimilarFix],
) -> str:
    tests_text = "\n".join(
        f"  - Column '{t.column_name}': {t.test_name} → {t.failure_reason}"
        for t in event.failed_tests
    )

    schema_text = "Not available"
    if event.table_context:
        ctx = event.table_context
        cols = "\n".join(
            f"  - {c.name} ({c.dataType}, nullable={c.nullable})"
            for c in ctx.columns
        )
        schema_text = (
            f"Table: {ctx.table_fqn}\n"
            f"Rows: {ctx.row_count or 'unknown'}\n"
            f"Columns:\n{cols}\n"
            f"Upstream: {ctx.upstream_tables or 'none'}\n"
            f"Downstream: {ctx.downstream_tables or 'none'}"
        )

    kb_text = "No similar fixes found in knowledge base (first occurrence)."
    if similar_fixes:
        kb_entries = "\n".join(
            f"  [{i+1}] similarity={f.similarity_score:.2f} | "
            f"category={f.failure_category} | "
            f"fix={f.fix_sql} | "
            f"was_successful={f.was_successful}"
            for i, f in enumerate(similar_fixes)
        )
        kb_text = f"Similar past fixes:\n{kb_entries}"

    return f"""## Data Quality Failure Report

**Table:** {event.table_fqn}
**Severity:** {detector.severity.value}
**Detector classification:** {[c.value for c in detector.failure_categories]}

## Failed Tests
{tests_text}

## Table Schema
{schema_text}

## Knowledge Base
{kb_text}

## Your Task
Analyze this failure and propose a safe SQL fix.
Replace the actual table name in your SQL with {{table}} as a placeholder.
The Repair agent will substitute the real table name before execution."""


class DiagnosisAgent:
    def __init__(self):
        self._llm = AsyncGroq(api_key=settings.groq_api_key)

    async def run(
        self,
        event: EnrichedFailureEvent,
        detector_result: DetectorResult,
    ) -> DiagnosisResult:

        logger.info(
            f"[Diagnosis] Starting for event={event.event_id} "
            f"table={event.table_fqn}"
        )

        if not detector_result.is_actionable:
            return DiagnosisResult(
                event_id=event.event_id,
                failure_categories=detector_result.failure_categories,
                root_cause=detector_result.detector_notes,
                confidence=0.0,
                is_repairable=False,
                escalation_reason=(
                    f"Detector marked as non-actionable: "
                    f"{detector_result.detector_notes}"
                ),
            )

        primary_category = detector_result.failure_categories[0]
        problem_desc = " ".join(
            f"{t.column_name}: {t.failure_reason}"
            for t in event.failed_tests if t.failure_reason
        )
        similar_fixes = vector_store.find_similar_fixes(
            table_fqn=event.table_fqn,
            failure_category=primary_category.value,
            problem_description=problem_desc,
            top_k=3,
        )

        try:
            raw_diagnosis = await self._call_llm(event, detector_result, similar_fixes)
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return DiagnosisResult(
                event_id=event.event_id,
                failure_categories=detector_result.failure_categories,
                root_cause=f"LLM unavailable: {e}",
                confidence=0.0,
                is_repairable=False,
                escalation_reason=f"LLM error: {e}",
            )

        return self._parse_llm_response(
            event.event_id,
            raw_diagnosis,
            detector_result,
            similar_fixes,
        )

    async def _call_llm(
        self,
        event: EnrichedFailureEvent,
        detector: DetectorResult,
        similar_fixes: list[SimilarFix],
    ) -> str:
        user_prompt = _build_user_prompt(event, detector, similar_fixes)
        system_prompt = _build_system_prompt()

        # Groq uses OpenAI-compatible chat completions format
        response = await self._llm.chat.completions.create(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,  # low temp for deterministic SQL generation
        )

        raw = response.choices[0].message.content.strip()
        logger.debug(f"LLM raw response:\n{raw}")
        return raw

    def _parse_llm_response(
        self,
        event_id: str,
        raw: str,
        detector: DetectorResult,
        similar_fixes: list[SimilarFix],
    ) -> DiagnosisResult:
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                # strip opening fence (```json or ```)
                clean = "\n".join(lines[1:])
            if clean.endswith("```"):
                clean = "\n".join(clean.split("\n")[:-1])
            clean = clean.strip()

            data = json.loads(clean)
            confidence = float(data.get("confidence", 0.0))
            is_repairable = confidence >= settings.confidence_threshold

            repair_proposal = None
            if is_repairable and data.get("repair_proposal"):
                rp = data["repair_proposal"]
                repair_proposal = RepairProposal(
                    fix_sql=rp["fix_sql"],
                    fix_description=rp.get("fix_description", ""),
                    affected_columns=rp.get("affected_columns", []),
                    is_reversible=rp.get("is_reversible", False),
                    rollback_sql=rp.get("rollback_sql"),
                    estimated_rows_affected=rp.get("estimated_rows_affected"),
                )

            logger.info(
                f"[Diagnosis] event={event_id} "
                f"confidence={confidence:.2f} "
                f"repairable={is_repairable}"
            )

            return DiagnosisResult(
                event_id=event_id,
                failure_categories=detector.failure_categories,
                root_cause=data.get("root_cause", "Unknown"),
                confidence=confidence,
                is_repairable=is_repairable,
                repair_proposal=repair_proposal,
                similar_fixes_used=similar_fixes,
                llm_reasoning=data.get("reasoning", ""),
                escalation_reason=(
                    None if is_repairable
                    else f"Low confidence ({confidence:.2f}): {data.get('root_cause', '')}"
                ),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Failed to parse LLM response: {e}\nRaw:\n{raw}")
            return DiagnosisResult(
                event_id=event_id,
                failure_categories=detector.failure_categories,
                root_cause=f"LLM response parse error: {e}",
                confidence=0.0,
                is_repairable=False,
                escalation_reason=f"Parse error: {e}",
                llm_reasoning=raw,
            )


diagnosis_agent = DiagnosisAgent()