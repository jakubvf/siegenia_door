"""Tests for setting up and tearing down the SIEGENIA Door integration."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.siegenia_door.const import DOMAIN

from .conftest import FakeClientSession, FakeDevice
from .const import DEVICE_MHS, LOCK_ENTITY_ID, SERIAL


async def test_setup_creates_the_lock_entity(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A successful setup logs in, polls once and registers the door."""
    assert init_integration.state is ConfigEntryState.LOADED
    assert device.commands[:3] == ["login", "getDevice", "getDeviceParams"]

    entity = entity_registry.async_get(LOCK_ENTITY_ID)
    assert entity is not None
    assert entity.unique_id == SERIAL

    registry_device = device_registry.async_get(entity.device_id)
    assert registry_device is not None
    assert registry_device.manufacturer == "SIEGENIA"
    assert registry_device.model == "ACS (variant 3)"
    assert registry_device.serial_number == SERIAL
    assert registry_device.sw_version == "1.11.1.23"


async def test_unload_closes_the_session(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """Unloading the entry closes the device session."""
    assert device.socket.closed is False

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert device.socket.closed is True

    state = hass.states.get(LOCK_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_setup_retries_when_the_device_is_unreachable(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A connection failure during setup leaves the entry retrying."""
    device.connect_error = aiohttp.ClientConnectionError("Connection refused")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.device_info(DEVICE_MHS)
async def test_setup_retries_for_an_unsupported_device(
    hass: HomeAssistant,
    fake_session: FakeClientSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A device that is not an ACS door never finishes setting up."""
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_triggers_reauth_when_credentials_are_rejected(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Rejected credentials put the entry in an error state and ask the user."""
    device.password = "no-longer-valid"
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id
