import asyncio
import base64
import logging
import re
import uuid

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)


# ── OM REST helpers ───────────────────────────────────────────────────────────

async def _get_om_token() -> str:
    """Get OM access token using base64-encoded password."""
    encoded = base64.b64encode(
        settings.om_admin_password.encode()
    ).decode()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.om_host}/api/v1/users/login",
            content=f'{{"email":"{settings.om_admin_email}","password":"{encoded}"}}',
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("accessToken")
            or data.get("jwtToken")
            or data.get("token")
        )


def _make_service_name(host: str, database: str, custom: str | None) -> str:
    """Generate a unique, OM-safe service name."""
    if custom:
        # Sanitize: only alphanumeric, underscore, hyphen
        return re.sub(r"[^a-zA-Z0-9_-]", "_", custom)
    safe_host = re.sub(r"[^a-zA-Z0-9]", "_", host)
    uid = str(uuid.uuid4())[:8]
    return f"aegisdb_{safe_host}_{database}_{uid}"


async def register_om_service(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    service_name: str,
) -> str:
    """
    Create a Postgres database service in OpenMetadata via REST API.
    Returns the service FQN.
    Uses the internal Docker network host for OM→DB connectivity.
    """
    token = await _get_om_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # For connections from within Docker network, containers
    # talk to each other by container name. For external DBs
    # use the provided host directly.
    om_host = host
    om_port = port

    service_body = {
        "name": service_name,
        "displayName": service_name,
        "serviceType": "Postgres",
        "connection": {
            "config": {
                "type": "Postgres",
                "scheme": "postgresql+psycopg2",
                "username": username,
                "authType": {"password": password},
                "hostPort": f"{om_host}:{om_port}",
                "database": database,
            }
        },
    }

    async with httpx.AsyncClient(
        base_url=settings.om_host, timeout=15
    ) as client:
        # Try create — if already exists (409) fetch the existing one
        resp = await client.post(
            "/api/v1/services/databaseServices",
            json=service_body,
            headers=headers,
        )
        if resp.status_code == 409:
            # Service name already exists — fetch it
            logger.info(
                f"[OMIngestion] Service '{service_name}' exists — fetching"
            )
            resp = await client.get(
                f"/api/v1/services/databaseServices/name/{service_name}",
                headers=headers,
            )

        resp.raise_for_status()
        data = resp.json()
        fqn = data.get("fullyQualifiedName", service_name)
        logger.info(f"[OMIngestion] Service registered: {fqn}")
        return fqn


def _build_ingestion_config(
    service_name: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    om_token: str,
) -> dict:
    """
    Build MetadataWorkflow config dict for Postgres ingestion.
    Runs entirely in-process — no Airflow, no race conditions.
    """
    return {
        "source": {
            "type": "postgres",
            "serviceName": service_name,
            "serviceConnection": {
                "config": {
                    "type": "Postgres",
                    "username": username,
                    "authType": {"password": password},
                    "hostPort": f"{host}:{port}",
                    "database": database,
                }
            },
            "sourceConfig": {
                "config": {
                    "type": "DatabaseMetadata",
                    "markDeletedTables": True,
                    "includeTables": True,
                    "includeViews": True,
                }
            },
        },
        "sink": {
            "type": "metadata-rest",
            "config": {},
        },
        "workflowConfig": {
            "loggerLevel": "WARNING",
            "openMetadataServerConfig": {
                "hostPort": f"{settings.om_host}/api",
                "authProvider": "openmetadata",
                "securityConfig": {
                    "jwtToken": om_token,
                },
            },
        },
    }


def _run_ingestion_sync(config: dict) -> tuple[bool, str]:
    """
    Synchronous ingestion — runs in asyncio.to_thread.
    Uses MetadataWorkflow directly, completely bypassing Airflow.
    Returns (success, error_message).
    """
    try:
        from metadata.workflow.metadata import MetadataWorkflow

        workflow = MetadataWorkflow.create(config)
        workflow.execute()
        workflow.raise_from_status()
        workflow.print_status()
        workflow.stop()
        return True, ""
    except Exception as e:
        return False, str(e)


async def run_ingestion(
    service_name: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> tuple[bool, str]:
    """
    Async wrapper: runs the synchronous MetadataWorkflow in a thread.
    Returns (success, error_message).
    """
    logger.info(
        f"[OMIngestion] Starting ingestion for service={service_name}"
    )
    token = await _get_om_token()
    config = _build_ingestion_config(
        service_name=service_name,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        om_token=token,
    )

    success, error = await asyncio.wait_for(
        asyncio.to_thread(_run_ingestion_sync, config),
        timeout=120,  # 2 min cap — most schemas ingest in <30s
    )

    if success:
        logger.info(
            f"[OMIngestion] Ingestion complete for service={service_name}"
        )
    else:
        logger.error(
            f"[OMIngestion] Ingestion failed for service={service_name}: "
            f"{error}"
        )

    return success, error


async def count_ingested_tables(service_name: str) -> int:
    """Count tables OM discovered after ingestion."""
    try:
        token = await _get_om_token()
        async with httpx.AsyncClient(
            base_url=settings.om_host, timeout=10
        ) as client:
            resp = await client.get(
                "/api/v1/tables",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "database": service_name,
                    "limit": 1,
                    "include": "non-deleted",
                },
            )
            if resp.status_code == 200:
                return resp.json().get("paging", {}).get("total", 0)
    except Exception as e:
        logger.warning(f"[OMIngestion] Table count failed: {e}")
    return 0