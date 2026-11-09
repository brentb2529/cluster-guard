"""Cluster Guard: a healthy cluster is quiet; each degraded state becomes one clear Repair."""

from __future__ import annotations

import time

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.cluster_guard.api import ProxmoxAuthError, ProxmoxConnectionError
from custom_components.cluster_guard.const import (
    DOMAIN,
    EVENT_FAILOVER,
    EVENT_NODE_DOWN,
    ISSUE_DEGRADED,
    ISSUE_HA_RESOURCE,
    ISSUE_NO_HA,
    ISSUE_NODE_DOWN,
    ISSUE_REPLICATION,
    ISSUE_UNREACHABLE,
    UNREACHABLE_POLLS,
)

from .conftest import FakeProxmox, setup_cg


def issues(hass: HomeAssistant) -> set[str]:
    return {i.issue_id for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN}


async def refresh(hass: HomeAssistant, coord) -> None:
    await coord.async_refresh()
    await hass.async_block_till_done()


async def test_healthy_cluster_is_quiet(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    await setup_cg(hass, entry)
    assert issues(hass) == set()
    assert hass.states.get("sensor.cluster_house_nodes_online").state == "3"
    assert hass.states.get("sensor.cluster_house_home_assistant_runs_on").state == "pve1"
    assert hass.states.get("binary_sensor.cluster_house_quorum_lost").state == "off"
    assert hass.states.get("binary_sensor.cluster_house_copy_problem").state == "off"
    assert float(hass.states.get("sensor.cluster_house_oldest_copy_age").state) < 2
    for n in ("pve1", "pve2", "pve3"):
        assert hass.states.get(f"binary_sensor.cluster_house_node_{n}").state == "on"


async def test_node_down_raises_repair_event_and_clears(
    hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox
) -> None:
    coord = await setup_cg(hass, entry)
    events = async_capture_events(hass, EVENT_NODE_DOWN)
    fake.nodes["pve3"] = False
    await refresh(hass, coord)
    assert f"{ISSUE_NODE_DOWN}_pve3" in issues(hass)
    assert ISSUE_DEGRADED not in issues(hass)  # one down of three is a warning, not an emergency
    assert len(events) == 1 and events[0].data["node"] == "pve3"
    assert hass.states.get("binary_sensor.cluster_house_node_pve3").state == "off"
    assert hass.states.get("sensor.cluster_house_nodes_online").state == "2"
    # a stale copy ON the dead node is not a separate problem: the node is the problem
    assert ISSUE_REPLICATION not in issues(hass)

    fake.nodes["pve3"] = True
    await refresh(hass, coord)
    assert issues(hass) == set()


async def test_two_nodes_down_is_degraded(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    coord = await setup_cg(hass, entry)
    fake.nodes["pve2"] = fake.nodes["pve3"] = False
    await refresh(hass, coord)
    assert ISSUE_DEGRADED in issues(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_DEGRADED).translation_placeholders == {
        "online": "1",
        "total": "3",
    }


async def test_failover_fires_event(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    coord = await setup_cg(hass, entry)
    events = async_capture_events(hass, EVENT_FAILOVER)
    fake.nodes["pve1"] = False
    fake.vm_node = "pve2"
    await refresh(hass, coord)
    assert len(events) == 1
    assert events[0].data == {"resource": "vm:100", "from": "pve1", "to": "pve2"}
    assert hass.states.get("sensor.cluster_house_home_assistant_runs_on").state == "pve2"


async def test_stale_and_failing_replication(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    coord = await setup_cg(hass, entry)
    fake.replication["100-1"]["last_sync"] = time.time() - 3600
    fake.replication["100-0"]["fail_count"] = 3
    fake.replication["100-0"]["error"] = "ssh: connect to host pve2 port 22: No route to host"
    await refresh(hass, coord)
    assert ISSUE_REPLICATION in issues(hass)
    ph = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REPLICATION).translation_placeholders
    assert "to pve2: failed 3 times" in ph["jobs"]
    assert "to pve3: last copy 60 minutes ago" in ph["jobs"]
    assert hass.states.get("binary_sensor.cluster_house_copy_problem").state == "on"
    assert float(hass.states.get("sensor.cluster_house_oldest_copy_age").state) > 59


async def test_ha_resource_error(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    coord = await setup_cg(hass, entry)
    fake.ha_state = "error"
    await refresh(hass, coord)
    assert f"{ISSUE_HA_RESOURCE}_vm_100" in issues(hass)
    assert hass.states.get("sensor.cluster_house_failover_state").state == "error"


async def test_ha_vm_missing_copy_or_protection(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    coord = await setup_cg(hass, entry)
    del fake.replication["100-1"]  # no copy on pve3
    await refresh(hass, coord)
    assert ISSUE_NO_HA in issues(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NO_HA).translation_placeholders["missing"] == "pve3"


async def test_unreachable_after_several_polls_keeps_last_picture(
    hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox
) -> None:
    coord = await setup_cg(hass, entry)
    fake.fail_all = ProxmoxConnectionError("boom")
    for _ in range(UNREACHABLE_POLLS - 1):
        await refresh(hass, coord)
        assert ISSUE_UNREACHABLE not in issues(hass)
    await refresh(hass, coord)
    assert ISSUE_UNREACHABLE in issues(hass)
    assert hass.states.get("sensor.cluster_house_nodes_online").state == "3"  # last good picture, not unavailable
    fake.fail_all = None
    await refresh(hass, coord)
    assert ISSUE_UNREACHABLE not in issues(hass)


async def test_migrate_service(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    await setup_cg(hass, entry)
    await hass.services.async_call(DOMAIN, "migrate", {"vmid": 100, "target": "pve3"}, blocking=True)
    assert fake.calls == ["migrate 100 pve1->pve3"]
    assert hass.states.get("sensor.cluster_house_home_assistant_runs_on").state == "pve3"


async def test_config_flow(hass: HomeAssistant, fake: FakeProxmox) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": "pve1.lab", "port": 8006, "token_id": "nope", "token_secret": "s", "verify_ssl": False},
    )
    assert bad["errors"] == {"token_id": "token_format"}
    fake.fail_all = ProxmoxAuthError("401")
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "host": "pve1.lab",
            "port": 8006,
            "token_id": "homeassistant@pve!ha",
            "token_secret": "s",
            "verify_ssl": False,
        },
    )
    assert bad["errors"] == {"base": "invalid_auth"}
    fake.fail_all = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "host": "pve1.lab",
            "port": 8006,
            "token_id": "homeassistant@pve!ha",
            "token_secret": "s",
            "verify_ssl": False,
        },
    )
    assert result["step_id"] == "ha_vm"
    labels = [o["label"] for o in result["data_schema"].schema["ha_vmid"].config["options"]]
    assert any("homeassistant (VM 100 on pve1)" in label for label in labels)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"ha_vmid": "100"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cluster house"
    assert result["data"]["ha_vmid"] == 100


async def test_switches_to_another_node_when_the_configured_one_dies(
    hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox
) -> None:
    coord = await setup_cg(hass, entry)
    assert coord.client.host == "pve1.lab"
    fake.down_hosts = {"pve1.lab", "192.168.77.11"}  # the node we were configured against is gone
    fake.nodes["pve1"] = False
    await refresh(hass, coord)
    assert coord.client.host == "192.168.77.12"  # learned from the cluster, used without a gap
    assert ISSUE_UNREACHABLE not in issues(hass)
    assert f"{ISSUE_NODE_DOWN}_pve1" in issues(hass)
    assert hass.states.get("sensor.cluster_house_nodes_online").state == "2"
    fake.down_hosts = set()
    fake.nodes["pve1"] = True
    await refresh(hass, coord)
    assert issues(hass) == set()
