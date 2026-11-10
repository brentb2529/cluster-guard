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
    fake.ha_state = "fence"  # Proxmox fencing the dead node: a failover in progress, not a fault
    await refresh(hass, coord)
    assert issues(hass) == {f"{ISSUE_NODE_DOWN}_pve1"}  # no resource error, no "not protected", no stale copy
    assert hass.states.get("sensor.cluster_house_failover_state").state == "fence"
    fake.ha_state = "started"
    fake.vm_node = "pve2"
    await refresh(hass, coord)
    assert len(events) == 1
    assert events[0].data == {"resource": "vm:100", "from": "pve1", "to": "pve2"}
    assert hass.states.get("sensor.cluster_house_home_assistant_runs_on").state == "pve2"
    assert issues(hass) == {f"{ISSUE_NODE_DOWN}_pve1"}


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


async def test_config_flow_with_root_password_provisions_its_own_token(hass: HomeAssistant, fake: FakeProxmox) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "pve1.lab", "port": 8006, "method": "password", "verify_ssl": False}
    )
    assert result["step_id"] == "password"
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "root@pam", "password": "wrong"}
    )
    assert bad["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "root@pam", "password": "pve-root-pw"}
    )
    assert result["step_id"] == "ha_vm"
    assert fake.provisioned == ["role+user+acl+token"]
    field = next(k for k in result["data_schema"].schema if str(k) == "ha_vmid")
    assert field.default() == "100"  # pre-selected by name: "homeassistant"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"ha_vmid": "100"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["token_id"] == "homeassistant@pve!cluster_guard"
    assert result["data"]["token_secret"] == "made-secret"
    assert "password" not in result["data"]


async def test_config_flow_with_existing_token(hass: HomeAssistant, fake: FakeProxmox) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "pve1.lab", "port": 8006, "method": "token", "verify_ssl": False}
    )
    assert result["step_id"] == "token"
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"token_id": "nope", "token_secret": "s"})
    assert bad["errors"] == {"token_id": "token_format"}
    fake.fail_all = ProxmoxAuthError("401")
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token_id": "homeassistant@pve!ha", "token_secret": "s"}
    )
    assert bad["errors"] == {"base": "invalid_auth"}
    fake.fail_all = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token_id": "homeassistant@pve!ha", "token_secret": "s"}
    )
    assert result["step_id"] == "ha_vm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"ha_vmid": "100"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cluster house"
    assert result["data"]["ha_vmid"] == 100


async def test_fix_button_protects_the_ha_vm(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeProxmox) -> None:
    from homeassistant.components.repairs import repairs_flow_manager  # noqa: PLC0415

    fake.ha_resource = False
    del fake.replication["100-1"]  # no copy on pve3
    coord = await setup_cg(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NO_HA)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders["ha"] == "no"
    mgr = repairs_flow_manager(hass)
    result = await mgr.async_init(DOMAIN, data={"issue_id": ISSUE_NO_HA})
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["missing"] == "pve3"
    result = await mgr.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.calls[-2:] == ["add_ha vm:100", "add_repl 100-1 -> pve3"]
    await refresh(hass, coord)
    assert ISSUE_NO_HA not in issues(hass)
