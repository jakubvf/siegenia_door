"""The SIEGENIA Door integration."""

from __future__ import annotations

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SiegeniaClient
from .const import CONF_USE_TLS, DEFAULT_PORT, DEFAULT_USE_TLS
from .coordinator import SiegeniaConfigEntry, SiegeniaCoordinator

PLATFORMS: list[Platform] = [Platform.LOCK]


async def async_setup_entry(hass: HomeAssistant, entry: SiegeniaConfigEntry) -> bool:
    """Set up SIEGENIA Door from a config entry."""
    # The device presents a self-signed certificate. Home Assistant caches one
    # session per verification setting, so this shares the non-verifying pool
    # rather than building (and having to clean up) a session per entry.
    session = async_get_clientsession(hass, verify_ssl=False)

    client = SiegeniaClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        use_tls=entry.data.get(CONF_USE_TLS, DEFAULT_USE_TLS),
    )

    coordinator = SiegeniaCoordinator(hass, entry, client)
    client.set_callbacks(
        on_push=coordinator.handle_push,
        on_availability=coordinator.handle_availability,
    )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SiegeniaConfigEntry) -> bool:
    """Unload a config entry, closing the device session."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
