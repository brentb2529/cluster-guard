"""Shared fixtures: a scriptable fake Proxmox API behind the real integration."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_guard.api import ProxmoxAuthError, ProxmoxConnectionError
from custom_components.cluster_guard.const import (
    CONF_HA_VMID,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN_ID,
    CONF_TOKEN_SECRET,
    CONF_VERIFY_SSL,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeProxmox:
    """Three nodes, one HA-managed VM 100 on pve1 replicated to pve2 and pve3 a minute ago."""

    def __init__(self) -> None:
        now = time.time()
        self.nodes = {"pve1": True, "pve2": True, "pve3": True}
        self.quorate = True
        self.vm_node = "pve1"
        self.ha_state = "started"
        self.ha_resource = True  # VM 100 is an HA resource
        self.replication = {
            "100-0": {"target": "pve2", "last_sync": now - 60, "fail_count": 0, "error": None},
            "100-1": {"target": "pve3", "last_sync": now - 60, "fail_count": 0, "error": None},
        }
        self.calls: list[str] = []
        self.fail_all: Exception | None = None
        self.down_hosts: set[str] = set()  # node addresses that refuse connections
        self.asked: list[str] = []  # which host each API call went to
        self.login_ok = {"root@pam": "pve-root-pw"}
        self.provisioned: list[str] = []

    def _check(self, host: str | None = None):
        if self.fail_all:
            raise self.fail_all
        if host is not None:
            self.asked.append(host)
            if host in self.down_hosts:
                raise ProxmoxConnectionError(f"{host}: connection refused")

    async def version(self) -> dict[str, Any]:
        self._check()
        return {"version": "9.2.1"}

    async def cluster_status(self) -> list[dict[str, Any]]:
        self._check()
        out = [{"type": "cluster", "name": "house", "quorate": int(self.quorate), "nodes": 3}]
        for i, (name, online) in enumerate(self.nodes.items(), 1):
            out.append({"type": "node", "name": name, "online": int(online), "ip": f"192.168.77.1{i}", "local": i == 1})
        return out

    async def ha_status(self) -> list[dict[str, Any]]:
        self._check()
        out = [{"type": "quorum", "quorate": int(self.quorate), "status": "OK"}]
        if self.ha_resource:
            out.append(
                {
                    "type": "service",
                    "sid": "vm:100",
                    "node": self.vm_node,
                    "state": self.ha_state,
                    "request_state": "started",
                }
            )
        return out

    async def ha_resources(self) -> list[dict[str, Any]]:
        return [{"sid": "vm:100", "state": "started", "type": "vm"}] if self.ha_resource else []

    async def resources(self, type_: str | None = None) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "type": "qemu",
                "vmid": 100,
                "name": "homeassistant",
                "node": self.vm_node,
                "status": "running",
                "hastate": self.ha_state if self.ha_resource else None,
            },
            {"type": "qemu", "vmid": 101, "name": "scratch", "node": "pve3", "status": "stopped"},
        ]

    async def node_replication(self, node: str) -> list[dict[str, Any]]:
        self._check()
        if node != self.vm_node:
            return []
        return [
            {"id": jid, "guest": 100, "source": node, "schedule": "*/1", **j} for jid, j in self.replication.items()
        ]

    async def node_status(self, node: str) -> dict[str, Any]:
        return {"uptime": 1234}

    async def migrate_vm(self, node: str, vmid: int, target: str) -> str:
        self.calls.append(f"migrate {vmid} {node}->{target}")
        self.vm_node = target
        return "UPID:fake"

    # setup-time and fix-time writes
    async def login(self, username: str, password: str) -> None:
        self.calls.append(f"login {username}")
        if self.login_ok.get(username) != password:
            raise ProxmoxAuthError("401 for /access/ticket")

    async def provision_token(self) -> tuple[str, str]:
        self.provisioned.append("role+user+acl+token")
        return "homeassistant@pve!cluster_guard", "made-secret"

    def use_token(self, token_id: str, token_secret: str) -> None:
        self.calls.append(f"use_token {token_id}")

    async def add_ha_resource(self, vmid: int) -> None:
        self.calls.append(f"add_ha vm:{vmid}")
        self.ha_resource = True
        self.ha_state = "started"

    async def add_replication(self, vmid: int, target: str, schedule: str = "*/1") -> str:
        job = f"{vmid}-{len(self.replication)}"
        self.replication[job] = {"target": target, "last_sync": time.time(), "fail_count": 0, "error": None}
        self.calls.append(f"add_repl {job} -> {target}")
        return job


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProxmox:
    f = FakeProxmox()

    class _Client:
        def __init__(self, *a, **k):
            self.host = a[1] if len(a) > 1 else "pve1.lab"

        def set_host(self, host: str) -> None:
            self.host = host

        def __getattr__(self, name):
            target = getattr(f, name)
            if not callable(target) or not asyncio.iscoroutinefunction(target):
                return target

            async def call(*args, **kwargs):
                f._check(self.host)
                return await target(*args, **kwargs)

            return call

    monkeypatch.setattr("custom_components.cluster_guard.coordinator.ProxmoxClient", _Client)
    monkeypatch.setattr("custom_components.cluster_guard.config_flow.ProxmoxClient", _Client)
    return f


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Cluster house",
        unique_id=f"{DOMAIN}_house",
        data={
            CONF_HOST: "pve1.lab",
            CONF_PORT: 8006,
            CONF_TOKEN_ID: "homeassistant@pve!ha",
            CONF_TOKEN_SECRET: "secret",
            CONF_VERIFY_SSL: False,
            CONF_HA_VMID: 100,
        },
        entry_id="cg_test",
    )


async def setup_cg(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data
