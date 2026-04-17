import asyncio
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from src.api.webhook import router
from src.services.om_client import om_client
from src.services.event_bus import event_bus
from src.services.stream_consumer import stream_consumer
from src.db.vector_store import vector_store
from src.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_consumer_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer_task

    logger.info("AegisDB starting...")

    # Vector store — synchronous init
    try:
        vector_store.connect()
        vector_store.seed_bootstrap_fixes()
    except Exception as e:
        logger.error(f"ChromaDB init failed: {e}")

    # Redis event bus
    try:
        await event_bus.connect()
        logger.info("Event bus ready")
    except Exception as e:
        logger.warning(f"Redis event bus unavailable: {e}")

    # Redis stream consumer — runs as background task
    try:
        await stream_consumer.connect()
        _consumer_task = asyncio.create_task(
            stream_consumer.start(),
            name="stream-consumer",
        )
        logger.info("Stream consumer started")
    except Exception as e:
        logger.warning(f"Stream consumer unavailable: {e}")

    logger.info("AegisDB ready — listening on http://localhost:8000")
    yield

    # Shutdown
    logger.info("AegisDB shutting down...")
    stream_consumer.stop()
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass

    await om_client.close()
    await event_bus.close()
    await stream_consumer.close()


app = FastAPI(
    title="AegisDB",
    description="Self-healing database agent",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")


@app.get("/api/v1/status")
async def status():
    """Quick system status — useful for demos."""
    return {
        "status": "running",
        "components": {
            "webhook": "ok",
            "event_bus": "ok",
            "stream_consumer": "ok" if not _consumer_task.done() else "stopped",
            "vector_store": "ok",
        }
    }


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,   # reload=True breaks asyncio tasks on Windows
        reload_dirs=["src"],
    )