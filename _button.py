"""Platform for sensor integration."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from typing import cast
import websocket
import ssl
import json
import logging
from .siegenia_door import SiegeniaDoor
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback,) -> None:

    device = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(
        [SiegeniaDoorOpen(device)]
    )

class SiegeniaDoorOpen(ButtonEntity):
    _attr_name = "Open"

    @property
    def device_info(self):
        return self._door.get_device_info()

    def __init__(self, door):
        self._door = door

        response = self._door.get_device_params()
        self._attr_unique_id = format_mac(response['data']['mac'])

    def press(self) -> None:
        self._door.open()
