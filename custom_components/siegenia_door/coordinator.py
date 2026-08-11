"""State coordination for the SIEGENIA Door integration.

The device keeps a persistent WebSocket session open, and *may* push parameter
updates over it unsolicited. Whether ACS-family doors actually do so is not
confirmed, so this coordinator treats pushes as an optimisation layered on top
of polling rather than a replacement for it: pushes are applied immediately when
they arrive, and the poll interval relaxes while they keep coming.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SiegeniaAuthError,
    SiegeniaClient,
    SiegeniaConnectionError,
    SiegeniaError,
)
from .const import (
    DEVICE_TYPE_ACS,
    DEVICE_TYPES,
    DOMAIN,
    PARAM_MAC,
    POLL_INTERVAL_DEFAULT,
    POLL_INTERVAL_MOVING,
    POLL_INTERVAL_PUSH,
    PUSH_IDLE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

type SiegeniaConfigEntry = ConfigEntry[SiegeniaCoordinator]


class SiegeniaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the device session and publishes its parameters to entities."""

    config_entry: SiegeniaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SiegeniaConfigEntry,
        client: SiegeniaClient,
    ) -> None:
        """Initialise the coordinator with an unconnected client."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(seconds=POLL_INTERVAL_DEFAULT),
        )
        self.client = client
        self.device: dict[str, Any] = {}
        self._last_push: float | None = None
        self._fast_poll_until: float = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        """Describe the door to the device registry.

        Built once from the static `getDevice` response rather than fetched per
        access, since every read would otherwise be a network round trip.
        """
        serial = self.device.get("serialnr")
        identifier = serial or self.config_entry.unique_id or self.config_entry.entry_id

        info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            manufacturer="SIEGENIA",
            model=self.model,
            name=self.device.get("systemname") or self.config_entry.title,
            sw_version=self.device.get("softwareversion"),
            hw_version=self.device.get("hardwareversion"),
            serial_number=serial,
        )

        # The MAC lives in the parameters, not the device description.
        if mac := (self.data or {}).get(PARAM_MAC):
            info["connections"] = {("mac", format_mac(mac))}

        return info

    @property
    def model(self) -> str:
        """Return a human-readable model name for the device registry."""
        device_type = self.device.get("type")
        family = DEVICE_TYPES.get(device_type, f"Type {device_type}")
        if (variant := self.device.get("variant")) is not None:
            return f"{family} (variant {variant})"
        return family

    async def _async_setup(self) -> None:
        """Open the session and read the static device description.

        Runs once before the first refresh, and again on every reload.
        """
        try:
            await self.client.async_start()
            self.device = await self.client.async_get_device()
        except SiegeniaAuthError as err:
            await self.client.async_stop()
            raise ConfigEntryAuthFailed(str(err)) from err
        except SiegeniaError as err:
            await self.client.async_stop()
            raise ConfigEntryNotReady(str(err)) from err

        device_type = self.device.get("type")
        if device_type != DEVICE_TYPE_ACS:
            await self.client.async_stop()
            # Guarded in the config flow too, but firmware updates and restored
            # entries can both land here with something unexpected.
            raise ConfigEntryNotReady(
                f"Device at {self.config_entry.title} is a "
                f"{DEVICE_TYPES.get(device_type, f'type {device_type}')}, "
                "which this integration does not support"
            )

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the device parameters."""
        try:
            params = await self.client.async_get_device_params()
        except SiegeniaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SiegeniaError as err:
            raise UpdateFailed(str(err)) from err

        self._reschedule()
        return params

    async def async_shutdown(self) -> None:
        """Close the session when the entry unloads."""
        await super().async_shutdown()
        await self.client.async_stop()

    @callback
    def async_expect_change(self) -> None:
        """Poll rapidly for a while after issuing a command.

        A command reply only means the device accepted the instruction; the
        physical cycle takes several seconds, and reading state back immediately
        still returns the old value.
        """
        self._fast_poll_until = monotonic() + 30
        self._reschedule()

    @callback
    def handle_push(self, data: dict[str, Any]) -> None:
        """Apply an unsolicited parameter update.

        Pushes are partial deltas, so they are merged into the cached state.
        Replacing it wholesale would drop every key the delta omits.
        """
        self._last_push = monotonic()
        merged = dict(self.data or {})
        merged.update(data)
        self._reschedule()
        self.async_set_updated_data(merged)

    @callback
    def handle_availability(self, available: bool) -> None:
        """React to the session coming up or going down."""
        if available:
            self.config_entry.async_create_task(
                self.hass, self.async_request_refresh(), eager_start=True
            )
            return

        self.async_set_update_error(
            SiegeniaConnectionError("Lost the connection to the device")
        )

    @callback
    def _reschedule(self) -> None:
        """Adjust the poll interval to what is happening right now."""
        now = monotonic()

        if now < self._fast_poll_until:
            interval = POLL_INTERVAL_MOVING
        elif self._last_push is not None and now - self._last_push < PUSH_IDLE_TIMEOUT:
            interval = POLL_INTERVAL_PUSH
        else:
            interval = POLL_INTERVAL_DEFAULT

        if self.update_interval != (updated := timedelta(seconds=interval)):
            self.update_interval = updated
