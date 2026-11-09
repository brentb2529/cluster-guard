"""Config flow: one node's address and an API token; then which VM is Home Assistant."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
)


async def _probe(hass, data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Return (cluster name, VMs) or raise."""
    client = ProxmoxClient(
        async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL]),
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_TOKEN_ID],
        data[CONF_TOKEN_SECRET],
        data[CONF_VERIFY_SSL],
    )
    status = await client.cluster_status()
    name = next((s.get("name") for s in status if s.get("type") == "cluster"), None) or data[CONF_HOST]
    vms = [v for v in await client.resources("vm") if v.get("type") == "qemu"]
    return name, vms


def _vm_options(vms: list[dict[str, Any]]) -> list[selector.SelectOptionDict]:
    out = [selector.SelectOptionDict(value="0", label="Home Assistant does not run in this cluster")]
    for v in sorted(vms, key=lambda x: int(x["vmid"])):
        out.append(
            selector.SelectOptionDict(
                value=str(v["vmid"]), label=f"{v.get('name', '')} (VM {v['vmid']} on {v.get('node', '?')})"
            )
        )
    return out


class ClusterGuardConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._vms: list[dict[str, Any]] = []
        self._name = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            user_input[CONF_TOKEN_ID] = user_input[CONF_TOKEN_ID].strip()
            if "!" not in user_input[CONF_TOKEN_ID] or "@" not in user_input[CONF_TOKEN_ID]:
                errors[CONF_TOKEN_ID] = "token_format"
            else:
                try:
                    self._name, self._vms = await _probe(self.hass, user_input)
                except ProxmoxAuthError:
                    errors["base"] = "invalid_auth"
                except ProxmoxConnectionError:
                    errors["base"] = "cannot_connect"
                except ProxmoxError:
                    errors["base"] = "unknown"
            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}_{self._name}")
                self._abort_if_unique_id_configured()
                self._data = user_input
                return await self.async_step_ha_vm()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                    vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
                    vol.Required(CONF_TOKEN_ID, default=(user_input or {}).get(CONF_TOKEN_ID, "")): str,
                    vol.Required(CONF_TOKEN_SECRET): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_ha_vm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_HA_VMID] = int(user_input[CONF_HA_VMID])
            return self.async_create_entry(title=f"Cluster {self._name}", data=self._data)
        return self.async_show_form(
            step_id="ha_vm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HA_VMID, default="0"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_vm_options(self._vms), mode=selector.SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            description_placeholders={"cluster": self._name},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ClusterGuardOptionsFlow()


class ClusterGuardOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_HA_VMID] = int(user_input[CONF_HA_VMID])
            return self.async_create_entry(title="", data=user_input)
        coord = self.config_entry.runtime_data
        vms = [
            {"vmid": v.vmid, "name": v.name, "node": v.node, "type": "qemu"}
            for v in (coord.data.vms.values() if coord.data else [])
        ]
        current = self.config_entry.options.get(CONF_HA_VMID, self.config_entry.data.get(CONF_HA_VMID, 0))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HA_VMID, default=str(current or 0)): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=_vm_options(vms), mode=selector.SelectSelectorMode.LIST)
                    ),
                    vol.Required(
                        CONF_STALE_MINUTES,
                        default=self.config_entry.options.get(CONF_STALE_MINUTES, DEFAULT_STALE_MINUTES),
                    ): selector.NumberSelector(selector.NumberSelectorConfig(min=2, max=1440, mode="box")),
                    vol.Required(
                        CONF_POLL_SECONDS,
                        default=self.config_entry.options.get(CONF_POLL_SECONDS, DEFAULT_POLL_SECONDS),
                    ): selector.NumberSelector(selector.NumberSelectorConfig(min=10, max=600, mode="box")),
                }
            ),
        )
