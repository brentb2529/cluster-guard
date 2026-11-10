"""Poll the cluster, derive its health, and turn problems into Repairs and events.

The one thing to keep in mind: Home Assistant usually runs *inside* this cluster. When the cluster
is in real trouble, HA is gone too and nothing here fires; what this integration catches is the
degraded states that precede that: a node died, the copies of the HA VM stopped being made, a
failover resource is stuck. Those are the states a person has days to fix instead of minutes.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ProxmoxAuthError, ProxmoxClient, ProxmoxConnectionError, ProxmoxError
from .const import (
    CONF_HA_VMID,
    CONF_HOST,
    CONF_POLL_SECONDS,
    CONF_PORT,
    CONF_STALE_MINUTES,
    CONF_TOKEN_ID,
    CONF_TOKEN_SECRET,
    CONF_VERIFY_SSL,
    DEFAULT_POLL_SECONDS,
    DEFAULT_PORT,
    DEFAULT_STALE_MINUTES,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    EVENT_FAILOVER,
    EVENT_NODE_BACK,
    EVENT_NODE_DOWN,
    ISSUE_DEGRADED,
    ISSUE_HA_RESOURCE,
    ISSUE_NO_HA,
    ISSUE_NODE_DOWN,
    ISSUE_QUORUM,
    ISSUE_REPLICATION,
    ISSUE_UNREACHABLE,
    NOTIFICATION_PREFIX,
    UNREACHABLE_POLLS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class NodeState:
    name: str
    online: bool
    ip: str | None = None
    local: bool = False


@dataclass
class ReplicationState:
    job_id: str  # "100-0"
    guest: int
    source: str
    target: str
    last_sync: float | None  # epoch seconds
    fail_count: int
    error: str | None
    schedule: str | None

    @property
    def age_minutes(self) -> float | None:
        if not self.last_sync:
            return None
        return (time.time() - self.last_sync) / 60


@dataclass
class HAResource:
    sid: str  # "vm:100"
    vmid: int | None
    node: str | None
    state: str  # started / stopped / error / fence / migrate / ...
    request_state: str | None


@dataclass
class VMState:
    vmid: int
    name: str
    node: str | None
    status: str  # running / stopped
    ha_state: str | None


@dataclass
class ClusterState:
    name: str = ""
    quorate: bool | None = None
    nodes: dict[str, NodeState] = field(default_factory=dict)
    vms: dict[int, VMState] = field(default_factory=dict)
    ha: dict[str, HAResource] = field(default_factory=dict)
    replication: dict[str, ReplicationState] = field(default_factory=dict)
    version: str | None = None
    reachable: bool = True

    @property
    def nodes_online(self) -> int:
        return sum(1 for n in self.nodes.values() if n.online)

    @property
    def nodes_total(self) -> int:
        return len(self.nodes)


class ClusterGuardCoordinator(DataUpdateCoordinator[ClusterState]):
    """Owns the API client, the derived state and the issues."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        poll = int(entry.options.get(CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS))
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=poll), config_entry=entry)
        data = entry.data
        self.client = ProxmoxClient(
            async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
            data[CONF_HOST],
            int(data.get(CONF_PORT, DEFAULT_PORT)),
            data[CONF_TOKEN_ID],
            data[CONF_TOKEN_SECRET],
            bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
        )
        self._failures = 0
        self._previous: ClusterState | None = None
        # Every node's address, learned from the cluster: if the node we talk to dies, we ask another.
        self._hosts: list[str] = [data[CONF_HOST]]

    # ------------------------------------------------------------ config

    @property
    def ha_vmid(self) -> int | None:
        v = self.entry.options.get(CONF_HA_VMID, self.entry.data.get(CONF_HA_VMID))
        return int(v) if v not in (None, "", 0, "0") else None

    @property
    def stale_minutes(self) -> float:
        return float(self.entry.options.get(CONF_STALE_MINUTES, DEFAULT_STALE_MINUTES))

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> ClusterState:
        try:
            state = await self._fetch_from_any_node()
        except ProxmoxAuthError as err:  # token gone (cluster rebuilt, token deleted): HA asks for the password again
            raise ConfigEntryAuthFailed(f"Proxmox rejected the API token: {err}") from err
        except (ProxmoxConnectionError, ProxmoxError) as err:
            self._failures += 1
            if self._failures >= UNREACHABLE_POLLS:
                self._set_issue(
                    ISSUE_UNREACHABLE,
                    True,
                    ir.IssueSeverity.WARNING,
                    {"host": self.client.host, "error": str(err)},
                )
            # Keep the last good picture on the entities rather than flapping to unavailable.
            if self.data is not None:
                self.data.reachable = False
                return self.data
            raise UpdateFailed(str(err)) from err
        self._failures = 0
        self._set_issue(ISSUE_UNREACHABLE, False, ir.IssueSeverity.WARNING, {})
        self._evaluate(state)
        self._previous = state
        return state

    async def _fetch_from_any_node(self) -> ClusterState:
        """Try the current node, then every other node we know; remember the one that answered."""
        last: Exception | None = None
        for host in [self.client.host] + [h for h in self._hosts if h != self.client.host]:
            if host != self.client.host:
                _LOGGER.info("Node %s is not answering; asking %s instead", self.client.host, host)
                self.client.set_host(host)
            try:
                st = await self._fetch()
            except ProxmoxConnectionError as err:
                last = err
                continue
            learned = [n.ip for n in st.nodes.values() if n.ip]
            if learned:
                self._hosts = [self.entry.data[CONF_HOST]] + [h for h in learned if h != self.entry.data[CONF_HOST]]
            return st
        raise last or ProxmoxConnectionError("no node answered")

    async def _fetch(self) -> ClusterState:
        st = ClusterState()
        for item in await self.client.cluster_status():
            if item.get("type") == "cluster":
                st.name = item.get("name", "")
                st.quorate = bool(item.get("quorate", 0))
            elif item.get("type") == "node":
                st.nodes[item["name"]] = NodeState(
                    item["name"], bool(item.get("online", 0)), item.get("ip"), bool(item.get("local", 0))
                )
        if st.quorate is None:  # single node, no cluster
            st.quorate = True
        for item in await self.client.ha_status():
            if item.get("type") == "service":
                sid = item["sid"]
                vmid = int(sid.split(":", 1)[1]) if ":" in sid and sid.split(":", 1)[1].isdigit() else None
                st.ha[sid] = HAResource(
                    sid, vmid, item.get("node"), str(item.get("state", "")), item.get("request_state")
                )
            elif item.get("type") == "quorum" and item.get("quorate") is not None:
                st.quorate = bool(item.get("quorate"))
        for item in await self.client.resources("vm"):
            if item.get("type") != "qemu":
                continue
            st.vms[int(item["vmid"])] = VMState(
                int(item["vmid"]),
                item.get("name", str(item["vmid"])),
                item.get("node"),
                item.get("status", ""),
                item.get("hastate"),
            )
        # Replication status lives per node; ask every online node.
        for node in st.nodes.values():
            if not node.online:
                continue
            try:
                jobs = await self.client.node_replication(node.name)
            except ProxmoxError:  # a node that just died answers nothing; the next poll catches up
                continue
            for j in jobs:
                st.replication[str(j["id"])] = ReplicationState(
                    str(j["id"]),
                    int(j.get("guest", 0)),
                    j.get("source", node.name),
                    j.get("target", ""),
                    j.get("last_sync") or None,
                    int(j.get("fail_count", 0) or 0),
                    j.get("error") or None,
                    j.get("schedule"),
                )
        with contextlib.suppress(ProxmoxError):
            st.version = (await self.client.version()).get("version")
        return st

    # ------------------------------------------------------------ judgement

    def _evaluate(self, st: ClusterState) -> None:
        prev = self._previous
        # While a node is down, its copies and jobs cannot be read; the node issue already says it all.
        degraded_now = st.nodes_online < st.nodes_total
        # Nodes
        for name, node in st.nodes.items():
            was_online = prev.nodes[name].online if prev and name in prev.nodes else True
            self._set_issue(
                f"{ISSUE_NODE_DOWN}_{name}",
                not node.online,
                ir.IssueSeverity.ERROR,
                {"node": name, "online": str(st.nodes_online), "total": str(st.nodes_total)},
                key=ISSUE_NODE_DOWN,
            )
            if was_online and not node.online:
                self.hass.bus.async_fire(EVENT_NODE_DOWN, {"node": name})
                persistent_notification.async_create(
                    self.hass,
                    f"Cluster node **{name}** stopped responding. {st.nodes_online} of {st.nodes_total} nodes are "
                    "up; Home Assistant keeps running, but there is one less place for it to go. Check the node's "
                    "power and network, then its screen.",
                    title="Cluster Guard: node down",
                    notification_id=f"{NOTIFICATION_PREFIX}node_{name}",
                )
            elif not was_online and node.online:
                self.hass.bus.async_fire(EVENT_NODE_BACK, {"node": name})
                persistent_notification.async_dismiss(self.hass, f"{NOTIFICATION_PREFIX}node_{name}")
        # Degraded: more than one node down, or down to a single node (no failover left)
        self._set_issue(
            ISSUE_DEGRADED,
            st.nodes_total >= 3 and st.nodes_online <= st.nodes_total - 2,
            ir.IssueSeverity.ERROR,
            {"online": str(st.nodes_online), "total": str(st.nodes_total)},
        )
        self._set_issue(ISSUE_QUORUM, st.quorate is False, ir.IssueSeverity.ERROR, {})
        # HA resources
        for sid, res in st.ha.items():
            # fence / recovery / migrate / starting are a failover in progress, not a fault
            bad = res.state == "error"
            self._set_issue(
                f"{ISSUE_HA_RESOURCE}_{sid.replace(':', '_')}",
                bad,
                ir.IssueSeverity.ERROR,
                {"resource": self._vm_label(st, res.vmid) or sid, "state": res.state, "node": res.node or "?"},
                key=ISSUE_HA_RESOURCE,
            )
            if prev and sid in prev.ha and prev.ha[sid].node and res.node and prev.ha[sid].node != res.node:
                self.hass.bus.async_fire(EVENT_FAILOVER, {"resource": sid, "from": prev.ha[sid].node, "to": res.node})
                persistent_notification.async_create(
                    self.hass,
                    f"**{self._vm_label(st, res.vmid) or sid}** moved from {prev.ha[sid].node} to {res.node}. "
                    "If nobody asked for this, a node failed: see the Repairs page.",
                    title="Cluster Guard: failover",
                    notification_id=f"{NOTIFICATION_PREFIX}failover_{sid}",
                )
        # Replication: the HA VM's copies on the other nodes must be fresh
        stale_jobs = [
            r
            for r in st.replication.values()
            if (
                r.fail_count > 0
                if degraded_now  # mid-failover, "never synced from here" is normal; real errors are not
                else (r.age_minutes is None or r.age_minutes > self.stale_minutes or r.fail_count > 0)
            )
            and (self.ha_vmid is None or r.guest == self.ha_vmid)
            and st.nodes.get(r.target, NodeState(r.target, True)).online  # a dead target is the node issue
        ]
        self._set_issue(
            ISSUE_REPLICATION,
            bool(stale_jobs),
            ir.IssueSeverity.WARNING,
            {
                "jobs": "\n".join(
                    f"- {self._vm_label(st, r.guest) or r.guest} to {r.target}: "
                    + (
                        f"failed {r.fail_count} times: {r.error}"
                        if r.fail_count
                        else "never synced"
                        if r.age_minutes is None
                        else f"last copy {r.age_minutes:.0f} minutes ago"
                    )
                    for r in stale_jobs
                ),
                "minutes": f"{self.stale_minutes:.0f}",
            },
        )
        # The HA VM itself should be an HA resource with copies on the other nodes
        if self.ha_vmid is not None:
            vm = st.vms.get(self.ha_vmid)
            protected = any(r.vmid == self.ha_vmid for r in st.ha.values())
            copies = {r.target for r in st.replication.values() if r.guest == self.ha_vmid}
            others = {n for n in st.nodes if vm is None or n != vm.node}
            self._set_issue(
                ISSUE_NO_HA,
                vm is not None
                and not degraded_now
                and (not protected or (st.nodes_total > 1 and not copies >= others)),
                ir.IssueSeverity.WARNING,
                {
                    "vm": self._vm_label(st, self.ha_vmid) or str(self.ha_vmid),
                    "missing": ", ".join(sorted(others - copies)) or "none",
                    "ha": "yes" if protected else "no",
                },
                fixable=True,
            )

    @staticmethod
    def _vm_label(st: ClusterState, vmid: int | None) -> str | None:
        if vmid is None:
            return None
        vm = st.vms.get(vmid)
        return f"{vm.name} (VM {vmid})" if vm else f"VM {vmid}"

    def _set_issue(
        self,
        issue_id: str,
        active: bool,
        severity: ir.IssueSeverity,
        placeholders: dict[str, str],
        *,
        key: str | None = None,
        fixable: bool = False,
    ) -> None:
        if active:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=fixable,
                severity=severity,
                translation_key=key or issue_id,
                translation_placeholders=placeholders,
                data=placeholders if fixable else None,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # ------------------------------------------------------------ actions

    async def async_protect_ha_vm(self) -> list[str]:
        """Make the HA VM an HA resource and give it a copy on every other node. Returns what was done."""
        st = self.data
        vmid = self.ha_vmid
        if st is None or vmid is None or vmid not in st.vms:
            raise ValueError("the Home Assistant VM is not known to the cluster")
        done: list[str] = []
        if not any(r.vmid == vmid for r in st.ha.values()):
            await self.client.add_ha_resource(vmid)
            done.append("HA resource added (started, max_restart 1, max_relocate 1)")
        here = st.vms[vmid].node
        covered = {r.target for r in st.replication.values() if r.guest == vmid}
        for node in sorted(st.nodes):
            if node != here and node not in covered:
                job = await self.client.add_replication(vmid, node)
                done.append(f"replication job {job} to {node}, every minute")
        await self.async_request_refresh()
        return done

    async def async_migrate(self, vmid: int, target: str) -> None:
        """Live-migrate a VM to another online node (the only write Cluster Guard performs)."""
        st = self.data
        vm = st.vms.get(vmid) if st else None
        if vm is None or vm.node is None:
            raise ValueError(f"VM {vmid} is not known to the cluster")
        if target not in st.nodes or not st.nodes[target].online:
            raise ValueError(f"{target} is not an online node")
        if target == vm.node:
            return
        await self.client.migrate_vm(vm.node, vmid, target)
        await self.async_request_refresh()
