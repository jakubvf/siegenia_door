"""Config flow for the SIEGENIA Door integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    SiegeniaAuthError,
    SiegeniaClient,
    SiegeniaError,
    SiegeniaSessionOccupied,
)
from .const import (
    CONF_USE_TLS,
    DEFAULT_PORT,
    DEFAULT_USE_TLS,
    DEVICE_TYPE_ACS,
    DEVICE_TYPES,
    DOMAIN,
    PARAM_MAC,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class WrongDeviceType(Exception):
    """The host is a SIEGENIA device, but not an automatic door."""

    def __init__(self, device_type: int | None) -> None:
        """Record the reported device family."""
        super().__init__(device_type)
        self.name = DEVICE_TYPES.get(device_type, f"type {device_type}")


class SiegeniaDoorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-facing setup of a SIEGENIA door."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect connection details and verify them against the device."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await self._async_validate(user_input)
            except WrongDeviceType as err:
                errors["base"] = "wrong_device_type"
                placeholders["device_type"] = err.name
            except SiegeniaSessionOccupied:
                errors["base"] = "session_occupied"
            except SiegeniaAuthError:
                errors["base"] = "invalid_auth"
            except SiegeniaError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error setting up a SIEGENIA door")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured(updates=dict(user_input))
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Begin re-authentication after the device rejected the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect fresh credentials for an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            candidate = {**entry.data, **user_input}
            try:
                await self._async_validate(candidate)
            except SiegeniaSessionOccupied:
                errors["base"] = "session_occupied"
            except SiegeniaAuthError:
                errors["base"] = "invalid_auth"
            except (SiegeniaError, WrongDeviceType):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reauthenticating a SIEGENIA door")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data=candidate)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_SCHEMA, {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            errors=errors,
            description_placeholders={CONF_HOST: entry.data[CONF_HOST]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user correct the host, port or credentials of an entry."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                info = await self._async_validate(user_input)
            except WrongDeviceType as err:
                errors["base"] = "wrong_device_type"
                placeholders["device_type"] = err.name
            except SiegeniaSessionOccupied:
                errors["base"] = "session_occupied"
            except SiegeniaAuthError:
                errors["base"] = "invalid_auth"
            except SiegeniaError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reconfiguring a SIEGENIA door")
                errors["base"] = "unknown"
            else:
                # Keep the entry pointed at the same physical door.
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or entry.data
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Confirm the host is a door we can log in to, and identify it.

        `getDevice` answers before authentication, so the device family is
        checked first: pointing the integration at a ventilation unit or a window
        drive fails with a useful message instead of a credentials error.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        client = SiegeniaClient(
            session,
            user_input[CONF_HOST],
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
            port=user_input.get(CONF_PORT, DEFAULT_PORT),
            use_tls=user_input.get(CONF_USE_TLS, DEFAULT_USE_TLS),
        )

        device = await client.async_probe()
        if (device_type := device.get("type")) != DEVICE_TYPE_ACS:
            raise WrongDeviceType(device_type)

        await client.async_start()
        try:
            params = await client.async_get_device_params()
        finally:
            await client.async_stop()

        serial = device.get("serialnr")
        mac = params.get(PARAM_MAC)
        unique_id = serial or (format_mac(mac) if mac else None)
        if unique_id is None:
            # Nothing stable to key on; fall back to the address the user gave.
            unique_id = (
                f"{user_input[CONF_HOST]}:{user_input.get(CONF_PORT, DEFAULT_PORT)}"
            )

        title = (
            device.get("systemname")
            or params.get("systemname")
            or f"SIEGENIA Door ({user_input[CONF_HOST]})"
        )

        return {"unique_id": unique_id, "title": title}
