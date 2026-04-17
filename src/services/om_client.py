import logging
from base64 import b64encode

import httpx

from src.core.config import settings
from src.core.models import TableContext, ColumnInfo

logger = logging.getLogger(__name__)


class OpenMetadataClient:
    """
    Thin async client over OpenMetadata REST API.
    Handles auth token refresh automatically.
    """

    def __init__(self):
        self._token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=settings.om_host,
            timeout=10.0,
        )

    @staticmethod
    def _encode_password(password: str) -> str:
        """
        OpenMetadata expects the login password as a Base64 string.
        If the setting is already stored as `base64:...`, use it as-is.
        """
        if password.startswith("base64:"):
            return password.removeprefix("base64:")
        return b64encode(password.encode("utf-8")).decode("utf-8")

    async def _get_token(self) -> str:
        if self._token:
            return self._token

        resp = await self._client.post(
            "/api/v1/users/login",
            json={
                "email": settings.om_admin_email,
                "password": settings.om_admin_password,
            },
            headers={"Content-Type": "application/json"},
        )

        if resp.status_code != 200:
            # Try the older endpoint format
            resp = await self._client.post(
                "/api/v1/users/login",
                content=f'{{"email":"{settings.om_admin_email}","password":"{settings.om_admin_password}"}}',
                headers={"Content-Type": "application/json"},
            )

        resp.raise_for_status()
        data = resp.json()
        self._token = data.get("accessToken") or data.get("jwtToken") or data.get("token")
        if not self._token:
            raise ValueError(f"No token in OM login response. Keys: {list(data.keys())}")
        logger.info(f"OM auth token acquired — field used: {[k for k in data.keys() if 'oken' in k]}")
        return self._token

    async def _headers(self) -> dict:
        token = await self._get_token()
        return {"Authorization": f"Bearer {token}"}

    async def get_table_by_fqn(self, fqn: str) -> dict | None:
        """Get full table metadata including columns."""
        try:
            headers = await self._headers()
            # URL-encode dots in FQN
            encoded = fqn.replace(".", "%2E")
            resp = await self._client.get(
                f"/api/v1/tables/name/{encoded}",
                headers=headers,
                params={"fields": "columns,tableConstraints,followers,tags"},
            )
            if resp.status_code == 404:
                logger.warning(f"Table not found in OM: {fqn}")
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch table {fqn}: {e}")
            return None

    async def get_lineage(self, table_id: str) -> dict | None:
        """Get upstream + downstream lineage for a table."""
        try:
            headers = await self._headers()
            resp = await self._client.get(
                f"/api/v1/lineage/table/{table_id}",
                headers=headers,
                params={"upstreamDepth": 1, "downstreamDepth": 1},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch lineage for {table_id}: {e}")
            return None

    async def get_table_context(self, table_fqn: str) -> TableContext | None:
        """
        Main enrichment method — pulls schema + lineage and returns
        a clean TableContext ready for the Diagnosis agent.
        """
        table_data = await self.get_table_by_fqn(table_fqn)
        if not table_data:
            return None

        # Parse columns
        columns = [
            ColumnInfo(
                name=col["name"],
                dataType=col.get("dataType", "UNKNOWN"),
                nullable=col.get("constraint") != "NOT_NULL",
                description=col.get("description"),
            )
            for col in table_data.get("columns", [])
        ]

        # Parse lineage
        upstream, downstream = [], []
        lineage_data = await self.get_lineage(table_data["id"])
        if lineage_data:
            for edge in lineage_data.get("upstreamEdges", []):
                upstream.append(edge.get("fromEntity", {}).get("fqn", ""))
            for edge in lineage_data.get("downstreamEdges", []):
                downstream.append(edge.get("toEntity", {}).get("fqn", ""))

        # Parse FQN parts: service.database.schema.table
        fqn_parts = table_fqn.split(".")
        return TableContext(
            table_fqn=table_fqn,
            table_name=table_data.get("name", fqn_parts[-1]),
            database=fqn_parts[1] if len(fqn_parts) > 1 else "",
            schema_name=fqn_parts[2] if len(fqn_parts) > 2 else "public",
            columns=columns,
            upstream_tables=[u for u in upstream if u],
            downstream_tables=[d for d in downstream if d],
            row_count=table_data.get("profile", {}).get("rowCount"),
        )

    async def close(self):
        await self._client.aclose()


# Module-level singleton
om_client = OpenMetadataClient()
