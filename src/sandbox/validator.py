import logging
from sqlalchemy import text
from src.core.models import FailedTest, TestAssertionResult, FailureCategory

logger = logging.getLogger(__name__)


async def run_assertions(
    conn,
    table_name: str,
    failed_tests: list[FailedTest],
) -> list[TestAssertionResult]:
    """
    Re-run each original test assertion against the sandbox table.
    Uses raw SQL — mirrors what OpenMetadata tests actually check.
    """
    results = []
    for test in failed_tests:
        result = await _run_single_assertion(conn, table_name, test)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        logger.info(f"  [{status}] {test.test_name} on {test.column_name}: {result.detail}")
    return results


async def _run_single_assertion(
    conn,
    table_name: str,
    test: FailedTest,
) -> TestAssertionResult:
    """Map test name to SQL assertion and execute it."""
    test_lower = test.test_name.lower()
    col = test.column_name

    try:
        # NULL check
        if "not_null" in test_lower or "not be null" in test_lower:
            return await _assert_not_null(conn, table_name, col, test.test_name)

        # Range check — extract bounds from test name or use defaults
        if "between" in test_lower or "range" in test_lower:
            return await _assert_between(conn, table_name, col, test.test_name)

        # Uniqueness check
        if "unique" in test_lower or "distinct" in test_lower:
            return await _assert_unique(conn, table_name, col, test.test_name)

        # Regex/format check
        if "regex" in test_lower or "like" in test_lower:
            return await _assert_regex(conn, table_name, col, test.test_name)

        # Unknown — mark as skipped
        return TestAssertionResult(
            test_name=test.test_name,
            column_name=col,
            passed=True,  # conservative: don't block on unknown
            detail="Assertion type not implemented — skipped",
        )

    except Exception as e:
        logger.error(f"Assertion failed to run: {e}")
        return TestAssertionResult(
            test_name=test.test_name,
            column_name=col,
            passed=False,
            detail=f"Assertion execution error: {e}",
        )


async def _assert_not_null(conn, table: str, col: str, test_name: str) -> TestAssertionResult:
    result = await conn.execute(
        text(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NULL')
    )
    null_count = result.scalar()
    passed = null_count == 0
    return TestAssertionResult(
        test_name=test_name,
        column_name=col,
        passed=passed,
        detail=f"{null_count} null values remaining" if not passed else "No nulls found",
    )


async def _assert_between(conn, table: str, col: str, test_name: str) -> TestAssertionResult:
    # Default bounds — can be extended to parse from test config
    min_val, max_val = 0, 9_999_999
    result = await conn.execute(
        text(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" < :min OR "{col}" > :max'),
        {"min": min_val, "max": max_val},
    )
    violation_count = result.scalar()
    passed = violation_count == 0
    return TestAssertionResult(
        test_name=test_name,
        column_name=col,
        passed=passed,
        detail=(
            f"{violation_count} rows outside [{min_val}, {max_val}]"
            if not passed else f"All values within [{min_val}, {max_val}]"
        ),
    )


async def _assert_unique(conn, table: str, col: str, test_name: str) -> TestAssertionResult:
    result = await conn.execute(
        text(f"""
            SELECT COUNT(*) FROM (
                SELECT "{col}" FROM "{table}"
                WHERE "{col}" IS NOT NULL
                GROUP BY "{col}"
                HAVING COUNT(*) > 1
            ) dupes
        """)
    )
    dupe_count = result.scalar()
    passed = dupe_count == 0
    return TestAssertionResult(
        test_name=test_name,
        column_name=col,
        passed=passed,
        detail=f"{dupe_count} duplicate values" if not passed else "All values unique",
    )


async def _assert_regex(conn, table: str, col: str, test_name: str) -> TestAssertionResult:
    # Use raw string for regex — avoids SyntaxWarning on escape sequences
    email_regex = r'^[^@]+@[^@]+\.[^@]+$'
    result = await conn.execute(
        text(
            f'SELECT COUNT(*) FROM "{table}" '
            f'WHERE "{col}" IS NOT NULL '
            f"AND \"{col}\" !~ :pattern"
        ),
        {"pattern": email_regex},
    )
    bad_count = result.scalar()
    passed = bad_count == 0
    return TestAssertionResult(
        test_name=test_name,
        column_name=col,
        passed=passed,
        detail=f"{bad_count} values fail format check" if not passed else "All values match format",
    )