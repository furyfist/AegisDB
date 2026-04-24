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
        """
        Fetch JWT token. Clear cache and retry on first failure —
        handles expired tokens without a restart.
        """
        if self._token:
            # Validate cached token is still working with a lightweight ping
            try:
                resp = await self._client.get(
                    "/api/v1/system/version",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                if resp.status_code == 200:
                    return self._token
                # Token expired or invalid — clear and re-fetch
                logger.info("OM token expired — re-authenticating")
                self._token = None
            except Exception:
                self._token = None

        import base64
        encoded_password = base64.b64encode(
            settings.om_admin_password.encode()
        ).decode()

        resp = await self._client.post(
            "/api/v1/users/login",
            content=f'{{"email":"{settings.om_admin_email}","password":"{encoded_password}"}}',
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = (
            data.get("accessToken")
            or data.get("jwtToken")
            or data.get("token")
        )
        if not self._token:
            raise ValueError(
                f"No token in OM login response. Keys: {list(data.keys())}"
            )
        logger.info("OM auth token acquired")
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

    async def ensure_aegisdb_tag(self) -> bool:
        """
        One-time bootstrap: ensure the AegisDB classification and 
        'healed' tag exist in OpenMetadata.
        Called once at startup — idempotent, safe to call repeatedly.
        Returns True if ready, False if OM is unreachable.
        """
        try:
            headers = await self._headers()

            # 1. Check if classification already exists
            resp = await self._client.get(
                "/api/v1/classifications/name/AegisDB",
                headers=headers,
            )

            if resp.status_code == 404:
                # Create the classification
                resp = await self._client.post(
                    "/api/v1/classifications",
                    headers={**headers, "Content-Type": "application/json"},
                    content='{"name":"AegisDB","description":"Tags applied automatically by the AegisDB self-healing system.","mutuallyExclusive":false}',
                )
                if resp.status_code not in (200, 201):
                    logger.warning(
                        f"[OM] Could not create AegisDB classification: "
                        f"{resp.status_code} {resp.text[:200]}"
                    )
                    return False
                logger.info("[OM] Created AegisDB classification")
            else:
                logger.info("[OM] AegisDB classification already exists")

            # 2. Check if 'healed' tag exists under AegisDB
            resp = await self._client.get(
                "/api/v1/tags/name/AegisDB.healed",
                headers=headers,
            )

            if resp.status_code == 404:
                # Create the tag
                resp = await self._client.post(
                    "/api/v1/tags",
                    headers={**headers, "Content-Type": "application/json"},
                    content='{"name":"healed","description":"This table was automatically healed by AegisDB.","classification":"AegisDB"}'
                )
                if resp.status_code not in (200, 201):
                    logger.warning(
                        f"[OM] Could not create AegisDB.healed tag: "
                        f"{resp.status_code} {resp.text[:200]}"
                    )
                    return False
                logger.info("[OM] Created AegisDB.healed tag")
            else:
                logger.info("[OM] AegisDB.healed tag already exists")

            return True

        except Exception as e:
            logger.warning(f"[OM] ensure_aegisdb_tag failed (non-fatal): {e}")
            return False

    async def patch_column_description(
        self,
        table_fqn: str,
        column_name: str,
        fix_note: str,
    ) -> bool:
        """
        Append a fix note to a column's description in OpenMetadata.
        Uses JSON Patch format: PATCH /api/v1/tables/name/{fqn}

        Strategy:
        - GET the table to find the column index and existing description
        - Build a JSON Patch 'replace' operation preserving existing text
        - PATCH — only the target column is modified

        Returns True on success, False on any failure.
        Never raises.
        """
        try:
            headers = await self._headers()

            # Step 1: GET current table state
            encoded_fqn = table_fqn.replace(".", "%2E")
            resp = await self._client.get(
                f"/api/v1/tables/name/{encoded_fqn}",
                headers=headers,
                params={"fields": "columns"},
            )
            if resp.status_code == 404:
                logger.warning(
                    f"[OM] Table {table_fqn} not found — skipping column patch"
                )
                return False
            resp.raise_for_status()
            table_data = resp.json()

            # Step 2: Find the column index
            columns = table_data.get("columns", [])
            col_index = None
            existing_description = ""
            for i, col in enumerate(columns):
                if col.get("name", "").lower() == column_name.lower():
                    col_index = i
                    existing_description = col.get("description") or ""
                    break

            if col_index is None:
                logger.warning(
                    f"[OM] Column '{column_name}' not found in {table_fqn} — "
                    f"skipping column patch"
                )
                return False

            # Step 3: Build new description — append, never replace
            # Separator so multiple fixes don't smash together
            if existing_description:
                new_description = (
                    f"{existing_description}\n\n---\n{fix_note}"
                )
            else:
                new_description = fix_note

            # Step 4: PATCH with JSON Patch array
            # OM uses application/json-patch+json
            import json
            patch_payload = json.dumps([
                {
                    "op": "add",
                    "path": f"/columns/{col_index}/description",
                    "value": new_description,
                }
            ])

            table_id = table_data.get("id")
            resp = await self._client.patch(
                f"/api/v1/tables/{table_id}",
                headers={
                    **headers,
                    "Content-Type": "application/json-patch+json",
                },
                content=patch_payload,
            )

            if resp.status_code in (200, 201):
                logger.info(
                    f"[OM] Patched column description: "
                    f"{table_fqn}.{column_name}"
                )
                return True
            else:
                logger.warning(
                    f"[OM] Column patch returned {resp.status_code}: "
                    f"{resp.text[:300]}"
                )
                return False

        except Exception as e:
            logger.warning(
                f"[OM] patch_column_description failed (non-fatal): {e}"
            )
            return False

    async def add_table_tag(self, table_fqn: str) -> bool:
        """
        Add the AegisDB.healed tag to a table entity.
        GET table → check if tag already present → PATCH to add if missing.
        Returns True on success, False on any failure.
        Never raises.
        """
        try:
            headers = await self._headers()

            # Step 1: GET current table with tags
            encoded_fqn = table_fqn.replace(".", "%2E")
            resp = await self._client.get(
                f"/api/v1/tables/name/{encoded_fqn}",
                headers=headers,
                params={"fields": "tags"},
            )
            if resp.status_code == 404:
                logger.warning(
                    f"[OM] Table {table_fqn} not found — skipping tag"
                )
                return False
            resp.raise_for_status()
            table_data = resp.json()
            table_id = table_data.get("id")

            # Step 2: Check if tag already applied — avoid duplicates
            existing_tags = table_data.get("tags", []) or []
            already_tagged = any(
                t.get("tagFQN") == "AegisDB.healed"
                for t in existing_tags
            )
            if already_tagged:
                logger.info(
                    f"[OM] Table {table_fqn} already has AegisDB.healed tag"
                )
                return True

            # Step 3: Build new tags array — preserve existing, add ours
            import json
            new_tags = existing_tags + [
                {
                    "tagFQN": "AegisDB.healed",
                    "source": "Classification",
                    "labelType": "Automated",
                    "state": "Confirmed",
                }
            ]

            patch_payload = json.dumps([
                {
                    "op": "add",
                    "path": "/tags",
                    "value": new_tags,
                }
            ])

            resp = await self._client.patch(
                f"/api/v1/tables/{table_id}",
                headers={
                    **headers,
                    "Content-Type": "application/json-patch+json",
                },
                content=patch_payload,
            )

            if resp.status_code in (200, 201):
                logger.info(f"[OM] Added AegisDB.healed tag to {table_fqn}")
                return True
            else:
                logger.warning(
                    f"[OM] Tag patch returned {resp.status_code}: "
                    f"{resp.text[:300]}"
                )
                return False

        except Exception as e:
            logger.warning(f"[OM] add_table_tag failed (non-fatal): {e}")
            return False

    async def annotate_fix(
        self,
        table_fqn: str,
        column_name: str,
        anomaly_type: str,
        rows_affected: int,
        confidence: float,
        recurrence_count: int,
        fix_type: str,
        event_id: str,
    ) -> None:
        """
        Public entry point called from apply.py after a successful commit.
        Runs both annotation steps — column description + table tag.
        Completely non-blocking: all failures are logged and swallowed.

        The fix_note format is intentionally short and scannable —
        an engineer opening this column in OM 3 months later can read
        the entire history at a glance.
        """
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        ordinal = recurrence_count + 1
        suffix = (
            "st" if ordinal == 1
            else "nd" if ordinal == 2
            else "rd" if ordinal == 3
            else "th"
        )

        fix_note = (
            f"**[AegisDB {timestamp}]** "
            f"Fixed `{anomaly_type}` ({fix_type.upper()}) — "
            f"{rows_affected} row{'s' if rows_affected != 1 else ''} affected. "
            f"Confidence: {round(confidence * 100)}%. "
            f"Occurrence: {ordinal}{suffix}. "
            f"Event: `{event_id[:8]}`"
        )

        # Run both — independent, both best-effort
        col_ok = await self.patch_column_description(
            table_fqn, column_name, fix_note
        )
        tag_ok = await self.add_table_tag(table_fqn)

        logger.info(
            f"[OM] annotate_fix complete: "
            f"table={table_fqn} col={column_name} "
            f"col_patch={'ok' if col_ok else 'skipped'} "
            f"tag={'ok' if tag_ok else 'skipped'}"
        )

# Module-level singleton
om_client = OpenMetadataClient()