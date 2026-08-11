"""Tests for the SIEGENIA door lock entity."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.lock import (
    DATA_COMPONENT,
    DOMAIN as LOCK_DOMAIN,
    SERVICE_LOCK,
    SERVICE_OPEN,
    SERVICE_UNLOCK,
    LockEntity,
    LockState,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.siegenia_door.lock import (
    OPEN_TRANSITION_TIMEOUT,
    TRANSITION_TIMEOUT,
)

from .conftest import FakeDevice, async_idle
from .const import LOCK_ENTITY_ID, PARAMS_NEW_FIRMWARE, PARAMS_OLD_FIRMWARE


def get_lock(hass: HomeAssistant) -> LockEntity:
    """Return the lock entity object, for properties HA does not expose."""
    entity = hass.data[DATA_COMPONENT].get_entity(LOCK_ENTITY_ID)
    assert entity is not None
    return entity


async def async_call_lock_service(hass: HomeAssistant, service: str) -> None:
    """Call one of the lock services on the door and wait for it to settle."""
    await hass.services.async_call(
        LOCK_DOMAIN,
        service,
        {ATTR_ENTITY_ID: LOCK_ENTITY_ID},
        blocking=True,
    )


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": False})
async def test_daymode_false_is_locked(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Night mode means the door is locked."""
    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.LOCKED


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": True})
async def test_daymode_true_is_unlocked(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Day mode releases the door for normal use, so it reads as unlocked."""
    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.UNLOCKED


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "OPEN"})
async def test_open_state_wins_over_locked(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An open leaf outranks the lock state in Home Assistant's precedence."""
    assert get_lock(hass).is_open is True
    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.OPEN


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "UNDEFINED"})
async def test_undefined_state_is_unknown_not_closed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Doors with no sash sensor report an unknown position, never a closed one."""
    assert get_lock(hass).is_open is None


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "CLOSED"})
async def test_closed_state_is_not_open(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A reported CLOSED leaf is a definite `False`, not unknown."""
    assert get_lock(hass).is_open is False


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "CLOSED_NOT_LOCKED"})
async def test_closed_not_locked_is_closed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A shut but unbolted leaf is closed.

    Attested on firmware 1.9.1.23. The device reports leaf position and bolt
    status in one field, and an earlier version of this integration matched
    `CLOSED` exactly, so a shut door read as an unknown position instead.
    """
    assert get_lock(hass).is_open is False


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "CLOSED_SOMETHING_NEW"})
async def test_unseen_closed_variant_is_still_closed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An unfamiliar CLOSED_* value reads as closed rather than as unknown.

    The enum is undocumented and has already produced one value this integration
    had never seen, so position is derived from the prefix.
    """
    assert get_lock(hass).is_open is False


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "SOMETHING_ELSE"})
async def test_unrecognised_state_is_unknown(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A value matching neither prefix stays unknown rather than being guessed."""
    assert get_lock(hass).is_open is None


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "CLOSED_NOT_LOCKED"})
async def test_raw_device_state_is_exposed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The undecoded state is published, since `is_open` cannot express it."""
    attributes = hass.states.get(LOCK_ENTITY_ID).attributes
    assert attributes["door_state"] == "CLOSED_NOT_LOCKED"


@pytest.mark.device_params(PARAMS_NEW_FIRMWARE)
async def test_missing_daymode_has_no_lock_state(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Firmware without `daymode` yields an unknown lock state, not a crash."""
    lock = get_lock(hass)
    assert lock.supports_daymode is False
    assert lock.is_locked is None
    assert lock.is_open is None
    assert hass.states.get(LOCK_ENTITY_ID).state == STATE_UNKNOWN


@pytest.mark.device_params(PARAMS_NEW_FIRMWARE)
@pytest.mark.parametrize("service", [SERVICE_LOCK, SERVICE_UNLOCK])
async def test_missing_daymode_rejects_locking(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
    service: str,
) -> None:
    """Locking a door that has no day mode is a validation error, not a KeyError."""
    with pytest.raises(ServiceValidationError):
        await async_call_lock_service(hass, service)

    assert device.requests("setDeviceParams") == []


@pytest.mark.device_params(PARAMS_NEW_FIRMWARE)
async def test_missing_daymode_still_opens(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """The opener still works on firmware that dropped day mode."""
    await async_call_lock_service(hass, SERVICE_OPEN)

    assert device.requests("setDeviceParams")[0]["params"] == {"openclose": "OPEN"}


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": True})
async def test_lock_sends_daymode_false(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """Locking the door turns day mode off."""
    await async_call_lock_service(hass, SERVICE_LOCK)

    assert device.requests("setDeviceParams")[0]["params"] == {"daymode": False}


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": False})
async def test_unlock_sends_daymode_true(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """Unlocking the door turns day mode on."""
    await async_call_lock_service(hass, SERVICE_UNLOCK)

    assert device.requests("setDeviceParams")[0]["params"] == {"daymode": True}


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": False})
async def test_open_sends_openclose(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """Opening the door triggers the opener with a bare string."""
    await async_call_lock_service(hass, SERVICE_OPEN)

    assert device.requests("setDeviceParams")[0]["params"] == {"openclose": "OPEN"}


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": True})
async def test_lock_reports_locking_until_the_device_confirms(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """The entity shows the requested state until the device catches up."""
    await async_call_lock_service(hass, SERVICE_LOCK)

    # The device still reports day mode: a reply only means "accepted".
    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.LOCKING

    device.params["daymode"] = False
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.LOCKED


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": False})
async def test_unlock_reports_unlocking_until_the_device_confirms(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """Unlocking is optimistic in the same way locking is."""
    await async_call_lock_service(hass, SERVICE_UNLOCK)

    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.UNLOCKING

    device.params["daymode"] = True
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.UNLOCKED


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "CLOSED"})
async def test_open_reports_opening_until_the_device_confirms(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """Triggering the opener shows `opening` until the leaf actually moves."""
    await async_call_lock_service(hass, SERVICE_OPEN)

    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.OPENING

    device.params["state"] = "OPEN"
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.OPEN


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "state": "CLOSED"})
async def test_opening_uses_the_shorter_window(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`opening` clears on the open window, well before the lock window.

    Releasing the latch does not move the leaf, so `state` normally never
    reaches OPEN and the only way this transition ends is by expiring. Were it
    to use the lock timeout, the entity would claim to be opening for thirty
    seconds after an actuation that finished almost immediately.
    """
    elapsed = 0.0
    monkeypatch.setattr(
        "custom_components.siegenia_door.coordinator.monotonic", lambda: elapsed
    )

    lock = get_lock(hass)
    await async_call_lock_service(hass, SERVICE_OPEN)
    assert lock.is_opening is True

    while elapsed <= OPEN_TRANSITION_TIMEOUT:
        freezer.tick(timedelta(seconds=2))
        elapsed += 2
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert lock.is_opening is False
    assert elapsed < TRANSITION_TIMEOUT


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": True})
async def test_transition_expires_when_the_device_never_confirms(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A door that never confirms must not stay stuck showing `locking`.

    Asserts on the entity rather than the state machine. Winding the clock past
    the transition window also winds it past the response timeout of whichever
    poll happens to be in flight, so the coordinator reports the device as
    unavailable -- an artefact of jumping a frozen clock, not of the behaviour
    under test. What matters here is that the optimistic flag itself clears.
    """
    # The coordinator paces its polling with time.monotonic, which freezegun
    # does not patch; without this it would stay in post-command fast-poll mode.
    elapsed = 0.0
    monkeypatch.setattr(
        "custom_components.siegenia_door.coordinator.monotonic", lambda: elapsed
    )

    lock = get_lock(hass)
    await async_call_lock_service(hass, SERVICE_LOCK)
    assert lock.is_locking is True
    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.LOCKING

    # The device keeps reporting day mode: this door has no way to confirm.
    while elapsed <= TRANSITION_TIMEOUT:
        freezer.tick(timedelta(seconds=5))
        elapsed += 5
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert lock.is_locking is False
    assert hass.states.get(LOCK_ENTITY_ID).state != LockState.LOCKING


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": True})
async def test_transition_timer_expires_on_its_own(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The optimistic window closes on a timer, without any device traffic.

    Reaches into the entity because the timer handle is the only observable
    trace of the window while the optimistic state itself is broken.
    """
    lock = get_lock(hass)
    await async_call_lock_service(hass, SERVICE_LOCK)
    assert lock._transition_timer is not None  # noqa: SLF001

    freezer.tick(timedelta(seconds=TRANSITION_TIMEOUT + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert lock._transition_timer is None  # noqa: SLF001


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": True})
async def test_unload_cancels_a_pending_transition(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """An entity removed mid-transition leaves no timer behind."""
    lock = get_lock(hass)
    await async_call_lock_service(hass, SERVICE_LOCK)
    assert lock._transition_timer is not None  # noqa: SLF001

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert lock._transition_timer is None  # noqa: SLF001


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": False})
async def test_push_updates_the_state_without_polling(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """An unsolicited frame moves the entity without waiting for a poll."""
    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.LOCKED
    polls_before = len(device.requests("getDeviceParams"))

    device.push({"state": "OPEN"})
    await async_idle()
    await hass.async_block_till_done()

    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.OPEN
    assert len(device.requests("getDeviceParams")) == polls_before


@pytest.mark.device_params({**PARAMS_OLD_FIRMWARE, "daymode": False})
async def test_push_merges_into_the_cached_parameters(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """A partial push keeps the keys it does not mention."""
    device.push({"daymode": True})
    await async_idle()
    await hass.async_block_till_done()

    assert init_integration.runtime_data.data["mac"] == PARAMS_OLD_FIRMWARE["mac"]
    assert hass.states.get(LOCK_ENTITY_ID).state == LockState.UNLOCKED
