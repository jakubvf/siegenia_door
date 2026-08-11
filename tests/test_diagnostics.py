"""Tests for SIEGENIA Door diagnostics."""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.siegenia_door.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .const import MAC, PARAMS_NEW_FIRMWARE, PARAMS_OLD_FIRMWARE, SERIAL


async def test_diagnostics_redact_identifiers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Everything that identifies the user or the hardware is redacted."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)

    assert diagnostics["entry"][CONF_HOST] == REDACTED
    assert diagnostics["entry"][CONF_USERNAME] == REDACTED
    assert diagnostics["entry"][CONF_PASSWORD] == REDACTED
    # The port is not sensitive, and is worth keeping in a bug report.
    assert diagnostics["entry"][CONF_PORT] == 443

    assert diagnostics["device"]["serialnr"] == REDACTED
    assert diagnostics["device"]["systemname"] == REDACTED
    assert diagnostics["params"]["mac"] == REDACTED

    assert SERIAL not in str(diagnostics)
    assert MAC not in str(diagnostics)


async def test_diagnostics_report_the_session(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Diagnostics describe the live session, not just the stored config."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)

    assert diagnostics["connected"] is True
    assert diagnostics["last_update_success"] is True
    assert diagnostics["poll_interval"] == 5
    assert diagnostics["device"]["type"] == 7
    assert diagnostics["params"]["state"] == "CLOSED"


@pytest.mark.device_params(PARAMS_OLD_FIRMWARE)
async def test_diagnostics_report_daymode_support(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Firmware that reports day mode is called out as supporting it."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)

    assert diagnostics["supports_daymode"] is True


@pytest.mark.device_params(PARAMS_NEW_FIRMWARE)
async def test_diagnostics_report_missing_daymode_support(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Firmware without day mode is called out as lacking it."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)

    assert diagnostics["supports_daymode"] is False
    assert "daymode" not in diagnostics["params"]
