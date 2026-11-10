"""Repairs with a Fix button: protect the Home Assistant VM (HA resource + copies on every node)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .api import ProxmoxAuthError, ProxmoxError
from .const import DOMAIN
from .coordinator import ClusterGuardCoordinator


def _coordinator(hass: HomeAssistant) -> ClusterGuardCoordinator:
    return hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data


class ProtectVmRepairFlow(ConfirmRepairFlow):
    """Confirm -> add the HA resource and the missing replication jobs through the API."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        super().__init__()
        self._hass = hass
        self._data = data

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        coord = _coordinator(self._hass)
        if user_input is not None:
            try:
                done = await coord.async_protect_ha_vm()
            except ProxmoxAuthError:
                return self.async_abort(reason="no_permission", description_placeholders=self._data)
            except ProxmoxError as err:
                return self.async_abort(reason="failed", description_placeholders={**self._data, "error": str(err)})
            return self.async_create_entry(title="", data={"done": done})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}), description_placeholders=self._data)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    return ProtectVmRepairFlow(hass, dict(data or {}))
