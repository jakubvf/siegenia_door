"""Tests for the SIEGENIA Door options flow.

The options flow writes nothing into the config entry: every step is a command
sent to the door, so each test asserts against the fake device's user store or
the requests it received rather than against `entry.options`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.siegenia_door.config_flow import STEP_ADD_USER_SCHEMA

from .conftest import FakeDevice
from .const import LOCK_ENTITY_ID, PARAMS_OLD_FIRMWARE

# A door that selects the unverified `...MultiUser` command family.
PARAMS_IO_SMART: dict[str, Any] = {**PARAMS_OLD_FIRMWARE, "acs_master": "io_smart"}

# Small but non-zero: a poll interval of zero would let the enrollment task
# finish eagerly, before the progress step ever gets to show itself, and the
# progress path would go untested.
FAST_POLL = 0.01


def patch_enrollment_timings(*, timeout: float = 30.0) -> Any:
    """Run the enrollment poll on test timings rather than the door's."""
    return patch.multiple(
        "custom_components.siegenia_door.config_flow",
        ENROLLMENT_POLL_INTERVAL=FAST_POLL,
        ENROLLMENT_TIMEOUT=timeout,
    )


async def async_open_user_menu(
    hass: HomeAssistant, entry: MockConfigEntry, userid: int
) -> dict[str, Any]:
    """Walk the flow as far as the menu for one existing user."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "select_user"}
    )
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"userid": str(userid)}
    )


async def test_options_menu_offers_adding_and_managing_users(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """The entry point is a menu whose every option is a real step."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert list(result["menu_options"]) == ["add_user", "select_user"]

    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.device_params(PARAMS_IO_SMART)
async def test_options_flow_refuses_the_io_smart_user_model(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """A door on the unverified command family is refused, not guessed at."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_user_model"
    assert device.requests("getUser") == []


async def test_add_user_creates_the_user_on_the_door(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """Submitting the form creates the user and returns to the top menu."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_user"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "dave", CONF_PASSWORD: "s3cret", "usertype": "1"},
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"

    params = device.requests("createUser")[-1]["params"]
    assert params["username"] == "dave"
    # Admin, so the door-side rules the app applies must have been applied here.
    assert params["usertype"] == 1
    assert params["isapp"] is True
    assert params["keyless"] is True
    assert any(user["username"] == "dave" for user in device.users.values())

    hass.config_entries.options.async_abort(result["flow_id"])


