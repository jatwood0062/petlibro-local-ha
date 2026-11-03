"""Config and options flow."""
from __future__ import annotations

import socket
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import CLOUD_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN, NAME

PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


def port_free(port: int) -> bool:
    """True when something can listen on the port (blocking; run in an executor)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


class PetlibroConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if await self.hass.async_add_executor_job(port_free, DEFAULT_PORT):
                return self.async_create_entry(title=NAME, data={}, options={CONF_PORT: DEFAULT_PORT})
            errors["base"] = "port_in_use"
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({}), errors=errors,
            description_placeholders={"hostname": CLOUD_HOST, "port": str(DEFAULT_PORT)})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PetlibroOptionsFlow()


class PetlibroOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options.get(CONF_PORT, DEFAULT_PORT)
        if user_input is not None:
            port = user_input[CONF_PORT]
            # the running broker holds its own port, so only a changed port can be probed
            if port != current and not await self.hass.async_add_executor_job(port_free, port):
                errors[CONF_PORT] = "port_in_use"
            else:
                return self.async_create_entry(data={CONF_PORT: port})
        return self.async_show_form(
            step_id="init", errors=errors,
            data_schema=vol.Schema({vol.Required(CONF_PORT, default=current): PORT}),
            description_placeholders={"hostname": CLOUD_HOST})
