"""Lock platform for the SIEGENIA Door integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SiegeniaError
from .const import (
    DOMAIN,
    OPENCLOSE_OPEN,
    PARAM_DAYMODE,
    PARAM_MAC,
    PARAM_OPENCLOSE,
    PARAM_STATE,
    STATE_PREFIX_CLOSED,
    STATE_PREFIX_OPEN,
)
from .coordinator import SiegeniaConfigEntry, SiegeniaCoordinator

_LOGGER = logging.getLogger(__name__)

# How long an optimistic transition is shown before giving up and falling back
# to whatever the device reports. The physical cycle takes 5-10 seconds; doors
# without a position sensor never confirm at all, so this must always expire.
TRANSITION_TIMEOUT = 30.0

# Opening gets a shorter window. The `open` command releases the latch -- it does
# not swing the leaf -- so unless somebody actually pushes the door, `state`
# never reaches OPEN and the transition can only ever end by expiring. Holding
# `opening` for the full thirty seconds after a latch release that already
# finished would just be wrong for that whole time.
OPEN_TRANSITION_TIMEOUT = 10.0

# Lock, unlock and open all drive the same physical mechanism, so commands are
# issued one at a time rather than racing each other at the device.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SiegeniaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the door lock from a config entry."""
    async_add_entities([SiegeniaDoorLock(entry.runtime_data)])


class SiegeniaDoorLock(CoordinatorEntity[SiegeniaCoordinator], LockEntity):
    """A SIEGENIA automatic door, exposed as a lock."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator: SiegeniaCoordinator) -> None:
        """Initialise the entity from already-fetched coordinator data."""
        super().__init__(coordinator)

        serial = coordinator.device.get("serialnr")
        mac = (coordinator.data or {}).get(PARAM_MAC)
        self._attr_unique_id = serial or format_mac(mac)

        self._target_daymode: bool | None = None
        self._opening = False
        self._transition_timer: CALLBACK_TYPE | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device this entity belongs to."""
        return self.coordinator.device_info

    @property
    def supports_daymode(self) -> bool:
        """Return whether this door exposes day/night mode.

        Firmware 1.11 and later drop the `daymode` parameter entirely. Those
        doors can still be triggered open, but cannot be locked or unlocked
        through this interface.
        """
        return PARAM_DAYMODE in (self.coordinator.data or {})

    @property
    def is_locked(self) -> bool | None:
        """Return whether the door is locked, or None if it cannot be known.

        Day mode means the door is released for normal use, so `daymode` true is
        unlocked. Doors that do not report it have no knowable lock state.
        """
        if self._in_transition and self._target_daymode is not None:
            return not self._target_daymode

        daymode = (self.coordinator.data or {}).get(PARAM_DAYMODE)
        if daymode is None:
            return None
        return not daymode

    @property
    def is_locking(self) -> bool:
        """Return whether a lock command is still settling."""
        return self._in_transition and self._target_daymode is False

    @property
    def is_unlocking(self) -> bool:
        """Return whether an unlock command is still settling."""
        return self._in_transition and self._target_daymode is True

    @property
    def is_opening(self) -> bool:
        """Return whether an open command is still settling."""
        return self._in_transition and self._opening

    @property
    def is_open(self) -> bool | None:
        """Return whether the door leaf is open.

        The device reports leaf position and bolt status in one field, so a shut
        door reads `CLOSED` when bolted and `CLOSED_NOT_LOCKED` when it is not.
        Both mean the leaf is shut, hence the prefix test rather than equality.

        `UNDEFINED` is a legitimate permanent value on doors with no sash sensor
        fitted -- the official SIEGENIA app shows no position for those either --
        so it maps to unknown rather than to closed.
        """
        state = (self.coordinator.data or {}).get(PARAM_STATE)
        if not isinstance(state, str):
            return None
        if state.startswith(STATE_PREFIX_OPEN):
            return True
        if state.startswith(STATE_PREFIX_CLOSED):
            return False
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw device state.

        `is_open` collapses the device's combined position/bolt enum into a
        boolean, which loses the difference between a door that is merely shut
        and one that is shut and bolted. Automations that care about that
        distinction need the original value.
        """
        return {"door_state": (self.coordinator.data or {}).get(PARAM_STATE)}

    @property
    def _in_transition(self) -> bool:
        """Return whether an optimistic transition is still in effect."""
        return self._transition_timer is not None

    async def async_lock(self, **kwargs: Any) -> None:
        """Engage night mode."""
        await self._async_set_daymode(False)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Engage day mode."""
        await self._async_set_daymode(True)

    async def async_open(self, **kwargs: Any) -> None:
        """Release the door latch.

        This actuates the lock only; the leaf does not swing on its own. That
        matches what Home Assistant's `open` means for a lock -- unlatch, as a
        door buzzer does -- so somebody still has to push the door.
        """
        self._opening = True
        self._target_daymode = None
        self._begin_transition(OPEN_TRANSITION_TIMEOUT)

        try:
            await self.coordinator.client.async_set_device_params(
                {PARAM_OPENCLOSE: OPENCLOSE_OPEN}
            )
        except SiegeniaError as err:
            self._end_transition()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="open_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _async_set_daymode(self, enabled: bool) -> None:
        """Switch day/night mode, if this door supports it."""
        if not self.supports_daymode:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="daymode_unsupported",
            )

        self._opening = False
        self._target_daymode = enabled
        self._begin_transition(TRANSITION_TIMEOUT)

        try:
            await self.coordinator.client.async_set_device_params(
                {PARAM_DAYMODE: enabled}
            )
        except SiegeniaError as err:
            self._end_transition()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="lock_failed" if not enabled else "unlock_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic state once the device confirms the change."""
        if self._in_transition and self._device_reached_target():
            self._reset_transition()

        super()._handle_coordinator_update()

    def _device_reached_target(self) -> bool:
        """Return whether the device now reports the state we asked for."""
        data = self.coordinator.data or {}

        if self._target_daymode is not None:
            return data.get(PARAM_DAYMODE) is self._target_daymode
        if self._opening:
            return self.is_open is True
        return False

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending transition timer before going away."""
        self._reset_transition()
        await super().async_will_remove_from_hass()

    @callback
    def _begin_transition(self, timeout: float) -> None:
        """Show the requested state optimistically and poll faster for a while.

        A timer backs the optimistic window rather than a deadline comparison:
        when nothing further arrives from the device, only a scheduled callback
        can write the entity back out of its `locking`/`opening` state.

        Only the timer is replaced here. The caller has already recorded which
        state it is driving towards, and clearing that would leave the entity
        showing no transition at all.
        """
        self._cancel_timer()
        self._transition_timer = async_call_later(
            self.hass, timeout, self._async_transition_timeout
        )
        self.async_write_ha_state()
        self.coordinator.async_expect_change()

    @callback
    def _async_transition_timeout(self, _now: Any) -> None:
        """Give up waiting for the device to confirm and show its real state."""
        self._transition_timer = None
        self._reset_transition()
        self.async_write_ha_state()

    @callback
    def _cancel_timer(self) -> None:
        """Cancel a pending transition timer, leaving the target intact."""
        if self._transition_timer is not None:
            self._transition_timer()
            self._transition_timer = None

    @callback
    def _reset_transition(self) -> None:
        """Drop back to reporting whatever the device says."""
        self._cancel_timer()
        self._opening = False
        self._target_daymode = None

    @callback
    def _end_transition(self) -> None:
        """Abandon an optimistic transition and publish the real state."""
        self._reset_transition()
        self.async_write_ha_state()
