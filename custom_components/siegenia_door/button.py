"""Button platform for the SIEGENIA Door integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SiegeniaError
from .const import DOMAIN, PARAM_MAC
from .coordinator import SiegeniaConfigEntry, SiegeniaCoordinator

# Pressing sends a single command; there is nothing to gain from overlapping it
# with anything else the door is doing.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SiegeniaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the door's buttons from a config entry."""
    async_add_entities([SiegeniaResetSecurityBlockButton(entry.runtime_data)])


class SiegeniaResetSecurityBlockButton(
    CoordinatorEntity[SiegeniaCoordinator], ButtonEntity
):
    """Unblock the door's readers after too many rejected attempts.

    The door stops accepting fingerprints once enough wrong ones have been
    presented, and stays that way until an administrator resets it -- which is
    what the official app's "Reset security block" does.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reset_security_block"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SiegeniaCoordinator) -> None:
        """Initialise the entity from already-fetched coordinator data."""
        super().__init__(coordinator)

        serial = coordinator.device.get("serialnr")
        mac = (coordinator.data or {}).get(PARAM_MAC)
        self._attr_unique_id = f"{serial or format_mac(mac)}_reset_security_block"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device this entity belongs to."""
        return self.coordinator.device_info

    async def async_press(self) -> None:
        """Reset the security block, then refresh so cleared warnings show."""
        try:
            await self.coordinator.client.async_reset_security_block()
        except SiegeniaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="reset_security_block_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        await self.coordinator.async_request_refresh()
