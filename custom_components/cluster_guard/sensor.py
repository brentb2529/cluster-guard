"""Sensors: nodes online, where the HA VM runs, oldest copy age, HA resource state."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClusterGuardConfigEntry
from .coordinator import ClusterGuardCoordinator
from .entity import ClusterEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ClusterGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    async_add_entities([NodesOnlineSensor(coord), HaVmNodeSensor(coord), CopyAgeSensor(coord), HaStateSensor(coord)])


class NodesOnlineSensor(ClusterEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClusterGuardCoordinator) -> None:
        super().__init__(coordinator, "nodes_online")

    @property
    def native_value(self) -> int:
        return self.coordinator.data.nodes_online

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        d = self.coordinator.data
        return {
            "total": d.nodes_total,
            "offline": [n.name for n in d.nodes.values() if not n.online],
            "cluster": d.name,
        }


class HaVmNodeSensor(ClusterEntity, SensorEntity):
    def __init__(self, coordinator: ClusterGuardCoordinator) -> None:
        super().__init__(coordinator, "ha_vm_node")

    @property
    def native_value(self) -> str | None:
        vmid = self.coordinator.ha_vmid
        vm = self.coordinator.data.vms.get(vmid) if vmid else None
        return vm.node if vm else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        vmid = self.coordinator.ha_vmid
        d = self.coordinator.data
        vm = d.vms.get(vmid) if vmid else None
        return {
            "vmid": vmid,
            "status": vm.status if vm else None,
            "ha_state": vm.ha_state if vm else None,
            "copies_on": sorted(r.target for r in d.replication.values() if vmid and r.guest == vmid),
        }


class CopyAgeSensor(ClusterEntity, SensorEntity):
    """Age of the oldest fresh copy of the HA VM on another node, in minutes."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: ClusterGuardCoordinator) -> None:
        super().__init__(coordinator, "copy_age")

    @property
    def native_value(self) -> float | None:
        vmid = self.coordinator.ha_vmid
        ages = [
            r.age_minutes
            for r in self.coordinator.data.replication.values()
            if vmid in (None, r.guest) and r.age_minutes is not None
        ]
        return max(ages) if ages else None


class HaStateSensor(ClusterEntity, SensorEntity):
    def __init__(self, coordinator: ClusterGuardCoordinator) -> None:
        super().__init__(coordinator, "ha_resource_state")

    @property
    def native_value(self) -> str | None:
        vmid = self.coordinator.ha_vmid
        for r in self.coordinator.data.ha.values():
            if vmid is None or r.vmid == vmid:
                return r.state
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {r.sid: f"{r.state} on {r.node}" for r in self.coordinator.data.ha.values()}
