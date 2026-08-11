"""Tests for the SIEGENIA Door config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.siegenia_door.const import DOMAIN

from .conftest import FakeClientSession, FakeDevice
from .const import (
    DEVICE_MHS,
    ENTRY_DATA,
    HOST,
    PASSWORD,
    SERIAL,
    SYSTEM_NAME,
    USER_INPUT,
    USERNAME,
)


async def test_user_flow_creates_entry(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
) -> None:
    """A reachable door with valid credentials becomes a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == SYSTEM_NAME
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == SERIAL
    assert len(mock_setup_entry.mock_calls) == 1
    assert device.commands == ["getDevice", "login", "getDeviceParams"]


async def test_user_flow_aborts_on_duplicate_device(
    hass: HomeAssistant,
    fake_session: FakeClientSession,
    mock_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
) -> None:
    """The same serial cannot be configured twice."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_invalid_auth(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
) -> None:
    """Rejected credentials are reported on the form and can be corrected."""
    device.password = "a-different-password"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}

    device.password = PASSWORD
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cannot_connect(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
) -> None:
    """An unreachable host is reported as a connection problem."""
    device.connect_error = aiohttp.ClientConnectionError("Connection refused")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.device_info(DEVICE_MHS)
async def test_user_flow_rejects_wrong_device_type(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
) -> None:
    """A window drive is rejected before any credentials are sent to it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_device_type"}
    assert result["description_placeholders"] == {"device_type": "MHS Family"}
    # `getDevice` answers before login, so the flow must not leak credentials
    # to a device it is about to refuse.
    assert device.commands == ["getDevice"]
    assert device.requests("login") == []


async def test_reauth_flow_updates_the_password(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
) -> None:
    """Re-authentication stores the new password on the existing entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=SYSTEM_NAME,
        unique_id=SERIAL,
        data={**ENTRY_DATA, CONF_PASSWORD: "no-longer-valid"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == PASSWORD
    assert entry.data[CONF_HOST] == HOST


async def test_reauth_flow_reports_invalid_auth(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A still-wrong password keeps the re-authentication form open."""
    mock_config_entry.add_to_hass(hass)
    device.password = "yet-another-password"

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "still-wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_flow_updates_the_connection(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Reconfiguring rewrites the connection details of the same door."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_HOST: "192.168.1.99", CONF_PORT: 8443},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == "192.168.1.99"
    assert mock_config_entry.data[CONF_PORT] == 8443
    assert fake_session.urls[-1] == "wss://192.168.1.99:8443/WebSocket"


async def test_reconfigure_flow_rejects_a_different_door(
    hass: HomeAssistant,
    device: FakeDevice,
    fake_session: FakeClientSession,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Pointing an entry at another door aborts instead of hijacking it."""
    mock_config_entry.add_to_hass(hass)
    device.device = {**device.device, "serialnr": "020300160Cyyyy"}

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.168.1.99"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
