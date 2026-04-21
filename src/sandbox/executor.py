import asyncio
import logging
from datetime import datetime
from typing import Callable

import psycopg2
from testcontainers.postgres import PostgresContainer

from src.core.config import settings
from src.core.models import (
    DiagnosisResult,
    EnrichedFailureEvent,
    SandboxResult,
    DataDiff,
    TestAssertionResult,
)
from src.db import target_db
from src.sandbox.validator import run_assertions

logger = logging.getLogger(__name__)

_POSTGRES_IMAGE = "postgres:16-alpine"


def _sanitize_rows(rows: list[dict]) -> list[dict]:
    """Convert non-JSON-serializable types. Handles BYTEA, dates, decimals."""
    import datetime, decimal
    clean = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            if isinstance(v, (memoryview, bytes)):
                new_row[k] = None
            elif isinstance(v, (datetime.date, datetime.datetime)):
                new_row[k] = v.isoformat()
            elif isinstance(v, decimal.Decimal):
                new_row[k] = float(v)
            else:
                new_row[k] = v
        clean.append(new_row)
    return clean


def _to_psycopg2_url(conn_url: str) -> str:
    """
    testcontainers returns postgresql+psycopg2://...
    psycopg2.connect() only accepts postgresql://...
    Strip the driver part.
    """
    return conn_url.replace("postgresql+psycopg2://", "postgresql://")


def _get_table_name(table_fqn: str) -> tuple[str, str]:
    """
    Split FQN into (schema, table_name).
    Format: service.database.schema.table
    """
    parts = table_fqn.split(".")
    schema = parts[2] if len(parts) >= 4 else "public"
    table = parts[3] if len(parts) >= 4 else parts[-1]
    return schema, table


