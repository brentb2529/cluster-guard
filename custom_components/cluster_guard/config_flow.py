"""Config flow: a node's address, then either Proxmox's root password (used once, to create Cluster
Guard's own least-privilege user and token) or an existing API token; then which VM is Home Assistant."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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

_LOGGER = logging.getLogger(__name__)
METHOD_PASSWORD = "password"
METHOD_TOKEN = "token"
_HA_NAME_HINTS = ("homeassistant", "home-assistant", "home_assistant", "hassio", "haos", "hass")


def _likely_ha_vm(vms: list[dict[str, Any]]) -> str:
    """Pre-select the VM that is probably Home Assistant, by name."""
    for v in vms:
        name = str(v.get("name", "")).lower()
        if any(h in name for h in _HA_NAME_HINTS):
            return str(v["vmid"])
    return "0"


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
        self._conn: dict[str, Any] = {}
        self._token: tuple[str, str] | None = None
        self._vms: list[dict[str, Any]] = []
        self._name = ""

    def _client(self, token: tuple[str, str] | None = None) -> ProxmoxClient:
        return ProxmoxClient(
            async_get_clientsession(self.hass, verify_ssl=self._conn[CONF_VERIFY_SSL]),
            self._conn[CONF_HOST],
            self._conn[CONF_PORT],
            token[0] if token else None,
            token[1] if token else None,
            self._conn[CONF_VERIFY_SSL],
        )

    async def _finish_probe(self, client: ProxmoxClient) -> ConfigFlowResult:
        status = await client.cluster_status()
        self._name = next((s.get("name") for s in status if s.get("type") == "cluster"), None) or self._conn[CONF_HOST]
        self._vms = [v for v in await client.resources("vm") if v.get("type") == "qemu"]
        await self.async_set_unique_id(f"{DOMAIN}_{self._name}")
        self._abort_if_unique_id_configured()
        return await self.async_step_ha_vm()

    # ---- step 1: where, and how to authenticate

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._conn = {
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: int(user_input[CONF_PORT]),
                CONF_VERIFY_SSL: bool(user_input[CONF_VERIFY_SSL]),
            }
            if user_input["method"] == METHOD_TOKEN:
                return await self.async_step_token()
            return await self.async_step_password()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Required("method", default=METHOD_PASSWORD): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[METHOD_PASSWORD, METHOD_TOKEN],
                            translation_key="method",
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                }
            ),
        )

    # ---- step 2a: root password once -> own user, role and token

    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = self._client()
            try:
                await client.login(user_input["username"].strip(), user_input["password"])
                self._token = await client.provision_token()
                client.use_token(*self._token)
                return await self._finish_probe(client)
            except ProxmoxAuthError:
                errors["base"] = "invalid_auth"
            except ProxmoxConnectionError:
                errors["base"] = "cannot_connect"
            except ProxmoxError:
                _LOGGER.exception("Could not set up the Cluster Guard user on Proxmox")
                errors["base"] = "provision_failed"
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {
                    vol.Required("username", default="root@pam"): str,
                    vol.Required("password"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"host": self._conn[CONF_HOST]},
        )

    # ---- re-authentication: the token stopped working (cluster rebuilt, token deleted)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._conn = {
            CONF_HOST: entry_data[CONF_HOST],
            CONF_PORT: int(entry_data[CONF_PORT]),
            CONF_VERIFY_SSL: bool(entry_data[CONF_VERIFY_SSL]),
        }
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = self._client()
            try:
                await client.login(user_input["username"].strip(), user_input["password"])
                token = await client.provision_token()
                client.use_token(*token)
                await client.cluster_status()
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={CONF_TOKEN_ID: token[0], CONF_TOKEN_SECRET: token[1]}
                )
            except ProxmoxAuthError:
                errors["base"] = "invalid_auth"
            except ProxmoxConnectionError:
                errors["base"] = "cannot_connect"
            except ProxmoxError:
                _LOGGER.exception("Could not recreate the Cluster Guard user on Proxmox")
                errors["base"] = "provision_failed"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("username", default="root@pam"): str,
                    vol.Required("password"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"host": self._conn[CONF_HOST]},
        )

    # ---- step 2b: an existing token

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token_id = user_input[CONF_TOKEN_ID].strip()
            if "!" not in token_id or "@" not in token_id:
                errors[CONF_TOKEN_ID] = "token_format"
            else:
                self._token = (token_id, user_input[CONF_TOKEN_SECRET])
                try:
                    return await self._finish_probe(self._client(self._token))
                except ProxmoxAuthError:
                    errors["base"] = "invalid_auth"
                except ProxmoxConnectionError:
                    errors["base"] = "cannot_connect"
                except ProxmoxError:
                    errors["base"] = "unknown"
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN_ID, default=(user_input or {}).get(CONF_TOKEN_ID, "")): str,
                    vol.Required(CONF_TOKEN_SECRET): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    # ---- step 3: which VM is Home Assistant

    async def async_step_ha_vm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {
                **self._conn,
                CONF_TOKEN_ID: self._token[0],
                CONF_TOKEN_SECRET: self._token[1],
                CONF_HA_VMID: int(user_input[CONF_HA_VMID]),
            }
            return self.async_create_entry(title=f"Cluster {self._name}", data=data)
        return self.async_show_form(
            step_id="ha_vm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HA_VMID, default=_likely_ha_vm(self._vms)): selector.SelectSelector(
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
