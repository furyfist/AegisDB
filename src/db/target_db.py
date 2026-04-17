import logging
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection
from sqlalchemy import text
from src.core.config import settings

logger = logging.getLogger(__name__)


def _build_url() -> str:
    s = settings
    return (
        f"postgresql+asyncpg://{s.target_db_user}:{s.target_db_password}"
        f"@{s.target_db_host}:{s.target_db_port}/{s.target_db_name}"
    )


_engine = create_async_engine(
    _build_url(),
    pool_size=2,
    max_overflow=0,
    pool_timeout=10,
    pool_pre_ping=True,
)


@asynccontextmanager
async def get_connection() -> AsyncConnection:
    async with _engine.connect() as conn:
        yield conn


async def get_table_ddl(table_name: str, schema: str = "public") -> str:
    """
    Reconstruct CREATE TABLE DDL from pg_catalog.
    Avoids needing pg_dump — works over a normal connection.
    """
    async with get_connection() as conn:
        # Get column definitions
        col_rows = await conn.execute(text("""
            SELECT
                c.column_name,
                c.data_type,
                c.character_maximum_length,
                c.numeric_precision,
                c.numeric_scale,
                c.is_nullable,
                c.column_default
            FROM information_schema.columns c
            WHERE c.table_schema = :schema
              AND c.table_name  = :table
            ORDER BY c.ordinal_position
        """), {"schema": schema, "table": table_name})

        columns = col_rows.fetchall()
        if not columns:
            raise ValueError(f"Table {schema}.{table_name} not found in target DB")

        col_defs = []
        for col in columns:
            name, dtype, char_len, num_prec, num_scale, nullable, default = col

            # Build type string
            if dtype == "character varying":
                type_str = f"VARCHAR({char_len})" if char_len else "VARCHAR"
            elif dtype == "numeric" and num_prec:
                type_str = f"NUMERIC({num_prec},{num_scale or 0})"
            else:
                type_str = dtype.upper()

            col_def = f"    {name} {type_str}"
            if default and "nextval" not in default:
                col_def += f" DEFAULT {default}"
            if nullable == "NO":
                col_def += " NOT NULL"
            col_defs.append(col_def)

        ddl = (
            f"CREATE TABLE IF NOT EXISTS {table_name} (\n"
            + ",\n".join(col_defs)
            + "\n);"
        )
        return ddl


async def get_sample_data(
    table_name: str,
    schema: str = "public",
    limit: int = 500,
) -> tuple[list[str], list[tuple]]:
    """
    Returns (column_names, rows) for seeding the sandbox.
    """
    async with get_connection() as conn:
        result = await conn.execute(
            text(f'SELECT * FROM "{schema}"."{table_name}" LIMIT :limit'),
            {"limit": limit},
        )
        rows = result.fetchall()
        col_names = list(result.keys())
        return col_names, [tuple(r) for r in rows]


async def get_row_count(table_name: str, schema: str = "public") -> int:
    async with get_connection() as conn:
        result = await conn.execute(
            text(f'SELECT COUNT(*) FROM "{schema}"."{table_name}"')
        )
        return result.scalar()