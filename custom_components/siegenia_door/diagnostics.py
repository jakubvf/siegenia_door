"""Diagnostics support for the SIEGENIA Door integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import SiegeniaConfigEntry

# The device description and parameters both carry identifiers that should not
# end up in a public issue report.
TO_REDACT = {
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    "cn",
    "ip",
    "mac",
    "serialnr",
    "systemfloor",
    "systemlocation",
    "systemname",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SiegeniaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "connected": coordinator.client.connected,
        "last_update_success": coordinator.last_update_success,
        "poll_interval": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
        "device": async_redact_data(coordinator.device, TO_REDACT),
        "params": async_redact_data(coordinator.data or {}, TO_REDACT),
        # The capability that differs across firmware revisions, called out so
        # issue reports answer the first question without further digging.
        "supports_daymode": "daymode" in (coordinator.data or {}),
    }
