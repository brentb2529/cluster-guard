"""Shared entity base: one device per cluster."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ClusterGuardCoordinator


class ClusterEntity(CoordinatorEntity[ClusterGuardCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ClusterGuardCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=f"Cluster {coordinator.data.name or coordinator.client.host}",
            manufacturer="Proxmox",
            model="Virtual Environment cluster",
            sw_version=coordinator.data.version,
            configuration_url=f"https://{coordinator.client.host}:8006/",
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None
