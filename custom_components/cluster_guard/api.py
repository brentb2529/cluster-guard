"""A thin, read-mostly client for the Proxmox VE API (token auth, aiohttp)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=15)


class ProxmoxError(Exception):
    """Base error."""


class ProxmoxAuthError(ProxmoxError):
    """Token rejected or lacking privileges."""


class ProxmoxConnectionError(ProxmoxError):
    """Node unreachable."""


class ProxmoxClient:
    """Talks to one node's API; a cluster answers cluster-wide questions from any node."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token_id: str,
        token_secret: str,
        verify_ssl: bool,
    ) -> None:
        self._session = session
        self._base = f"https://{host}:{port}/api2/json"
        self._headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        self._ssl: bool | None = None if verify_ssl else False
        self.host = host

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method, url, headers=self._headers, ssl=self._ssl, timeout=TIMEOUT, **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    raise ProxmoxAuthError(f"{resp.status} {resp.reason} for {path}")
                if resp.status >= 400:
                    text = await resp.text()
                    raise ProxmoxError(f"{resp.status} {resp.reason} for {path}: {text[:200]}")
                body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise ProxmoxConnectionError(f"{self.host}: {err}") from err
        return body.get("data") if isinstance(body, dict) else body

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def post(self, path: str, **data: Any) -> Any:
        return await self._request("POST", path, data=data or None)

    # ---- cluster-wide reads

    async def version(self) -> dict[str, Any]:
        return await self.get("/version")

    async def cluster_status(self) -> list[dict[str, Any]]:
        return await self.get("/cluster/status")

    async def ha_status(self) -> list[dict[str, Any]]:
        return await self.get("/cluster/ha/status/current")

    async def ha_resources(self) -> list[dict[str, Any]]:
        return await self.get("/cluster/ha/resources")

    async def resources(self, type_: str | None = None) -> list[dict[str, Any]]:
        return await self.get("/cluster/resources", **({"type": type_} if type_ else {}))

    async def replication_jobs(self) -> list[dict[str, Any]]:
        return await self.get("/cluster/replication")

    async def node_replication(self, node: str) -> list[dict[str, Any]]:
        return await self.get(f"/nodes/{node}/replication")

    async def node_status(self, node: str) -> dict[str, Any]:
        return await self.get(f"/nodes/{node}/status")

    # ---- the one write we offer (live migration of a VM); guarded by the caller

    async def migrate_vm(self, node: str, vmid: int, target: str) -> str:
        return await self.post(f"/nodes/{node}/qemu/{vmid}/migrate", target=target, online=1)
