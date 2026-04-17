import asyncio
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from src.api.webhook import router
from src.services.om_client import om_client
from src.services.event_bus import event_bus
from src.services.stream_consumer import stream_consumer
from src.agents.repair import repair_agent
from src.db.vector_store import vector_store
from src.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_consumer_task: asyncio.Task | None = None
_repair_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer_task, _repair_task

    logger.info("AegisDB starting...")

    # Vector store
    try:
        vector_store.connect()
        vector_store.seed_bootstrap_fixes()
    except Exception as e:
        logger.error(f"ChromaDB init failed: {e}")

    # Redis event bus
    try:
        await event_bus.connect()
    except Exception as e:
        logger.warning(f"Event bus unavailable: {e}")

    # Diagnosis stream consumer
    try:
        await stream_consumer.connect()
        _consumer_task = asyncio.create_task(
            stream_consumer.start(), name="diagnosis-consumer"
        )
        logger.info("Diagnosis stream consumer started")
    except Exception as e:
        logger.warning(f"Diagnosis consumer unavailable: {e}")

    # Repair agent
    try:
        await repair_agent.connect()
        _repair_task = asyncio.create_task(
            repair_agent.start(), name="repair-agent"
        )
        logger.info("Repair agent started")
    except Exception as e:
        logger.warning(f"Repair agent unavailable: {e}")

    logger.info(
        f"AegisDB ready — DRY_RUN={'ON' if settings.dry_run else 'OFF'}"
    )
    yield

    # Shutdown — cancel in reverse start order
    for task, name in [(_repair_task, "repair"), (_consumer_task, "consumer")]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info(f"{name} task cancelled cleanly")

    await repair_agent.close()
    await stream_consumer.close()
    await om_client.close()
    await event_bus.close()


app = FastAPI(
    title="AegisDB",
    description="Self-healing database agent",
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")


@app.get("/api/v1/status")
async def status():
    return {
        "status": "running",
        "dry_run": settings.dry_run,
        "components": {
            "webhook": "ok",
            "event_bus": "ok",
            "diagnosis_consumer": (
                "ok" if _consumer_task and not _consumer_task.done() else "stopped"
            ),
            "repair_agent": (
                "ok" if _repair_task and not _repair_task.done() else "stopped"
            ),
            "vector_store": "ok",
        },
    }


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )