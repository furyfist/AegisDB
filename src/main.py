import asyncio
import logging
import time
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import table_routes
from src.api.webhook import router as webhook_router
from src.api.dashboard import router as dashboard_router
from src.services.om_client import om_client
from src.services.event_bus import event_bus
from src.services.stream_consumer import stream_consumer
from src.agents.repair import repair_agent
from src.agents.apply import apply_agent
from src.db.vector_store import vector_store
from src.db.audit_log import init_audit_table, close_audit
from src.core.config import settings
from src.db.event_store import init_event_store, close_event_store
from src.api.profiler_routes import router as profiler_router
from src.db.profiling_store import init_profiling_store, close_profiling_store
from src.api.onboarding_routes import router as onboarding_router
from src.api.proposal_routes import router as proposal_router
from src.db.proposal_store import init_proposal_store, close_proposal_store
from src.db.connection_registry import (
    init_connection_registry,
    close_connection_registry,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}


def _make_task(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks[name] = task
    return task


def _task_ok(name: str) -> str:
    t = _tasks.get(name)
    if t is None:
        return "not_started"
    if t.done():
        exc = t.exception() if not t.cancelled() else None
        return f"stopped({'error' if exc else 'cancelled'})"
    return "ok"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("AegisDB starting up")
    logger.info(f"  DRY_RUN       : {settings.dry_run}")
    logger.info(f"  POST_VERIFY   : {settings.post_apply_verify}")
    logger.info(f"  CONFIDENCE_θ  : {settings.confidence_threshold}")
    logger.info("=" * 60)

    #  1. Vector store (sync, must be first) 
    try:
        vector_store.connect()
        vector_store.seed_bootstrap_fixes()
        logger.info("[Boot] ChromaDB ready")
    except Exception as e:
        logger.error(f"[Boot] ChromaDB FAILED: {e}")

    #  2. Audit table in target DB 
    try:
        await init_audit_table()
        logger.info("[Boot] Audit table ready")
    except Exception as e:
        logger.warning(f"[Boot] Audit table unavailable (non-fatal): {e}")

    #  3. Redis event bus 
    try:
        await event_bus.connect()
        logger.info("[Boot] Event bus ready")
    except Exception as e:
        logger.warning(f"[Boot] Event bus unavailable: {e}")

    #  4. Diagnosis stream consumer 
    try:
        await stream_consumer.connect()
        _make_task(stream_consumer.start(), "diagnosis-consumer")
        logger.info("[Boot] Diagnosis consumer running")
    except Exception as e:
        logger.warning(f"[Boot] Diagnosis consumer unavailable: {e}")

    #  5. Repair agent 
    try:
        await repair_agent.connect()
        _make_task(repair_agent.start(), "repair-agent")
        logger.info("[Boot] Repair agent running")
    except Exception as e:
        logger.warning(f"[Boot] Repair agent unavailable: {e}")

    #  6. Apply agent
    try:
        await apply_agent.connect()
        _make_task(apply_agent.start(), "apply-agent")
        logger.info("[Boot] Apply agent running")
    except Exception as e:
        logger.warning(f"[Boot] Apply agent unavailable: {e}")
    
    # ── 2b. Event store 
    try:
        await init_event_store()
        logger.info("[Boot] Event store ready")
    except Exception as e:
        logger.warning(f"[Boot] Event store unavailable (non-fatal): {e}")

    # ── 2c. Profiling store 
    try:
        await init_profiling_store()
        logger.info("[Boot] Profiling store ready")
    except Exception as e:
        logger.warning(f"[Boot] Profiling store unavailable: {e}")

    # ── 2d. Connection registry 
    try:
        await init_connection_registry()
        logger.info("[Boot] Connection registry ready")
    except Exception as e:
        logger.warning(f"[Boot] Connection registry unavailable: {e}")    
    
    # ── 2e. Proposal store 
    try:
        await init_proposal_store()
        logger.info("[Boot] Proposal store ready")
    except Exception as e:
        logger.warning(f"[Boot] Proposal store unavailable: {e}")

    logger.info("AegisDB ready ✓")
    logger.info(f"  Webhook  → http://localhost:{settings.app_port}/api/v1/webhook/om-test-failure")
    logger.info(f"  Audit    → http://localhost:{settings.app_port}/api/v1/audit")
    logger.info(f"  Streams  → http://localhost:{settings.app_port}/api/v1/streams")
    logger.info(f"  Docs     → http://localhost:{settings.app_port}/docs")

    yield  # ← app runs here

    #  Shutdown 
    logger.info("AegisDB shutting down...")

    for agent in [apply_agent, repair_agent, stream_consumer]:
        agent.stop()

    for name, task in _tasks.items():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.debug(f"{name} cancelled cleanly")
        except Exception as e:
            logger.warning(f"{name} shutdown error: {e}")

    await apply_agent.close()
    await repair_agent.close()
    await stream_consumer.close()
    await om_client.close()
    await event_bus.close()
    await close_event_store()
    await close_profiling_store()
    await close_connection_registry()
    await close_proposal_store()
    await close_audit()

    logger.info("AegisDB shutdown complete")


app = FastAPI(
    title="AegisDB",
    description="Autonomous self-healing database agent",
    version="0.4.0",
    lifespan=lifespan,
)

# ── CORS: allow the frontend dev server at localhost:3000 ──────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global unhandled-exception handler — no raw tracebacks to client ───────
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        f"[Unhandled] {request.method} {request.url.path} → {exc}",
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error": str(exc)},
    )


# ── Simple request logger middleware ──────────────────────────────────────
@app.middleware("http")
async def _log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration_ms = round((time.time() - start) * 1000)
    logger.info(
        f"{request.method} {request.url.path} → {response.status_code} "
        f"({duration_ms}ms)"
    )
    return response


app.include_router(webhook_router, prefix="/api/v1", tags=["Webhook"])
app.include_router(dashboard_router, prefix="/api/v1", tags=["Dashboard"])
app.include_router(profiler_router, prefix="/api/v1", tags=["Profiler"])
app.include_router(onboarding_router, prefix="/api/v1", tags=["Onboarding"])
app.include_router(proposal_router, prefix="/api/v1", tags=["Proposals"])
app.include_router(table_routes.router, prefix="/api/v1", tags=["tables"])

@app.get("/api/v1/status", tags=["Health"])
async def status():
    """Full system health — all 5 pipeline stages."""
    return {
        "version": "0.4.0",
        "dry_run": settings.dry_run,
        "confidence_threshold": settings.confidence_threshold,
        "pipeline": {
            "1_webhook":            "ok",
            "2_event_bus":          "ok",
            "3_diagnosis_consumer": _task_ok("diagnosis-consumer"),
            "4_repair_agent":       _task_ok("repair-agent"),
            "5_apply_agent":        _task_ok("apply-agent"),
        },
    }


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,  # reload=True kills background tasks on Windows
        reload_dirs=["src"],
    )
    