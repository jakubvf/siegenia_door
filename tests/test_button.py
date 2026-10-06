"""Tests for the SIEGENIA door's buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import FakeDevice, FakeWebSocket
from .const import RESET_BUTTON_ENTITY_ID, SERIAL


async def async_press_reset(hass: HomeAssistant) -> None:
    """Press the reset button and wait for it to settle."""
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: RESET_BUTTON_ENTITY_ID},
        blocking=True,
    )


async def test_reset_button_is_a_config_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """The button is a configuration control, keyed to the door's serial."""
    entry = entity_registry.async_get(RESET_BUTTON_ENTITY_ID)

    assert entry is not None
    assert entry.unique_id == f"{SERIAL}_reset_security_block"
    assert entry.entity_category is EntityCategory.CONFIG


async def test_press_sends_vdsreset_and_nothing_else(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """Pressing sends exactly the app's security-block reset.

    `setAdminDeviceParams` also carries `reset`, which factory-resets the whole
    system, so the payload is checked in full rather than for the presence of
    `vdsreset` alone.
    """
    await async_press_reset(hass)

    requests = device.requests("setAdminDeviceParams")
    assert len(requests) == 1
    assert requests[0]["params"] == {"vdsreset": True}
    assert device.admin_params == {"vdsreset": True}


async def test_press_refreshes_the_door_state(
    hass: HomeAssistant, device: FakeDevice, init_integration: MockConfigEntry
) -> None:
    """The door is polled after the reset so cleared warnings show at once."""
    polls_before = len(device.requests("getDeviceParams"))

    await async_press_reset(hass)

    assert len(device.requests("getDeviceParams")) > polls_before


@pytest.mark.parametrize("status", ["error", "not_authenticated"])
async def test_press_failure_raises(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
    status: str,
) -> None:
    """A refused reset surfaces as an error instead of passing silently."""

    def refuse(ws: FakeWebSocket, request: dict[str, Any]) -> None:
        ws.push_json({"data": {}, "id": request["id"], "status": status})

    device.handlers["setAdminDeviceParams"] = refuse

    with pytest.raises(HomeAssistantError, match="security block"):
        await async_press_reset(hass)
