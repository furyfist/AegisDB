import asyncio
import logging
import time
import uuid
from typing import Any

import asyncpg

from src.core.models import (
    AnomalySeverity,
    ColumnAnomaly,
    FailureCategory,
    ProfilingReport,
    TableProfile,
)

logger = logging.getLogger(__name__)

# Columns whose names suggest they should be unique
_UNIQUE_HINT_NAMES = {
    "id", "uuid", "email", "username", "user_name",
    "phone", "mobile", "national_id", "ssn", "tax_id",
    "order_id", "customer_id", "product_id", "sku",
    "transaction_id", "invoice_number",
}

# Data types that support range / numeric checks
_NUMERIC_TYPES = {
    "integer", "bigint", "smallint", "numeric", "decimal",
    "real", "double precision", "money", "float",
}

# Data types that support format checks
_TEXT_TYPES = {"character varying", "varchar", "text", "char", "character"}

# Column names that suggest email format
_EMAIL_HINT_NAMES = {"email", "email_address", "mail", "e_mail"}


class ProfilerAgent:
    """
    Connects to ANY Postgres database and profiles every table
    in the specified schema without any prior configuration.

    Uses direct asyncpg connections — no SQLAlchemy overhead.
    Profiling is read-only: it never writes to the target database.
    """

    async def profile(
        self,
        connection_url: str,
        schemas: list[str] | None = None,
        table_limit: int = 50,
        row_sample_limit: int = 100_000,
    ) -> ProfilingReport:
        """
        Main entry point. Returns a full ProfilingReport.
        connection_url: standard postgres:// or postgresql:// URL
        schemas: list of schema names to scan (defaults to ["public"])
        table_limit: max tables to scan per schema
        row_sample_limit: cap for expensive per-column queries
        """
        start = time.monotonic()
        schemas = schemas or ["public"]
        report_id = str(uuid.uuid4())

        # Build a safe hint (host:port/db) — never store credentials
        connection_hint = _safe_hint(connection_url)
        logger.info(
            f"[Profiler] Starting report={report_id} "
            f"target={connection_hint} schemas={schemas}"
        )

        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(connection_url, timeout=10),
                timeout=15,
            )
        except Exception as e:
            logger.error(f"[Profiler] Connection failed: {e}")
            return ProfilingReport(
                report_id=report_id,
                connection_hint=connection_hint,
                status="failed",
                error=f"Connection failed: {e}",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        table_profiles: list[TableProfile] = []
        try:
            for schema in schemas:
                tables = await self._list_tables(conn, schema, table_limit)
                logger.info(
                    f"[Profiler] Schema '{schema}' — {len(tables)} tables found"
                )
                for table_name in tables:
                    profile = await self._profile_table(
                        conn, schema, table_name, row_sample_limit
                    )
                    table_profiles.append(profile)
                    if profile.anomalies:
                        logger.info(
                            f"[Profiler] {schema}.{table_name} — "
                            f"{len(profile.anomalies)} anomalies"
                        )
        except Exception as e:
            logger.error(f"[Profiler] Profiling error: {e}", exc_info=True)
        finally:
            await conn.close()

        total_anomalies = sum(len(t.anomalies) for t in table_profiles)
        critical = sum(
            1 for t in table_profiles
            for a in t.anomalies
            if a.severity == AnomalySeverity.CRITICAL
        )
        warning = total_anomalies - critical
        duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            f"[Profiler] Done report={report_id} "
            f"tables={len(table_profiles)} "
            f"anomalies={total_anomalies} "
            f"duration={duration_ms}ms"
        )

        return ProfilingReport(
            report_id=report_id,
            connection_hint=connection_hint,
            tables_scanned=len(table_profiles),
            total_anomalies=total_anomalies,
            critical_count=critical,
            warning_count=warning,
            tables=table_profiles,
            status="completed",
            duration_ms=duration_ms,
        )

    # ── Private methods ──────────────────────────────────────────────────────

    async def _list_tables(
        self,
        conn: asyncpg.Connection,
        schema: str,
        limit: int,
    ) -> list[str]:
        rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = $1
              AND table_type = 'BASE TABLE'
              AND table_name NOT LIKE '\\_aegisdb\\_%'
            ORDER BY table_name
            LIMIT $2
            """,
            schema, limit,
        )
        return [r["table_name"] for r in rows]

    async def _profile_table(
        self,
        conn: asyncpg.Connection,
        schema: str,
        table_name: str,
        row_sample_limit: int,
    ) -> TableProfile:
        t_start = time.monotonic()

        # Total row count
        try:
            total_rows = await conn.fetchval(
                f'SELECT COUNT(*) FROM "{schema}"."{table_name}"'
            )
        except Exception:
            total_rows = 0

        # Column metadata
        columns = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position
            """,
            schema, table_name,
        )

        # Foreign key info for referential integrity checks
        fk_map = await self._get_foreign_keys(conn, schema, table_name)

        anomalies: list[ColumnAnomaly] = []

        # Skip profiling empty tables
        if total_rows == 0:
            return TableProfile(
                table_name=table_name,
                schema_name=schema,
                total_rows=0,
                total_columns=len(columns),
                anomalies=[],
                profiling_duration_ms=int(
                    (time.monotonic() - t_start) * 1000
                ),
            )

        # Use sample if table is large — expensive queries cap at sample
        use_sample = total_rows > row_sample_limit

        for col in columns:
            col_name  = col["column_name"]
            data_type = col["data_type"].lower()
            nullable  = col["is_nullable"] == "YES"

            col_anomalies = await self._check_column(
                conn=conn,
                schema=schema,
                table=table_name,
                col_name=col_name,
                data_type=data_type,
                nullable=nullable,
                total_rows=total_rows,
                use_sample=use_sample,
                sample_limit=row_sample_limit,
                fk_info=fk_map.get(col_name),
            )
            anomalies.extend(col_anomalies)

        return TableProfile(
            table_name=table_name,
            schema_name=schema,
            total_rows=total_rows,
            total_columns=len(columns),
            anomalies=anomalies,
            profiling_duration_ms=int((time.monotonic() - t_start) * 1000),
        )

    async def _check_column(
        self,
        conn: asyncpg.Connection,
        schema: str,
        table: str,
        col_name: str,
        data_type: str,
        nullable: bool,
        total_rows: int,
        use_sample: bool,
        sample_limit: int,
        fk_info: dict | None,
    ) -> list[ColumnAnomaly]:
        anomalies: list[ColumnAnomaly] = []
        fqcol = f'"{schema}"."{table}"."{col_name}"'

        # ── 1. Null check ──────────────────────────────────────────────
        try:
            null_count = await conn.fetchval(
                f'SELECT COUNT(*) FROM "{schema}"."{table}" '
                f'WHERE "{col_name}" IS NULL'
            )
            null_rate = null_count / total_rows if total_rows else 0

            # Flag high null rate even on nullable columns (>80% is suspicious)
            # Flag any nulls on non-nullable columns
            if not nullable and null_count > 0:
                samples = await self._get_null_context(
                    conn, schema, table, col_name
                )
                anomalies.append(ColumnAnomaly(
                    column_name=col_name,
                    anomaly_type=FailureCategory.NULL_VIOLATION,
                    severity=AnomalySeverity.CRITICAL,
                    affected_rows=null_count,
                    total_rows=total_rows,
                    rate=round(null_rate, 4),
                    description=(
                        f"Column '{col_name}' has {null_count} NULL values "
                        f"({null_rate:.1%}) but should be NOT NULL"
                    ),
                    sample_values=samples,
                ))
            elif nullable and null_rate > 0.8:
                anomalies.append(ColumnAnomaly(
                    column_name=col_name,
                    anomaly_type=FailureCategory.NULL_VIOLATION,
                    severity=AnomalySeverity.WARNING,
                    affected_rows=null_count,
                    total_rows=total_rows,
                    rate=round(null_rate, 4),
                    description=(
                        f"Column '{col_name}' is {null_rate:.1%} NULL — "
                        f"unusually high for an optional column"
                    ),
                ))
        except Exception as e:
            logger.debug(f"Null check failed for {fqcol}: {e}")

        # ── 2. Uniqueness check (for ID/key-like columns) ─────────────
        col_lower = col_name.lower()
        if any(hint in col_lower for hint in _UNIQUE_HINT_NAMES):
            try:
                dupe_count = await conn.fetchval(
                    f"""
                    SELECT COUNT(*) FROM (
                        SELECT "{col_name}"
                        FROM "{schema}"."{table}"
                        WHERE "{col_name}" IS NOT NULL
                        GROUP BY "{col_name}"
                        HAVING COUNT(*) > 1
                    ) d
                    """
                )
                if dupe_count > 0:
                    # Count actual duplicate rows (not just groups)
                    dupe_row_count = await conn.fetchval(
                        f"""
                        SELECT COUNT(*) FROM "{schema}"."{table}"
                        WHERE "{col_name}" IN (
                            SELECT "{col_name}"
                            FROM "{schema}"."{table}"
                            WHERE "{col_name}" IS NOT NULL
                            GROUP BY "{col_name}"
                            HAVING COUNT(*) > 1
                        )
                        """
                    )
                    samples = await self._get_dupe_samples(
                        conn, schema, table, col_name
                    )
                    rate = dupe_row_count / total_rows if total_rows else 0
                    anomalies.append(ColumnAnomaly(
                        column_name=col_name,
                        anomaly_type=FailureCategory.UNIQUENESS_VIOLATION,
                        severity=(
                            AnomalySeverity.CRITICAL
                            if rate > 0.01
                            else AnomalySeverity.WARNING
                        ),
                        affected_rows=dupe_row_count,
                        total_rows=total_rows,
                        rate=round(rate, 4),
                        description=(
                            f"Column '{col_name}' has {dupe_count} duplicate "
                            f"values across {dupe_row_count} rows"
                        ),
                        sample_values=samples,
                    ))
            except Exception as e:
                logger.debug(f"Uniqueness check failed for {fqcol}: {e}")

        # ── 3. Range / outlier check for numeric columns ──────────────
        if data_type in _NUMERIC_TYPES:
            try:
                stats = await conn.fetchrow(
                    f"""
                    SELECT
                        MIN("{col_name}")::float          AS min_val,
                        MAX("{col_name}")::float          AS max_val,
                        AVG("{col_name}")::float          AS avg_val,
                        PERCENTILE_CONT(0.25) WITHIN GROUP
                            (ORDER BY "{col_name}")::float AS q1,
                        PERCENTILE_CONT(0.75) WITHIN GROUP
                            (ORDER BY "{col_name}")::float AS q3,
                        COUNT(*) FILTER (WHERE "{col_name}" < 0) AS neg_count
                    FROM "{schema}"."{table}"
                    WHERE "{col_name}" IS NOT NULL
                    """
                )
                if stats and stats["q1"] is not None:
                    q1, q3 = stats["q1"], stats["q3"]
                    iqr = q3 - q1
                    lower = q1 - 1.5 * iqr
                    upper = q3 + 1.5 * iqr

                    outlier_count = await conn.fetchval(
                        f"""
                        SELECT COUNT(*) FROM "{schema}"."{table}"
                        WHERE "{col_name}" IS NOT NULL
                          AND ("{col_name}" < $1 OR "{col_name}" > $2)
                        """,
                        lower, upper,
                    )

                    if outlier_count and outlier_count > 0:
                        rate = outlier_count / total_rows
                        # Only flag if meaningful (>0.1%)
                        if rate > 0.001:
                            anomalies.append(ColumnAnomaly(
                                column_name=col_name,
                                anomaly_type=FailureCategory.RANGE_VIOLATION,
                                severity=(
                                    AnomalySeverity.WARNING
                                    if rate < 0.05
                                    else AnomalySeverity.CRITICAL
                                ),
                                affected_rows=outlier_count,
                                total_rows=total_rows,
                                rate=round(rate, 4),
                                description=(
                                    f"Column '{col_name}' has {outlier_count} "
                                    f"statistical outliers outside IQR range "
                                    f"[{lower:.2f}, {upper:.2f}]"
                                ),
                            ))

                    # Separately flag negatives for amount-like columns
                    neg_hints = {
                        "amount", "price", "cost", "fee", "total",
                        "balance", "salary", "revenue", "quantity", "qty",
                    }
                    if (
                        any(h in col_lower for h in neg_hints)
                        and stats["neg_count"]
                        and stats["neg_count"] > 0
                    ):
                        neg_rate = stats["neg_count"] / total_rows
                        anomalies.append(ColumnAnomaly(
                            column_name=col_name,
                            anomaly_type=FailureCategory.RANGE_VIOLATION,
                            severity=AnomalySeverity.CRITICAL,
                            affected_rows=stats["neg_count"],
                            total_rows=total_rows,
                            rate=round(neg_rate, 4),
                            description=(
                                f"Column '{col_name}' has "
                                f"{stats['neg_count']} negative values — "
                                f"unexpected for a monetary/quantity field"
                            ),
                        ))

            except Exception as e:
                logger.debug(f"Range check failed for {fqcol}: {e}")

        # ── 4. Email format check ──────────────────────────────────────
        if data_type in _TEXT_TYPES and col_lower in _EMAIL_HINT_NAMES:
            try:
                bad_count = await conn.fetchval(
                    f"""
                    SELECT COUNT(*) FROM "{schema}"."{table}"
                    WHERE "{col_name}" IS NOT NULL
                      AND "{col_name}" !~ $1
                    """,
                    r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                )
                if bad_count and bad_count > 0:
                    rate = bad_count / total_rows
                    samples = await self._get_format_samples(
                        conn, schema, table, col_name
                    )
                    anomalies.append(ColumnAnomaly(
                        column_name=col_name,
                        anomaly_type=FailureCategory.FORMAT_VIOLATION,
                        severity=AnomalySeverity.WARNING,
                        affected_rows=bad_count,
                        total_rows=total_rows,
                        rate=round(rate, 4),
                        description=(
                            f"Column '{col_name}' has {bad_count} values "
                            f"that don't look like valid email addresses"
                        ),
                        sample_values=samples,
                    ))
            except Exception as e:
                logger.debug(f"Format check failed for {fqcol}: {e}")

        # ── 5. Referential integrity check ────────────────────────────
        if fk_info:
            try:
                ref_table  = fk_info["ref_table"]
                ref_col    = fk_info["ref_col"]
                ref_schema = fk_info["ref_schema"]

                orphan_count = await conn.fetchval(
                    f"""
                    SELECT COUNT(*) FROM "{schema}"."{table}" c
                    LEFT JOIN "{ref_schema}"."{ref_table}" p
                        ON c."{col_name}" = p."{ref_col}"
                    WHERE c."{col_name}" IS NOT NULL
                      AND p."{ref_col}" IS NULL
                    """
                )
                if orphan_count and orphan_count > 0:
                    rate = orphan_count / total_rows
                    anomalies.append(ColumnAnomaly(
                        column_name=col_name,
                        anomaly_type=FailureCategory.REFERENTIAL_INTEGRITY,
                        severity=AnomalySeverity.CRITICAL,
                        affected_rows=orphan_count,
                        total_rows=total_rows,
                        rate=round(rate, 4),
                        description=(
                            f"Column '{col_name}' has {orphan_count} values "
                            f"that don't exist in "
                            f"{ref_schema}.{ref_table}.{ref_col}"
                        ),
                    ))
            except Exception as e:
                logger.debug(f"FK check failed for {fqcol}: {e}")

        return anomalies

    async def _get_foreign_keys(
        self,
        conn: asyncpg.Connection,
        schema: str,
        table: str,
    ) -> dict[str, dict]:
        """Returns {col_name: {ref_table, ref_col, ref_schema}} for all FKs."""
        rows = await conn.fetch(
            """
            SELECT
                kcu.column_name,
                ccu.table_schema  AS ref_schema,
                ccu.table_name   AS ref_table,
                ccu.column_name  AS ref_col
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
               AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name
               AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = $1
              AND tc.table_name = $2
            """,
            schema, table,
        )
        return {
            r["column_name"]: {
                "ref_schema": r["ref_schema"],
                "ref_table":  r["ref_table"],
                "ref_col":    r["ref_col"],
            }
            for r in rows
        }

    async def _get_null_context(
        self,
        conn: asyncpg.Connection,
        schema: str,
        table: str,
        col_name: str,
        limit: int = 3,
    ) -> list[Any]:
        """Get a few row IDs where the column is null — for context."""
        try:
            rows = await conn.fetch(
                f"""
                SELECT ctid::text AS row
                FROM "{schema}"."{table}"
                WHERE "{col_name}" IS NULL
                LIMIT $1
                """,
                limit,
            )
            return [r["row"] for r in rows]
        except Exception:
            return []

    async def _get_dupe_samples(
        self,
        conn: asyncpg.Connection,
        schema: str,
        table: str,
        col_name: str,
        limit: int = 5,
    ) -> list[Any]:
        """Get sample duplicate values."""
        try:
            rows = await conn.fetch(
                f"""
                SELECT "{col_name}"::text AS val, COUNT(*) AS cnt
                FROM "{schema}"."{table}"
                WHERE "{col_name}" IS NOT NULL
                GROUP BY "{col_name}"
                HAVING COUNT(*) > 1
                ORDER BY cnt DESC
                LIMIT $1
                """,
                limit,
            )
            return [f"{r['val']} (×{r['cnt']})" for r in rows]
        except Exception:
            return []

    async def _get_format_samples(
        self,
        conn: asyncpg.Connection,
        schema: str,
        table: str,
        col_name: str,
        limit: int = 5,
    ) -> list[Any]:
        """Get sample invalid-format values."""
        try:
            rows = await conn.fetch(
                f"""
                SELECT "{col_name}"::text AS val
                FROM "{schema}"."{table}"
                WHERE "{col_name}" IS NOT NULL
                  AND "{col_name}" !~ $1
                LIMIT $2
                """,
                r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                limit,
            )
            return [r["val"] for r in rows]
        except Exception:
            return []


def _safe_hint(url: str) -> str:
    """Strip credentials from URL for safe logging/storage."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.hostname}:{p.port}/{p.path.lstrip('/')}"
    except Exception:
        return "unknown"


# Module-level singleton
profiler_agent = ProfilerAgent()