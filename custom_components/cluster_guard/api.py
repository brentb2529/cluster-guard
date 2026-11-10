"""A thin client for the Proxmox VE API (token or ticket auth, aiohttp).

Reads are what Cluster Guard lives on. The handful of writes exist so that nothing has to be done on
a command line: creating its own least-privilege user and token during setup, adding the HA resource
and replication jobs that protect the Home Assistant VM, and live-migrating a VM for maintenance.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=20)

CG_USER = "homeassistant@pve"
CG_TOKEN_NAME = "cluster_guard"
CG_ROLE = "ClusterGuard"
# Everything Cluster Guard does, and nothing it does not: read the cluster, protect and move VMs.
CG_PRIVS = [
    "Sys.Audit",
    "VM.Audit",
    "VM.GuestAgent.Audit",
    "Datastore.Audit",
    "Pool.Audit",
    "SDN.Audit",
    "Mapping.Audit",
    "VM.Migrate",
    "VM.Replicate",
    "Sys.Console",
    "Datastore.AllocateSpace",
]


class ProxmoxError(Exception):
    """Base error."""


class ProxmoxAuthError(ProxmoxError):
    """Credentials rejected or lacking privileges."""


class ProxmoxConnectionError(ProxmoxError):
    """Node unreachable."""


class ProxmoxClient:
    """Talks to one node's API; a cluster answers cluster-wide questions from any node."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token_id: str | None = None,
        token_secret: str | None = None,
        verify_ssl: bool = False,
    ) -> None:
        self._session = session
        self._port = port
        self._base = f"https://{host}:{port}/api2/json"
        self._headers: dict[str, str] = {}
        if token_id and token_secret:
            self._headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        self._ssl: bool | None = None if verify_ssl else False
        self.host = host

    def set_host(self, host: str) -> None:
        """Talk to another node (the cluster answers the same from any of them)."""
        self.host = host
        self._base = f"https://{host}:{self._port}/api2/json"

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

    async def put(self, path: str, **data: Any) -> Any:
        return await self._request("PUT", path, data=data or None)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ---- ticket login (setup only; the password is used once and never stored)

    async def login(self, username: str, password: str) -> None:
        data = await self.post("/access/ticket", username=username, password=password)
        if not data or "ticket" not in data:
            raise ProxmoxAuthError("login refused")
        self._headers = {
            "Cookie": f"PVEAuthCookie={data['ticket']}",
            "CSRFPreventionToken": data.get("CSRFPreventionToken", ""),
        }

    async def provision_token(self) -> tuple[str, str]:
        """Create (or refresh) Cluster Guard's own user, role and API token. Returns (token id, secret)."""
        roles = {r["roleid"] for r in await self.get("/access/roles")}
        privs = ",".join(CG_PRIVS)
        if CG_ROLE not in roles:
            await self.post("/access/roles", roleid=CG_ROLE, privs=privs)
        else:
            await self.put(f"/access/roles/{CG_ROLE}", privs=privs)
        users = {u["userid"] for u in await self.get("/access/users")}
        if CG_USER not in users:
            await self.post("/access/users", userid=CG_USER, comment="Home Assistant Cluster Guard")
        await self.put("/access/acl", path="/", roles=CG_ROLE, users=CG_USER)
        existing = {t["tokenid"] for t in await self.get(f"/access/users/{CG_USER}/token")}
        if CG_TOKEN_NAME in existing:
            await self.delete(f"/access/users/{CG_USER}/token/{CG_TOKEN_NAME}")
        made = await self.post(f"/access/users/{CG_USER}/token/{CG_TOKEN_NAME}", privsep=0)
        token_id = made.get("full-tokenid") or f"{CG_USER}!{CG_TOKEN_NAME}"
        return token_id, made["value"]

    def use_token(self, token_id: str, token_secret: str) -> None:
        self._headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}

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

    # ---- the writes that protect and move a VM

    async def add_ha_resource(self, vmid: int) -> None:
        await self.post("/cluster/ha/resources", sid=f"vm:{vmid}", state="started", max_restart=1, max_relocate=1)

    async def add_replication(self, vmid: int, target: str, schedule: str = "*/1") -> str:
        taken = {j["id"] for j in await self.replication_jobs()}
        n = 0
        while f"{vmid}-{n}" in taken:
            n += 1
        job_id = f"{vmid}-{n}"
        await self.post("/cluster/replication", id=job_id, type="local", target=target, schedule=schedule)
        return job_id

    async def migrate_vm(self, node: str, vmid: int, target: str) -> str:
        return await self.post(f"/nodes/{node}/qemu/{vmid}/migrate", target=target, online=1)
