import logging
import re
from src.core.models import (
    EnrichedFailureEvent,
    DetectorResult,
    FailureCategory,
    FailureSeverity,
    FailedTest,
)

logger = logging.getLogger(__name__)


# Keyword → category mapping. Order matters — more specific first.
_CATEGORY_RULES: list[tuple[list[str], FailureCategory]] = [
    (["not_null", "null", "not be null", "null values"], FailureCategory.NULL_VIOLATION),
    (["between", "range", "min", "max", "negative", "greater", "less"], FailureCategory.RANGE_VIOLATION),
    (["unique", "duplicate", "distinct"], FailureCategory.UNIQUENESS_VIOLATION),
    (["foreign key", "referential", "fk", "not exist"], FailureCategory.REFERENTIAL_INTEGRITY),
    (["regex", "format", "pattern", "like", "email", "phone"], FailureCategory.FORMAT_VIOLATION),
    (["column not found", "schema", "missing column", "type mismatch"], FailureCategory.SCHEMA_DRIFT),
]

# Columns that, when failing, escalate severity
_HIGH_PRIORITY_COLUMNS = {
    "customer_id", "order_id", "user_id", "id",
    "amount", "email", "status",
}


def _classify_test(test: FailedTest) -> FailureCategory:
    """Map a single failed test to a FailureCategory using keyword rules."""
    text = f"{test.test_name} {test.failure_reason or ''}".lower()
    for keywords, category in _CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return category
    return FailureCategory.UNKNOWN


def _compute_severity(
    categories: list[FailureCategory],
    affected_columns: list[str],
    base_severity: FailureSeverity,
) -> FailureSeverity:
    """Upgrade severity based on what failed."""
    # Critical: referential integrity or schema drift — downstream breakage
    if FailureCategory.REFERENTIAL_INTEGRITY in categories:
        return FailureSeverity.CRITICAL
    if FailureCategory.SCHEMA_DRIFT in categories:
        return FailureSeverity.CRITICAL

    # High: key business columns
    if any(col in _HIGH_PRIORITY_COLUMNS for col in affected_columns):
        return FailureSeverity.HIGH

    # Medium: multiple failure categories
    if len(set(categories)) >= 2:
        return FailureSeverity.MEDIUM

    return base_severity


def _is_actionable(categories: list[FailureCategory]) -> bool:
    """
    UNKNOWN + SCHEMA_DRIFT → not safe to auto-repair.
    Everything else → attempt automated fix.
    """
    if FailureCategory.SCHEMA_DRIFT in categories:
        return False
    if all(c == FailureCategory.UNKNOWN for c in categories):
        return False
    return True


def run_detector(event: EnrichedFailureEvent) -> DetectorResult:
    """
    Pure function — no I/O. Fast classification before the LLM call.
    """
    if not event.failed_tests:
        return DetectorResult(
            event_id=event.event_id,
            failure_categories=[FailureCategory.UNKNOWN],
            affected_columns=[],
            severity=FailureSeverity.LOW,
            is_actionable=False,
            detector_notes="No failed tests found in event",
        )

    categories = [_classify_test(t) for t in event.failed_tests]
    affected_columns = [
        t.column_name for t in event.failed_tests if t.column_name
    ]

    severity = _compute_severity(categories, affected_columns, event.severity)
    actionable = _is_actionable(categories)

    notes = (
        f"Classified {len(event.failed_tests)} failed test(s) into "
        f"{[c.value for c in set(categories)]}. "
        f"Affected columns: {affected_columns}."
    )

    logger.info(
        f"[Detector] event={event.event_id} "
        f"categories={[c.value for c in set(categories)]} "
        f"severity={severity.value} actionable={actionable}"
    )

    return DetectorResult(
        event_id=event.event_id,
        failure_categories=list(set(categories)),
        affected_columns=affected_columns,
        severity=severity,
        is_actionable=actionable,
        detector_notes=notes,
    )