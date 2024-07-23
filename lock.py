from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from typing import cast
import json
import logging
from .siegenia_door import SiegeniaDoor
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback,) -> None:

    device = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(
        [SiegeniaDoorLock(device)]
    )

class SiegeniaDoorLock(LockEntity):
    _attr_name = "Lock Status"

    def __init__(self, door):
        self._door = door

        response = self._door.get_device_params()
        self._attr_unique_id = format_mac(response['data']['mac'])
        self._attr_is_unlocked = response['data']['daymode']
        self._attr_is_open = response['data']['state'] == 'OPEN'
        self._attr_is_locked = self._attr_is_unlocked and not self._attr_is_open

    def update(self):
        response = self._door.get_device_params()
        self._attr_is_unlocked = response['data']['daymode']
        self._attr_is_open = response['data']['state'] == 'OPEN'
        self._attr_is_locked = not self._attr_is_unlocked and not self._attr_is_open

    def lock(self, **kwargs):
        """Lock all or specified locks. A code to lock the lock with may optionally be specified."""
        self._door.set_day_mode(False)

    def unlock(self, **kwargs):
        """Unlock all or specified locks. A code to unlock the lock with may optionally be specified."""
        self._door.set_day_mode(True)

    def open(self, **kwargs):
        """Open (unlatch) all or specified locks. A code to open the lock with may optionally be specified."""
        self._door.open()

    @property
    def device_info(self):
        return self._door.get_device_info()

    @property
    def supported_features(self):
        return LockEntityFeature.OPEN