async def test_add_user_form_never_echoes_the_password(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """A refused create reopens the form empty, not pre-filled with secrets."""
    device.handlers["createUser"] = lambda ws, request: ws.push_json(
        {"data": {}, "id": request.get("id"), "status": "error"}
    )

    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_user"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "dave", CONF_PASSWORD: "s3cret", "usertype": "2"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "create_user_failed"}
    # The untouched schema object carries no suggested values, so nothing the
    # user typed -- least of all the password -- is rendered back at them.
    assert result["data_schema"] is STEP_ADD_USER_SCHEMA

    hass.config_entries.options.async_abort(result["flow_id"])


async def test_delete_user_removes_it_from_the_door(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """Confirming the deletion removes the user and ends the flow."""
    result = await async_open_user_menu(hass, init_integration, 2)
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"] == {"username": "bob"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "delete_user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "delete_user"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert device.requests("deleteUser")[-1]["params"] == {"userid": 2}
    assert 2 not in device.users
    # Nothing is stored locally: the users live on the door.
    assert init_integration.options == {}


async def test_remove_credential_deletes_the_chosen_access_property(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """Removal goes by `apid`, so it cannot hit another user's credential."""
    result = await async_open_user_menu(hass, init_integration, 1)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_credential"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"apid": "3"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert device.requests("deleteAccessProperty")[-1]["params"] == {"apid": 3}
    assert device.users[1]["ap"] == [{"apid": 2, "aptype": 0}]
    # bob's fingerprint shares alice's aptype but not her apid, and must survive.
    assert device.users[2]["ap"] == [{"apid": 5, "aptype": 0}]


async def test_enroll_fingerprint_offers_only_the_free_slots(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """A slot already in use is not offered for a second enrollment."""
    result = await async_open_user_menu(hass, init_integration, 1)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )

    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema["aptype"].config["options"]
    # alice holds fingerprint slot 0; the RFID tag is not a fingerprint slot.
    assert [option["value"] for option in options] == ["1", "2", "3"]

    hass.config_entries.options.async_abort(result["flow_id"])


async def test_enroll_fingerprint_refuses_when_all_slots_are_taken(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """Four fingerprints is an error path, not an empty dropdown."""
    device.users[1]["ap"] = [{"apid": 20 + slot, "aptype": slot} for slot in range(4)]

    result = await async_open_user_menu(hass, init_integration, 1)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_free_fingerprint_slots"
    assert device.requests("createAccessProperty") == []


async def test_enroll_fingerprint_rejects_a_slot_taken_while_the_form_was_open(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """The slot list is re-read on submit, so a race cannot overwrite a slot."""
    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    assert result["type"] is FlowResultType.FORM

    device.users[3]["ap"] = [{"apid": 9, "aptype": 0}]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"aptype": "0"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"aptype": "slot_taken"}
    assert device.requests("createAccessProperty") == []

    hass.config_entries.options.async_abort(result["flow_id"])


async def test_enroll_fingerprint_refuses_while_the_door_is_already_enrolling(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """Starting a second enrollment would silently cancel someone else's."""
    device.enrollment_state = "ENROLL"

    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"aptype": "0"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "enrollment_already_running"
    # Neither a start nor an abort: the running enrollment is left alone.
    assert device.requests("createAccessProperty") == []


async def test_the_lock_stays_available_while_the_door_enrolls(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """A door busy capturing a credential must not flap every entity.

    Firmware 1.9.1.23 refuses `getDeviceParams` for the whole of an enrollment,
    so without this the lock would go unavailable for the half minute a
    fingerprint takes, and log an error for every poll in between.
    """
    device.enrollment_script = ["START", "ENROLL"]

    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    flow_id = result["flow_id"]

    with patch_enrollment_timings():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"aptype": "0"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS

        coordinator = init_integration.runtime_data
        assert coordinator.enrollment_active is True

        # The door refuses this poll; the last known state must survive it.
        await coordinator.async_refresh()
        assert coordinator.last_update_success is True
        assert hass.states.get(LOCK_ENTITY_ID).state != STATE_UNAVAILABLE

        hass.config_entries.options.async_abort(flow_id)
        await hass.async_block_till_done()

    assert coordinator.enrollment_active is False


async def test_a_refused_poll_outside_an_enrollment_still_counts_as_a_failure(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """The enrollment suppression must not become a permanent blindfold."""
    coordinator = init_integration.runtime_data
    assert coordinator.enrollment_active is False

    device.handlers["getDeviceParams"] = lambda ws, request: ws.push_json(
        {"data": {}, "id": request.get("id"), "status": "error"}
    )
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert hass.states.get(LOCK_ENTITY_ID).state == STATE_UNAVAILABLE


async def test_enrollment_clears_a_stale_finish_before_starting(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """A FINISH left by an earlier run must not be read as this run's success.

    The door keeps the previous enrollment's terminal state, so a poll that
    trusts the first FINISH it sees reports success within a second, before the
    reader has been touched, and stores no credential. Observed on real
    hardware: the flow reported success and the user's `ap` array stayed empty.
    """
    device.enrollment_state = "FINISH"

    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    flow_id = result["flow_id"]

    with patch_enrollment_timings():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"aptype": "0"}
        )
        # The stale state is cleared, so this is a real enrollment, not an
        # instant false success.
        assert result["type"] is FlowResultType.SHOW_PROGRESS

        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(flow_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The abort that reset the machine comes first, then the real start.
    starts = device.requests("createAccessProperty")
    assert starts[0]["params"] == {"abort": True}
    assert starts[1]["params"] == {"userid": 3, "aptype": 0}
    # And the credential genuinely exists, which is what the live run lacked.
    assert device.users[3]["ap"] == [{"apid": 6, "aptype": 0}]


async def test_enrollment_refuses_a_door_that_will_not_go_idle(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """If the machine cannot be reset, polling it could not tell runs apart."""
    device.enrollment_state = "FINISH"

    def ignore_abort(ws: Any, request: dict[str, Any]) -> None:
        """Answer an abort without ever leaving the terminal state."""
        ws.push_json({"data": {"apid": 65535}, "id": request["id"], "status": "ok"})

    device.handlers["createAccessProperty"] = ignore_abort

    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"aptype": "0"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "enrollment_not_idle"
    # Refused before starting anything, so nothing is left half open.
    assert device.users[3]["ap"] == []


async def test_enrollment_stores_the_fingerprint_when_the_door_finishes(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """A run reaching FINISH ends the flow and leaves no abort behind."""
    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    flow_id = result["flow_id"]

    with patch_enrollment_timings():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"aptype": "0"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "enrolling"
        # The task is popped by the manager before the result is handed out.
        assert "progress_task" not in result
        # The instruction has to be on screen before the poll can succeed.
        assert result["description_placeholders"] == {
            "username": "carol",
            "slot": "Fingerprint 1",
        }

        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(flow_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert device.requests("createAccessProperty")[0]["params"] == {
        "userid": 3,
        "aptype": 0,
    }
    # apid 6 follows the fixture's global maximum of 5, not a per-user count.
    assert device.users[3]["ap"] == [{"apid": 6, "aptype": 0}]
    # One pre-check that nothing else was enrolling, then one poll per scripted
    # state -- proof the progress task really ran rather than being skipped.
    assert len(device.requests("getEnrollmentState")) == 4
    assert [
        request
        for request in device.requests("createAccessProperty")
        if request["params"].get("abort")
    ] == []


async def test_enrollment_timeout_aborts_at_the_door(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """The door waits forever, so the client's deadline must cancel it."""
    # The script sticks on its last entry, as a door nobody presents a finger to.
    device.enrollment_script = ["START", "ENROLL"]

    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    flow_id = result["flow_id"]

    with patch_enrollment_timings(timeout=0.05):
        result = await hass.config_entries.options.async_configure(
            flow_id, {"aptype": "0"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS

        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(flow_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "enrollment_timeout"
    assert device.requests("createAccessProperty")[-1]["params"] == {"abort": True}
    assert device.users[3]["ap"] == []


async def test_closing_the_dialog_aborts_the_enrollment(
    hass: HomeAssistant,
    device: FakeDevice,
    init_integration: MockConfigEntry,
) -> None:
    """A cancelled progress task cannot reach the door, so removal must."""
    device.enrollment_script = ["START", "ENROLL"]

    result = await async_open_user_menu(hass, init_integration, 3)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "enroll_fingerprint"}
    )
    flow_id = result["flow_id"]

    with patch_enrollment_timings():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"aptype": "0"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS

        # Let the poll get going first: closing before the task has run at all
        # would exercise a different, much easier path than a real mid-poll
        # cancellation, and `async_block_till_done` cannot be used to wait for a
        # task that is designed never to finish.
        await asyncio.sleep(FAST_POLL * 3)
        assert len(device.requests("getEnrollmentState")) > 1

        hass.config_entries.options.async_abort(flow_id)
        await hass.async_block_till_done()

    assert device.requests("createAccessProperty")[-1]["params"] == {"abort": True}
    assert device.users[3]["ap"] == []
