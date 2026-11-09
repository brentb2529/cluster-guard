"""Shared fixtures: a scriptable fake Proxmox API behind the real integration."""

from __future__ import annotations

import time
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
        self.replication = {
            "100-0": {"target": "pve2", "last_sync": now - 60, "fail_count": 0, "error": None},
            "100-1": {"target": "pve3", "last_sync": now - 60, "fail_count": 0, "error": None},
        }
        self.calls: list[str] = []
        self.fail_all: Exception | None = None

    def _check(self):
        if self.fail_all:
            raise self.fail_all

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
        return [
            {"type": "quorum", "quorate": int(self.quorate), "status": "OK"},
            {
                "type": "service",
                "sid": "vm:100",
                "node": self.vm_node,
                "state": self.ha_state,
                "request_state": "started",
            },
        ]

    async def ha_resources(self) -> list[dict[str, Any]]:
        return [{"sid": "vm:100", "state": "started", "type": "vm"}]

    async def resources(self, type_: str | None = None) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "type": "qemu",
                "vmid": 100,
                "name": "homeassistant",
                "node": self.vm_node,
                "status": "running",
                "hastate": self.ha_state,
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


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProxmox:
    f = FakeProxmox()

    class _Client:
        def __init__(self, *a, **k):
            self.host = a[1] if len(a) > 1 else "pve1.lab"

        def __getattr__(self, name):
            return getattr(f, name)

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
