"""Binary sensors: quorum, per-node online, replication healthy, API reachable."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClusterGuardConfigEntry
from .coordinator import ClusterGuardCoordinator
from .entity import ClusterEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ClusterGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        QuorateSensor(coord),
        ReplicationHealthySensor(coord),
        ReachableSensor(coord),
    ]
    known: set[str] = set()

    @callback
    def _add_nodes() -> None:
        new = [NodeOnlineSensor(coord, n) for n in coord.data.nodes if n not in known]
        known.update(n.node for n in new)
        if new:
            async_add_entities(new)

    async_add_entities(entities)
    _add_nodes()
    entry.async_on_unload(coord.async_add_listener(_add_nodes))


class QuorateSensor(ClusterEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ClusterGuardCoordinator) -> None:
        super().__init__(coordinator, "quorum_lost")

    @property
    def is_on(self) -> bool | None:
        q = self.coordinator.data.quorate
        return None if q is None else not q

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        d = self.coordinator.data
        return {"nodes_online": d.nodes_online, "nodes_total": d.nodes_total}


class NodeOnlineSensor(ClusterEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: ClusterGuardCoordinator, node: str) -> None:
        super().__init__(coordinator, "node_online")
        self.node = node
        self._attr_unique_id = f"{coordinator.entry.entry_id}_node_{node}"
        self._attr_translation_placeholders = {"node": node}

    @property
    def is_on(self) -> bool | None:
        n = self.coordinator.data.nodes.get(self.node)
        return None if n is None else n.online

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        n = self.coordinator.data.nodes.get(self.node)
        vms = [
            f"{v.name} ({v.vmid})"
            for v in self.coordinator.data.vms.values()
            if v.node == self.node and v.status == "running"
        ]
        return {"ip": n.ip if n else None, "running_vms": ", ".join(vms) or None}


class ReplicationHealthySensor(ClusterEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ClusterGuardCoordinator) -> None:
        super().__init__(coordinator, "replication_problem")

    @property
    def is_on(self) -> bool | None:
        d = self.coordinator.data
        jobs = [r for r in d.replication.values() if self.coordinator.ha_vmid in (None, r.guest)]
        if not jobs:
            return None
        return any(
            r.fail_count > 0 or r.age_minutes is None or r.age_minutes > self.coordinator.stale_minutes for r in jobs
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {
            r.job_id: (
                f"error x{r.fail_count}: {r.error}"
                if r.fail_count
                else "never"
                if r.age_minutes is None
                else f"{r.age_minutes:.0f} min ago"
            )
            for r in self.coordinator.data.replication.values()
        }


class ReachableSensor(ClusterEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: ClusterGuardCoordinator) -> None:
        super().__init__(coordinator, "api_reachable")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.reachable
