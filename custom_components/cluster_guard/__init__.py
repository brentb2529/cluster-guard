"""Cluster Guard: watches the Proxmox cluster Home Assistant runs on and raises Repairs."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import ClusterGuardCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
SERVICE_MIGRATE = "migrate"
type ClusterGuardConfigEntry = ConfigEntry[ClusterGuardCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ClusterGuardConfigEntry) -> bool:
    coordinator = ClusterGuardCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))

    async def _migrate(call: ServiceCall) -> None:
        try:
            await coordinator.async_migrate(int(call.data["vmid"]), call.data["target"])
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    if not hass.services.has_service(DOMAIN, SERVICE_MIGRATE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_MIGRATE,
            _migrate,
            schema=vol.Schema({vol.Required("vmid"): cv.positive_int, vol.Required("target"): cv.string}),
        )
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ClusterGuardConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok and len(hass.config_entries.async_loaded_entries(DOMAIN)) <= 1:
        hass.services.async_remove(DOMAIN, SERVICE_MIGRATE)
    return ok
