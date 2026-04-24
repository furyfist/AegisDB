import asyncio
import json
import logging
from datetime import datetime

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from src.services.om_client import om_client
from src.core.config import settings
from src.core.models import (
    RepairDecision,
    ApplyAction,
    ApplyResult,
    AuditEntry,
    TestAssertionResult,
    FailedTest,
    TestStatus,
)
from src.db.audit_log import write_audit
from src.sandbox.validator import run_assertions

from src.db.reports_store import (
    write_report,
    extract_fix_type,
    extract_primary_column,
    get_recurrence_count,
)
from src.core.models import FixReport

logger = logging.getLogger(__name__)


def _build_target_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


class ApplyAgent:
    """
    Reads aegisdb:apply stream.

    DRY_RUN=true  → logs intent, no DB mutation
    DRY_RUN=false → executes fix in a transaction:
                    → post-apply verify passes → COMMIT → audit 'applied'
                    → post-apply verify fails  → ROLLBACK → audit 'rolled_back' → escalate
    """

    def __init__(self):
        self._redis: redis.Redis | None = None
        self._engine = None
        self._running = False
        self._consumer_name = "apply-agent-1"
        self._group = settings.redis_consumer_group

    async def connect(self):
        self._redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
        await self._redis.ping()

        # Create consumer groups idempotently
        for stream in [settings.redis_apply_stream]:
            try:
                await self._redis.xgroup_create(
                    stream, self._group, id="0", mkstream=True
                )
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise

        # Separate write engine — pool_size=1, only one apply at a time
        self._engine = create_async_engine(
            _build_target_url(),
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            echo=False,
        )

        mode = "DRY RUN" if settings.dry_run else "LIVE — WILL MUTATE PRODUCTION"
        logger.info(f"[ApplyAgent] Connected | mode={mode}")

    async def start(self):
        self._running = True
        logger.info(
            f"[ApplyAgent] Listening on '{settings.redis_apply_stream}'")

        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams={settings.redis_apply_stream: ">"},
                    count=1,
                    block=2000,
                )

                if not messages:
                    continue

                for _, stream_messages in messages:
                    for msg_id, fields in stream_messages:
                        await self._handle_message(msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ApplyAgent] Loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _handle_message(self, msg_id: str, fields: dict):
        event_id = fields.get("event_id", "unknown")
        logger.info(f"[ApplyAgent] Processing msg={msg_id} event={event_id}")

        try:
            decision = RepairDecision.model_validate_json(fields["data"])

            if not decision.approved:
                logger.warning(
                    f"[ApplyAgent] Decision not approved for event={event_id} "
                    f"reason={decision.rejection_reason} — routing to escalation"
                )
                await self._escalate(decision, reason=decision.rejection_reason or "Not approved")
                await self._ack(msg_id)
                return

            result = await self._execute(decision)
            await self._ack(msg_id)

            logger.info(
                f"[ApplyAgent] Done event={event_id} "
                f"action={result.action.value} "
                f"rows_affected={result.rows_affected}"
            )

        except Exception as e:
            logger.error(
                f"[ApplyAgent] Failed msg={msg_id}: {e}", exc_info=True
            )
            # No ACK → Redis redelivers

    async def _execute(self, decision: RepairDecision) -> ApplyResult:
        """Branch on dry_run flag."""
        if decision.dry_run or settings.dry_run:
            return await self._dry_run(decision)
        else:
            return await self._apply_live(decision)

    async def _dry_run(self, decision: RepairDecision) -> ApplyResult:
        """
        Dry run — no DB mutation.
        Logs exactly what WOULD have happened, writes audit row.
        """
        table_name = decision.table_name
        fix_sql = decision.fix_sql.replace("{table}", f'"{table_name}"')
        confidence = _extract_confidence(decision.diagnosis_result_json)
        categories = _extract_categories(decision.diagnosis_result_json)

        logger.info(
            f"[ApplyAgent][DRY RUN] Would execute on {table_name}:\n"
            f"  SQL: {fix_sql}\n"
            f"  Rollback: {decision.rollback_sql or 'N/A'}\n"
            f"  Sandbox rows_deleted: "
            f"{decision.sandbox_result.data_diff.rows_deleted if decision.sandbox_result.data_diff else 'N/A'}"
        )

        audit_entry = AuditEntry(
            event_id=decision.event_id,
            table_fqn=decision.table_fqn,
            table_name=decision.table_name,
            action=ApplyAction.DRY_RUN,
            fix_sql=fix_sql,
            rollback_sql=decision.rollback_sql,
            rows_affected=(
                decision.sandbox_result.data_diff.rows_deleted
                + decision.sandbox_result.data_diff.rows_updated
                if decision.sandbox_result.data_diff else 0
            ),
            dry_run=True,
            sandbox_passed=decision.sandbox_result.sandbox_passed,
            confidence=confidence,
            failure_categories=categories,
        )
        audit_id = await write_audit(audit_entry)

        # ── Auto-documentation: only on successful production apply 
        if action == ApplyAction.APPLIED:
            try:
                col_name    = extract_primary_column(fix_sql)
                anomaly_type = categories[0].lower() if categories else "unknown"
                recurrence  = await get_recurrence_count(
                    table_name, col_name, anomaly_type
                )
                assertions_passed = sum(
                    1 for a in post_assertions if a.passed
                )
                downstream = []
                try:
                    diag = json.loads(decision.diagnosis_result_json)
                    downstream = (
                        diag.get("table_context", {})
                           .get("downstream_tables", [])
                        if isinstance(diag.get("table_context"), dict)
                        else []
                    )
                except Exception:
                    downstream = []

                fix_report = FixReport(
                    event_id          = decision.event_id,
                    table_fqn         = decision.table_fqn,
                    table_name        = table_name,
                    column_name       = col_name,
                    anomaly_type      = anomaly_type,
                    anomaly_severity  = audit_entry.failure_categories[1]
                                       if len(audit_entry.failure_categories) > 1
                                       else "low",
                    fix_type          = extract_fix_type(fix_sql),
                    fix_sql           = fix_sql,
                    rows_affected     = rows_affected,
                    confidence        = confidence,
                    sandbox_passed    = decision.sandbox_result.sandbox_passed,
                    post_apply_passed = True,
                    assertions_passed = assertions_passed,
                    assertions_total  = len(post_assertions),
                    recurrence_count  = recurrence,
                    downstream_tables = downstream,
                    approver          = "human",
                )
                await write_report(fix_report)
                # ── OM annotation — best-effort, never blocks pipeline 
                await om_client.annotate_fix(
                    table_fqn        = decision.table_fqn,
                    column_name      = col_name,
                    anomaly_type     = anomaly_type,
                    rows_affected    = rows_affected,
                    confidence       = confidence,
                    recurrence_count = recurrence,
                    fix_type         = fix_report.fix_type.value,
                    event_id         = decision.event_id,
                )

            except Exception as e:
                # Never block the pipeline — report write is best-effort
                logger.error(
                    f"[ApplyAgent] write_report failed (non-critical): {e}"
                )

        return ApplyResult(
            event_id=decision.event_id,
            table_fqn=decision.table_fqn,
            action=ApplyAction.DRY_RUN,
            rows_affected=audit_entry.rows_affected,
            post_apply_passed=True,  # trivially true — nothing ran
            audit_id=audit_id,
        )

    async def _apply_live(self, decision: RepairDecision) -> ApplyResult:
        """
        Live execution path.

        Pattern: explicit transaction → execute fix → verify → commit or rollback.
        Uses SET LOCAL statement_timeout to cap runaway queries.
        """
        table_name = decision.table_name
        # Substitute {table} placeholder with the real quoted table name
        # This mirrors what the sandbox executor does internally
        fix_sql = decision.fix_sql.replace("{table}", f'"{table_name}"')
        confidence = _extract_confidence(decision.diagnosis_result_json)
        categories = _extract_categories(decision.diagnosis_result_json)
        failed_tests = self._extract_failed_tests(decision)

        logger.info(
            f"[ApplyAgent][LIVE] Executing fix on PRODUCTION table={table_name}"
        )

        rows_affected = 0
        post_assertions: list[TestAssertionResult] = []
        action = ApplyAction.FAILED
        error_msg: str | None = None

        try:
            async with self._engine.connect() as conn:
                # Explicit transaction — we control commit/rollback
                async with conn.begin() as txn:
                    # Safety: cap query runtime
                    await conn.execute(
                        text(
                            f"SET LOCAL statement_timeout = "
                            f"{settings.apply_statement_timeout_ms}"
                        )
                    )

                    # Execute the fix
                    result = await conn.execute(text(fix_sql))
                    rows_affected = result.rowcount
                    logger.info(
                        f"[ApplyAgent] Fix executed: rowcount={rows_affected}"
                    )

                    # Post-apply verification
                    if settings.post_apply_verify and failed_tests:
                        post_assertions = await run_assertions(
                            conn, table_name, failed_tests
                        )
                        all_passed = all(a.passed for a in post_assertions)

                        if all_passed:
                            # All assertions pass — commit
                            await txn.commit()
                            action = ApplyAction.APPLIED
                            logger.info(
                                f"[ApplyAgent] ✓ COMMITTED fix to production "
                                f"table={table_name} rows_affected={rows_affected}"
                            )
                        else:
                            # Assertions failed — rollback, don't touch production
                            await txn.rollback()
                            action = ApplyAction.ROLLED_BACK
                            failed_assertions = [
                                a for a in post_assertions if not a.passed
                            ]
                            error_msg = (
                                f"Post-apply assertions failed: "
                                f"{[f'{a.column_name}: {a.detail}' for a in failed_assertions]}"
                            )
                            logger.error(
                                f"[ApplyAgent] ✗ ROLLED BACK — assertions failed: "
                                f"{error_msg}"
                            )

                            # Attempt rollback SQL if provided
                            if decision.rollback_sql:
                                await self._execute_rollback(
                                    decision.rollback_sql, table_name
                                )

                            # Escalate so a human knows the fix didn't hold
                            await self._escalate(
                                decision,
                                reason=f"Post-apply verification failed: {error_msg}",
                            )
                    else:
                        # No verification configured — commit directly
                        await txn.commit()
                        action = ApplyAction.APPLIED
                        logger.info(
                            f"[ApplyAgent] ✓ COMMITTED (no post-verify) "
                            f"table={table_name}"
                        )

        except asyncio.TimeoutError:
            error_msg = (
                f"Statement timeout exceeded "
                f"({settings.apply_statement_timeout_ms}ms)"
            )
            action = ApplyAction.FAILED
            logger.error(f"[ApplyAgent] {error_msg}")
            await self._escalate(decision, reason=error_msg)

        except Exception as e:
            error_msg = str(e)
            action = ApplyAction.FAILED
            logger.error(f"[ApplyAgent] Execution error: {e}", exc_info=True)
            await self._escalate(decision, reason=f"Apply error: {e}")

        # Always write audit — win or lose
        audit_entry = AuditEntry(
            event_id=decision.event_id,
            table_fqn=decision.table_fqn,
            table_name=table_name,
            action=action,
            fix_sql=fix_sql,
            rollback_sql=decision.rollback_sql,
            rows_affected=rows_affected,
            dry_run=False,
            sandbox_passed=decision.sandbox_result.sandbox_passed,
            confidence=confidence,
            failure_categories=categories,
            post_apply_assertions=[a.model_dump() for a in post_assertions],
            error=error_msg,
        )
        audit_id = await write_audit(audit_entry)

        return ApplyResult(
            event_id=decision.event_id,
            table_fqn=decision.table_fqn,
            action=action,
            rows_affected=rows_affected,
            post_apply_passed=(action == ApplyAction.APPLIED),
            post_apply_assertions=post_assertions,
            audit_id=audit_id,
            error=error_msg,
        )

    async def _execute_rollback(self, rollback_sql: str, table_name: str):
        """
        Best-effort rollback SQL execution.
        Runs in its own connection+transaction — completely separate from the failed one.
        """
        try:
            async with self._engine.begin() as conn:
                await conn.execute(text(rollback_sql.replace("{table}", f'"{table_name}"')))
            logger.info(f"[ApplyAgent] Rollback SQL executed for {table_name}")
        except Exception as e:
            logger.error(
                f"[ApplyAgent] Rollback SQL also failed: {e} "
                f"— human intervention required"
            )

    async def _escalate(self, decision: RepairDecision, reason: str):
        """Push to escalation stream with full context."""
        try:
            await self._redis.xadd(
                settings.redis_escalation_stream,
                {
                    "event_id": decision.event_id,
                    "table_fqn": decision.table_fqn,
                    "reason": reason,
                    "stage": "apply",
                    "fix_sql": decision.fix_sql,
                    "escalated_at": datetime.now().isoformat(),
                },
                maxlen=500,
            )
        except Exception as e:
            logger.error(f"[ApplyAgent] Failed to publish escalation: {e}")

    async def _ack(self, msg_id: str):
        await self._redis.xack(
            settings.redis_apply_stream, self._group, msg_id
        )

    def stop(self):
        self._running = False

    async def close(self):
        self.stop()
        if self._redis:
            await self._redis.aclose()
        if self._engine:
            await self._engine.dispose()

    def _extract_failed_tests(self, decision) -> list:
        """
        Derives FailedTest objects dynamically from diagnosis_json.
        No hardcoded column map — works for any schema including Northwind.
        """
        import re
        import json

        failed_tests = []

        try:
            diag = json.loads(decision.diagnosis_json or "{}")
        except Exception:
            return failed_tests

        fix_sql = (diag.get("fix_sql") or "").lower()
        categories = diag.get("failure_categories") or []
        table_name = decision.table_name

        # Extract columns from fix_sql
        columns: list[str] = []
        columns += re.findall(r"where\s+(\w+)\s+is\s+(?:not\s+)?null", fix_sql)
        columns += re.findall(r"where\s+(\w+)\s*[<>=!]",               fix_sql)
        columns += re.findall(r"set\s+(\w+)\s*=",
                              fix_sql)
        columns += re.findall(r"and\s+(\w+)\s+is\s+(?:not\s+)?null",   fix_sql)
        columns += re.findall(r"and\s+(\w+)\s*[<>=!]",
                              fix_sql)

        _sql_keywords = {
            "null", "not", "and", "or", "where", "set",
            "from", "table", "select", "true", "false",
            "is", "in", "like", "between", "exists",
        }
        columns = list(dict.fromkeys(
            c for c in columns if c not in _sql_keywords
        ))

        _cat_to_assertion = {
            "null_violation":        "not_null",
            "range_violation":       "range",
            "uniqueness_violation":  "unique",
            "format_violation":      "format",
            "referential_integrity": "referential",
            "schema_drift":          "schema",
        }
        primary_category = categories[0].lower(
        ) if categories else "null_violation"
        assertion_type = _cat_to_assertion.get(primary_category, "not_null")

        for col in columns:
            failed_tests.append({
                "column_name":      col,
                "assertion_type":   assertion_type,
                "table_name":       table_name,
                "failure_category": primary_category,
            })

        return failed_tests


# ── Helpers to extract fields from serialized DiagnosisResult ────────────────

def _extract_confidence(diagnosis_json: str) -> float:
    try:
        return float(json.loads(diagnosis_json).get("confidence", 0.0))
    except Exception:
        return 0.0


def _extract_categories(diagnosis_json: str) -> list[str]:
    try:
        return json.loads(diagnosis_json).get("failure_categories", [])
    except Exception:
        return []


# Module-level singleton
apply_agent = ApplyAgent()