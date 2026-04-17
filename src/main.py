import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from src.api.webhook import router
from src.services.om_client import om_client
from src.services.event_bus import event_bus
from src.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("AegisDB webhook service starting...")
    try:
        await event_bus.connect()
        logger.info("Redis event bus connected")
    except Exception as e:
        logger.warning(f"Redis not available: {e} — events will not be published")

    yield

    # Shutdown
    logger.info("AegisDB shutting down...")
    await om_client.close()
    await event_bus.close()


app = FastAPI(
    title="AegisDB",
    description="Self-healing database agent — webhook receiver",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )

# put in claude: {"status":"accepted","event_id":"35c1c7b1-85d0-4e97-9a92-cfdea1fc1665","table_fqn":"aegisdb-target.aegisdb.public.orders","failed_tests":1}