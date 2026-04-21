from fastapi import APIRouter, HTTPException
import asyncpg
import datetime
import decimal

router = APIRouter()


@router.get("/tables/live")
async def get_live_table_data(
    connection_id: str,
    table_name: str,
    schema: str = "public",
    limit: int = 100,
):
    from src.db.connection_registry import get_connection
    from src.db.profiling_store import get_report as get_profiling_report
    from src.core.config import settings

    if limit > 500:
        limit = 500

    conn_record = await get_connection(connection_id)
    if not conn_record:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Replace conn_record.get("db_name") → conn_record.db_name
    # Replace conn_record.get("profiling_report_id") → conn_record.profiling_report_id
    # Replace conn_record.get("last_profiled_at") → conn_record.last_profiled_at

    connection_url = (
        f"postgresql://{settings.target_db_user}:{settings.target_db_password}"
        f"@{settings.target_db_host}:{settings.target_db_port}"
        f"/{conn_record.db_name}"
    )

    try:
        conn = await asyncpg.connect(connection_url, timeout=10)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {str(e)}")

    try:
        # Column metadata
        col_rows = await conn.fetch("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position
        """, schema, table_name)

        columns = [
            {
                "name":     r["column_name"],
                "type":     r["data_type"],
                "nullable": r["is_nullable"] == "YES",
            }
            for r in col_rows
        ]

        if not columns:
            raise HTTPException(
                status_code=404,
                detail=f"Table '{schema}.{table_name}' not found",
            )

        # Total count
        total_rows = await conn.fetchval(
            f'SELECT COUNT(*) FROM "{schema}"."{table_name}"'
        )

        # Live rows
        rows_raw = await conn.fetch(
            f'SELECT * FROM "{schema}"."{table_name}" LIMIT $1', limit
        )

        def _serialize(v):
            if isinstance(v, (datetime.date, datetime.datetime)):
                return v.isoformat()
            if isinstance(v, decimal.Decimal):
                return float(v)
            if isinstance(v, bytes):
                return None  # skip binary
            return v

        rows = [
            {k: _serialize(v) for k, v in dict(r).items()}
            for r in rows_raw
        ]

    finally:
        await conn.close()

    # Anomaly columns from latest profiling report
    anomaly_columns: list[str] = []
    if conn_record.profiling_report_id:
        try:
            from src.db.profiling_store import get_report
            report = await get_report(conn_record.profiling_report_id)
            if report:
                for t in (report.tables or []):
                    if t.table_name == table_name:
                        anomaly_columns = [
                            a.column_name
                            for a in (t.anomalies or [])
                        ]
                        break
        except Exception:
            pass

    return {
        "table_name":      table_name,
        "schema":          schema,
        "columns":         columns,
        "rows":            rows,
        "total_rows":      total_rows,
        "returned_rows":   len(rows),
        "anomaly_columns": anomaly_columns,
        "last_profiled_at": conn_record.last_profiled_at.isoformat() if conn_record.last_profiled_at else None,
    }