def _seed_sandbox_sync(conn_url, ddl, col_names, rows, table_name):
    import time
    clean_url = _to_psycopg2_url(conn_url)
    
    # Retry connection — Postgres may not be ready immediately on Windows
    last_error = None
    for attempt in range(10):
        try:
            conn = psycopg2.connect(clean_url)
            break
        except psycopg2.OperationalError as e:
            last_error = e
            logger.info(f"[Sandbox] Waiting for Postgres to be ready (attempt {attempt+1}/10)...")
            time.sleep(2)
    else:
        raise last_error

    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(ddl)
        if rows:
            placeholders = ",".join(["%s"] * len(col_names))
            col_list = ",".join(f'"{c}"' for c in col_names)
            insert_sql = (
                f'INSERT INTO "{table_name}" ({col_list}) '
                f"VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            )
            cur.executemany(insert_sql, rows)
        logger.info(f"[Sandbox] Seeded {len(rows)} rows into sandbox.{table_name}")
    finally:
        cur.close()
        conn.close()


def _run_fix_sync(conn_url: str, fix_sql: str) -> int:
    """
    Sync. Executes the fix SQL, returns rowcount.
    """
    conn = psycopg2.connect(_to_psycopg2_url(conn_url))
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute(fix_sql)
        rowcount = cur.rowcount
        conn.commit()
        logger.info(f"[Sandbox] Fix executed, rowcount={rowcount}")
        return rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _get_row_count_sync(conn_url: str, table_name: str) -> int:
    conn = psycopg2.connect(_to_psycopg2_url(conn_url))
    cur = conn.cursor()
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        return cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()


def _get_sample_sync(conn_url: str, table_name: str, limit: int = 10) -> list[dict]:
    conn = psycopg2.connect(_to_psycopg2_url(conn_url))
    cur = conn.cursor()
    try:
        cur.execute(f'SELECT * FROM "{table_name}" LIMIT {limit}')
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return _sanitize_rows(rows)
    finally:
        cur.close()
        conn.close()


async def _run_async_assertions(
    conn_url: str,
    table_name: str,
    event: EnrichedFailureEvent,
) -> list[TestAssertionResult]:
    """
    Run assertions using asyncpg via SQLAlchemy async engine.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    # Convert psycopg2 URL → asyncpg URL
    async_url = conn_url.replace(
        "postgresql://", "postgresql+asyncpg://"
    ).replace("postgresql+psycopg2://", "postgresql+asyncpg://")

    engine = create_async_engine(async_url, echo=False)
    results = []
    try:
        async with engine.connect() as conn:
            results = await run_assertions(conn, table_name, event.failed_tests)
    finally:
        await engine.dispose()
    return results


def _execute_sandbox_sync(
    ddl: str,
    col_names: list[str],
    rows: list[tuple],
    fix_sql: str,
    table_name: str,
    row_count_before: int,
    failed_tests_json: str,   # serialized so we can pass across thread boundary
) -> dict:
    """
    Entire sandbox lifecycle in one sync function.
    Runs in asyncio.to_thread — no event loop inside.
    Returns a plain dict (not Pydantic — can't pickle across threads safely).
    """
    import json

    with PostgresContainer(
        image=_POSTGRES_IMAGE,
        username="sandbox_user",
        password="sandbox_pass",
        dbname="sandbox_db",
        driver="psycopg2",
    ) as postgres:
        conn_url = postgres.get_connection_url()
        logger.info(f"[Sandbox] Container up: {conn_url[:50]}...")

        # 1. Seed schema + data
        _seed_sandbox_sync(conn_url, ddl, col_names, rows, table_name)

        # 2. Snapshot before
        rows_before = _get_row_count_sync(conn_url, table_name)
        sample_before = _get_sample_sync(conn_url, table_name, limit=5)

        # 3. Apply fix
        try:
            rowcount = _run_fix_sync(conn_url, fix_sql)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "rows_before": rows_before,
                "rows_after": rows_before,
                "rowcount": 0,
                "sample_before": sample_before,
                "sample_after": [],
                "conn_url": conn_url,
            }

        # 4. Snapshot after
        rows_after = _get_row_count_sync(conn_url, table_name)
        sample_after = _get_sample_sync(conn_url, table_name, limit=5)

        # Return data needed for async assertion step
        return {
            "success": True,
            "error": None,
            "rows_before": rows_before,
            "rows_after": rows_after,
            "rowcount": rowcount,
            "sample_before": sample_before,
            "sample_after": sample_after,
            # Pass connection URL back — assertions need to run async
            # Container is still alive at this point if we structure carefully
        }

        # NOTE: Container tears down when `with` block exits here.
        # Assertions must happen before the container dies.
        # This is why assertions are done inside the sync block below.


async def run_sandbox(
    event: EnrichedFailureEvent,
    diagnosis: DiagnosisResult,
    attempt: int = 1,
) -> SandboxResult:
    """
    Main entry point. Async interface over the sync sandbox execution.

    Flow:
    1. Pull schema + data from production (async)
    2. Run sandbox (sync, in thread)
    3. Return SandboxResult
    """
    schema, table_name = _get_table_name(event.table_fqn)
    proposal = diagnosis.repair_proposal

    if not proposal:
        return SandboxResult(
            event_id=event.event_id,
            fix_sql_executed="",
            table_name=table_name,
            sandbox_passed=False,
            attempt=attempt,
            error="No repair proposal in diagnosis result",
        )

    # Substitute {table} placeholder with real table name
    fix_sql = proposal.fix_sql.replace("{table}", f'"{table_name}"')

    logger.info(
        f"[Sandbox] Starting attempt {attempt}/{settings.sandbox_max_retries} "
        f"for event={event.event_id} table={table_name}"
    )
    logger.info(f"[Sandbox] Fix SQL: {fix_sql}")

    # Step 1: Pull production schema + sample data (async)
    try:
        ddl = await target_db.get_table_ddl(table_name, schema)
        col_names, rows = await target_db.get_sample_data(
            table_name, schema, limit=settings.sandbox_sample_rows
        )
        row_count_before = await target_db.get_row_count(table_name, schema)
        logger.info(
            f"[Sandbox] Fetched schema + {len(rows)} rows from production"
        )
    except Exception as e:
        logger.error(f"[Sandbox] Failed to fetch production data: {e}")
        return SandboxResult(
            event_id=event.event_id,
            fix_sql_executed=fix_sql,
            table_name=table_name,
            sandbox_passed=False,
            attempt=attempt,
            error=f"Production data fetch failed: {e}",
        )

    # Step 2: Run entire sandbox lifecycle in a thread
    # (testcontainers is blocking — must not run in event loop)
    try:
        result_dict = await asyncio.wait_for(
            asyncio.to_thread(
                _run_sandbox_with_assertions_sync,
                ddl=ddl,
                col_names=col_names,
                rows=rows,
                fix_sql=fix_sql,
                table_name=table_name,
                failed_tests=event.failed_tests,
            ),
            timeout=settings.sandbox_timeout_seconds,
        )
    except asyncio.TimeoutError:
        return SandboxResult(
            event_id=event.event_id,
            fix_sql_executed=fix_sql,
            table_name=table_name,
            sandbox_passed=False,
            attempt=attempt,
            error=f"Sandbox timed out after {settings.sandbox_timeout_seconds}s",
        )
    except Exception as e:
        logger.error(f"[Sandbox] Thread execution failed: {e}", exc_info=True)
        return SandboxResult(
            event_id=event.event_id,
            fix_sql_executed=fix_sql,
            table_name=table_name,
            sandbox_passed=False,
            attempt=attempt,
            error=str(e),
        )

    # Step 3: Build SandboxResult from thread output
    assertions = [
        TestAssertionResult(**a) for a in result_dict.get("assertions", [])
    ]
    all_passed = all(a.passed for a in assertions)

    data_diff = DataDiff(
        rows_before=result_dict.get("rows_before", row_count_before),
        rows_after=result_dict.get("rows_after", 0),
        rows_deleted=max(
            0,
            result_dict.get("rows_before", 0) - result_dict.get("rows_after", 0),
        ),
        rows_updated=result_dict.get("rowcount", 0),
        sample_before=result_dict.get("sample_before", []),
        sample_after=result_dict.get("sample_after", []),
    )

    sandbox_passed = result_dict.get("fix_success", False) and all_passed

    logger.info(
        f"[Sandbox] Completed attempt={attempt} "
        f"passed={sandbox_passed} "
        f"assertions={[a.passed for a in assertions]}"
    )

    return SandboxResult(
        event_id=event.event_id,
        fix_sql_executed=fix_sql,
        table_name=table_name,
        sandbox_passed=sandbox_passed,
        test_assertions=assertions,
        data_diff=data_diff,
        attempt=attempt,
        error=result_dict.get("error"),
        approved_for_production=sandbox_passed,
    )


def _run_sandbox_with_assertions_sync(
    ddl: str,
    col_names: list[str],
    rows: list[tuple],
    fix_sql: str,
    table_name: str,
    failed_tests,
) -> dict:
    """
    Full sandbox lifecycle including assertions — all sync, runs in thread.
    Keeps the container alive through assertion phase.
    """
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from src.sandbox.validator import run_assertions

    with PostgresContainer(
        image=_POSTGRES_IMAGE,
        username="sandbox_user",
        password="sandbox_pass",
        dbname="sandbox_db",
        driver=None,
    ) as postgres:
        conn_url = postgres.get_connection_url()
        logger.info(f"[Sandbox] Container ready")

        # Seed
        _seed_sandbox_sync(conn_url, ddl, col_names, rows, table_name)

        rows_before = _get_row_count_sync(conn_url, table_name)
        sample_before = _get_sample_sync(conn_url, table_name, limit=5)

        # Apply fix
        fix_success = True
        rowcount = 0
        error = None
        try:
            rowcount = _run_fix_sync(conn_url, fix_sql)
        except Exception as e:
            fix_success = False
            error = str(e)
            logger.error(f"[Sandbox] Fix SQL failed: {e}")

        rows_after = _get_row_count_sync(conn_url, table_name)
        sample_after = _get_sample_sync(conn_url, table_name, limit=5)

        # Run assertions in a new event loop inside the thread
        assertion_dicts = []
        if fix_success:
            async_url = conn_url.replace(
                "postgresql+psycopg2://", "postgresql+asyncpg://"
            ).replace(
                "postgresql://", "postgresql+asyncpg://"
            )
            engine = create_async_engine(async_url, echo=False)

            async def _assert():
                async with engine.connect() as conn:
                    results = await run_assertions(conn, table_name, failed_tests)
                    return [r.model_dump() for r in results]

            try:
                loop = asyncio.new_event_loop()
                assertion_dicts = loop.run_until_complete(_assert())
                loop.run_until_complete(engine.dispose())
                loop.close()
            except Exception as e:
                logger.error(f"[Sandbox] Assertions failed: {e}")
                error = str(e)

        return {
            "fix_success": fix_success,
            "error": error,
            "rows_before": rows_before,
            "rows_after": rows_after,
            "rowcount": rowcount,
            "sample_before": sample_before,
            "sample_after": sample_after,
            "assertions": assertion_dicts,
        }
        # Container tears down cleanly here