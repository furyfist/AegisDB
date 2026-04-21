import asyncio
import logging
import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request

from src.core.models import (
    ConnectRequest,
    ConnectResponse,
    ConnectionStatus,
    DatabaseConnection,
)
from src.db.connection_registry import (
    save_connection,
    get_connection,
    list_connections,
)
from src.services.om_ingestion import (
    _make_service_name,
    register_om_service,
    run_ingestion,
    count_ingested_tables,
)
from src.agents.profiler import profiler_agent
from src.db.profiling_store import save_report

logger = logging.getLogger(__name__)
router = APIRouter()


async def _validate_connection(
    host: str, port: int, database: str, username: str, password: str
) -> tuple[bool, str]:
    """
    Test the database connection before doing anything else.
    Returns (ok, error_message).
    """
    url = f"postgresql://{username}:{password}@{host}:{port}/{database}"
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(url, timeout=8), timeout=10
        )
        await conn.fetchval("SELECT 1")
        await conn.close()
        return True, ""
    except asyncio.TimeoutError:
        return False, "Connection timed out — check host and port"
    except asyncpg.InvalidPasswordError:
        return False, "Invalid username or password"
    except asyncpg.InvalidCatalogNameError:
        return False, f"Database '{database}' does not exist"
    except Exception as e:
        return False, str(e)


async def _onboard_database(
    conn_obj: DatabaseConnection,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
):
    """
    Full onboarding pipeline — runs in background after 200 is returned.
    Steps: register in OM → ingest metadata → profile → save results.
    """
    connection_id = conn_obj.connection_id
    service_name  = conn_obj.service_name

    # ── Step 1: Register service in OpenMetadata ──────────────────────
    conn_obj.status = ConnectionStatus.INGESTING
    await save_connection(conn_obj)

    try:
        fqn = await register_om_service(
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            service_name=service_name,
        )
        conn_obj.om_service_fqn = fqn
        logger.info(
            f"[Onboarding] {connection_id} — OM service registered: {fqn}"
        )
    except Exception as e:
        conn_obj.status = ConnectionStatus.FAILED
        conn_obj.error  = f"OM service registration failed: {e}"
        await save_connection(conn_obj)
        logger.error(f"[Onboarding] {connection_id} — registration failed: {e}")
        return

    # ── Step 2: Run metadata ingestion in-process ─────────────────────
    success, error = await run_ingestion(
        service_name=service_name,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
    )

    if not success:
        # Non-fatal: profiler works directly on Postgres
        # OM enrichment will be limited but pipeline still works
        logger.warning(
            f"[Onboarding] {connection_id} — OM ingestion failed: {error}. "
            f"Continuing with direct profiling."
        )

    tables_found = await count_ingested_tables(service_name)
    conn_obj.tables_found = tables_found
    logger.info(
        f"[Onboarding] {connection_id} — {tables_found} tables ingested"
    )

    # ── Step 3: Profile the database directly ─────────────────────────
    conn_obj.status = ConnectionStatus.PROFILING
    await save_connection(conn_obj)

    profiling_url = (
        f"postgresql://{username}:{password}@{host}:{port}/{database}"
    )
    try:
        report = await profiler_agent.profile(
            connection_url=profiling_url,
            schemas=conn_obj.schema_names,
            table_limit=50,
        )
        await save_report(report)

        conn_obj.profiling_report_id = report.report_id
        conn_obj.total_anomalies     = report.total_anomalies
        conn_obj.critical_count      = report.critical_count
        conn_obj.tables_found        = report.tables_scanned
        logger.info(
            f"[Onboarding] {connection_id} — "
            f"profiling done: {report.total_anomalies} anomalies"
        )
    except Exception as e:
        logger.error(
            f"[Onboarding] {connection_id} — profiling failed: {e}"
        )
        conn_obj.error = f"Profiling failed: {e}"

    # ── Step 4: Mark ready ────────────────────────────────────────────
    conn_obj.status = ConnectionStatus.READY
    from datetime import datetime
    conn_obj.last_profiled_at = datetime.now()
    await save_connection(conn_obj)

    logger.info(
        f"[Onboarding] {connection_id} — "
        f"READY | anomalies={conn_obj.total_anomalies} "
        f"critical={conn_obj.critical_count}"
    )


@router.post("/connect", response_model=ConnectResponse)
async def connect_database(
    request: ConnectRequest,
    background_tasks: BackgroundTasks,
):
    """
    Connect a Postgres database to AegisDB.

    1. Validates connection credentials immediately
    2. Returns 200 with connection_id
    3. Registers in OpenMetadata + runs ingestion + profiles in background

    Poll GET /api/v1/connections/{connection_id} to track progress.
    """
    # Validate first — fail fast, don't start background work on bad creds
    ok, err = await _validate_connection(
        request.host, request.port,
        request.database, request.username, request.password,
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot connect to database: {err}",
        )

    service_name = _make_service_name(
        request.host, request.database, request.service_name
    )

    hint = f"{request.host}:{request.port}/{request.database}"

    conn_obj = DatabaseConnection(
        service_name=service_name,
        connection_hint=hint,
        db_name=request.database,
        schema_names=request.schemas,
        status=ConnectionStatus.PENDING,
    )

    # Persist immediately so poll endpoint works right away
    await save_connection(conn_obj)

    # Full onboarding runs in background
    background_tasks.add_task(
        _onboard_database,
        conn_obj,
        request.host,
        request.port,
        request.database,
        request.username,
        request.password,
    )

    logger.info(
        f"[Connect] Accepted connection_id={conn_obj.connection_id} "
        f"service={service_name} db={hint}"
    )

    return ConnectResponse(
        connection_id=conn_obj.connection_id,
        service_name=service_name,
        connection_hint=hint,
        status=ConnectionStatus.PENDING,
        tables_found=0,
        total_anomalies=0,
        critical_count=0,
        profiling_report_id=None,
        message=(
            "Connection validated. Onboarding started in background. "
            f"Poll GET /api/v1/connections/{conn_obj.connection_id} "
            "for status."
        ),
    )


@router.get("/connections/{connection_id}")
async def get_connection_status(connection_id: str):
    """
    Poll onboarding status. Transitions:
    pending → ingesting → profiling → ready (or failed)
    """
    conn = await get_connection(connection_id)
    if not conn:
        raise HTTPException(
            status_code=404,
            detail=f"Connection {connection_id} not found",
        )
    return conn


@router.get("/connections")
async def list_all_connections():
    """List all registered database connections."""
    connections = await list_connections()
    return {"count": len(connections), "connections": connections}


@router.post("/connections/{connection_id}/re-profile")
async def re_profile_connection(
    connection_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    body = await request.json()

    required = ["host", "port", "database", "username", "password"]
    missing = [f for f in required if f not in body]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {missing}. Credentials must be re-supplied.",
        )

    conn_record = await connection_registry.get_connection(connection_id)
    if not conn_record:
        raise HTTPException(status_code=404, detail="Connection not found")

    host     = body["host"]
    port     = int(body["port"])
    database = body["database"]
    username = body["username"]
    password = body["password"]
    schemas  = body.get("schemas", ["public"])

    connection_url = f"postgresql://{username}:{password}@{host}:{port}/{database}"

    # Validate credentials synchronously before returning
    try:
        import asyncpg as _asyncpg
        _c = await _asyncpg.connect(connection_url, timeout=10)
        await _c.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")

    # Mark as profiling immediately
    await connection_registry.update_connection(
        connection_id, {"status": "profiling"}
    )

    async def _run_reprofiling():
        try:
            from src.agents.profiler import ProfilerAgent
            from src.db.profiling_store import save_profiling_report

            profiler = ProfilerAgent()
            report   = await profiler.profile_database(
                connection_url=connection_url,
                schemas=schemas,
                table_limit=50,
            )
            report_id = await save_profiling_report(report)

            await connection_registry.update_connection(
                connection_id,
                {
                    "status":              "ready",
                    "profiling_report_id": report_id,
                    "total_anomalies":     report.total_anomalies,
                    "critical_count":      report.critical_count,
                    "tables_found":        len(report.tables),
                    "last_profiled_at":    "now()",
                    "error":               None,
                },
            )
        except Exception as e:
            await connection_registry.update_connection(
                connection_id, {"status": "failed", "error": str(e)}
            )

    background_tasks.add_task(_run_reprofiling)

    return {
        "connection_id": connection_id,
        "status":        "profiling",
        "message":       "Re-profiling started. Poll GET /api/v1/connections/{id} for status.",
    }