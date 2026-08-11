"""Tests for the SIEGENIA WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.siegenia_door.api import (
    SiegeniaAuthError,
    SiegeniaClient,
    SiegeniaCommandError,
    SiegeniaConnectionError,
    SiegeniaError,
    SiegeniaSessionOccupied,
    SiegeniaUnsupportedError,
    iter_json_objects,
    uses_multi_user_model,
)
from custom_components.siegenia_door.const import (
    APID_NONE,
    ENROLLMENT_IN_PROGRESS,
    USER_ID_SCAN_LIMIT,
    USERTYPE_ADMIN,
    USERTYPE_USER,
)

from .conftest import FakeDevice, FakeWebSocket, async_idle
from .const import DEVICE_ACS, PARAMS_OLD_FIRMWARE


def test_iter_json_objects_single_object() -> None:
    """A frame holding one object yields exactly that object."""
    assert iter_json_objects('{"id": 1, "status": "ok"}') == [{"id": 1, "status": "ok"}]


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":1}{"b":2}',
        '{"a":1} {"b":2}',
        '{"a":1}\n\t {"b":2}',
        '  {"a":1}{"b":2}  ',
    ],
    ids=["packed", "space", "whitespace", "surrounded"],
)
def test_iter_json_objects_concatenated_objects(raw: str) -> None:
    """Several objects packed into one frame are split, whitespace or not."""
    assert iter_json_objects(raw) == [{"a": 1}, {"b": 2}]


def test_iter_json_objects_keeps_valid_prefix_before_garbage() -> None:
    """Trailing garbage is dropped without discarding the valid objects."""
    assert iter_json_objects('{"a":1} not json at all') == [{"a": 1}]


@pytest.mark.parametrize("raw", ["", "   ", "\n\t"], ids=["empty", "spaces", "breaks"])
def test_iter_json_objects_empty_frame(raw: str) -> None:
    """An empty or whitespace-only frame yields nothing."""
    assert iter_json_objects(raw) == []


@pytest.mark.parametrize(
    "raw", ["[1, 2, 3]", '"a string"', "42", "null"], ids=["list", "str", "int", "null"]
)
def test_iter_json_objects_non_dict_top_level(raw: str) -> None:
    """Valid JSON that is not an object is discarded rather than returned."""
    assert iter_json_objects(raw) == []


def test_iter_json_objects_keeps_dicts_among_non_dicts() -> None:
    """A non-object between two objects is skipped, the objects are kept."""
    assert iter_json_objects('{"a":1}[2]{"b":3}') == [{"a": 1}, {"b": 3}]


async def test_connect_and_login(client: SiegeniaClient, device: FakeDevice) -> None:
    """A successful start authenticates and leaves the session connected."""
    await client.async_start()

    assert client.connected is True
    assert client.url == "wss://192.168.1.50:443/WebSocket"
    assert device.commands == ["login"]

    login = device.requests("login")[0]
    # The device expects the credentials top-level, not nested under `params`.
    assert login["user"] == "homeassistant"
    assert login["password"] == "correct-horse"
    assert login["long_life"] is False
    assert "params" not in login


async def test_login_rejected_raises_auth_error(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """Bad credentials surface as an auth error and close the socket."""
    device.password = "something-else"

    with pytest.raises(SiegeniaAuthError):
        await client.async_start()

    assert client.connected is False
    assert device.socket.closed is True


async def test_unreachable_host_raises_connection_error(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A host that refuses the socket surfaces as a connection error."""
    device.connect_error = aiohttp.ClientConnectionError("Connection refused")

    with pytest.raises(SiegeniaConnectionError):
        await client.async_start()

    assert client.connected is False
    assert device.sent == []


async def test_response_survives_interleaved_foreign_reply(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A reply for an unknown id does not satisfy the pending request."""
    await client.async_start()

    def reply_after_stranger(ws: FakeWebSocket, request: dict[str, Any]) -> None:
        message_id = request["id"]
        ws.push_json({"data": {"state": "OPEN"}, "id": message_id + 7, "status": "ok"})
        ws.push_json({"data": dict(device.params), "id": message_id, "status": "ok"})

    device.handlers["getDeviceParams"] = reply_after_stranger

    assert await client.async_get_device_params() == PARAMS_OLD_FIRMWARE


async def test_responses_are_correlated_by_id_not_arrival_order(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """Two in-flight requests are matched by id even if the replies swap order.

    Commands are serialised by a lock, so the only way to have two requests in
    flight at once is to register the waiters directly.
    """
    await client.async_start()
    loop = asyncio.get_running_loop()

    first: asyncio.Future[dict[str, Any]] = loop.create_future()
    second: asyncio.Future[dict[str, Any]] = loop.create_future()
    client._pending[101] = first  # noqa: SLF001
    client._pending[102] = second  # noqa: SLF001

    device.socket.push_raw(
        json.dumps({"data": {"for": 102}, "id": 102, "status": "ok"})
        + json.dumps({"data": {"for": 101}, "id": 101, "status": "ok"})
    )
    await async_idle()

    assert (await first)["data"] == {"for": 101}
    assert (await second)["data"] == {"for": 102}


async def test_pending_request_fails_when_the_connection_drops(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A request in flight when the socket dies fails instead of hanging."""
    await client.async_start()
    device.handlers["getDeviceParams"] = lambda ws, request: ws.drop()

    with pytest.raises(SiegeniaConnectionError):
        await client.async_get_device_params()

    assert client.connected is False


async def test_transport_error_ends_the_session(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """An error frame ends the session rather than being parsed as data."""
    await client.async_start()

    device.socket.push_error(aiohttp.ClientConnectionError("Broken pipe"))
    await async_idle()

    assert client.connected is False


async def test_push_sharing_the_takeover_id_is_still_a_push(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """An `id: -1` push must not be mistaken for a takeover.

    The device reuses `id: -1` for both unsolicited updates and takeover
    notices, distinguishing them only by status. Confusing the two would tear
    the session down every time somebody opened the door.
    """
    pushes: list[dict[str, Any]] = []
    availability: list[bool] = []
    client.set_callbacks(on_push=pushes.append, on_availability=availability.append)

    await client.async_start()
    device.push({"state": "OPEN"})
    await async_idle()

    assert pushes == [{"state": "OPEN"}]
    assert client.connected is True
    assert availability == [True]


async def test_session_occupied_takes_the_session_down(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """An unsolicited takeover drops the session and is not seen as a push."""
    pushes: list[dict[str, Any]] = []
    availability: list[bool] = []
    client.set_callbacks(on_push=pushes.append, on_availability=availability.append)

    await client.async_start()
    assert client.connected is True

    device.push_session_occupied()
    await async_idle()

    assert client.connected is False
    assert pushes == []
    # The supervisor picks the drop up and reports the session as unavailable.
    assert availability == [True, False]


async def test_login_refused_because_the_session_is_busy(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A login refused for a busy account is reported as a takeover, not a drop.

    `RESPONSE_TIMEOUT` is shortened so the test does not sit through the real
    ten second wait; the outcome is the same either way.
    """

    def busy(ws: FakeWebSocket, request: dict[str, Any]) -> None:
        ws.push_json({"data": {}, "id": request["id"], "status": "session_occupied"})

    device.handlers["login"] = busy

    with (
        patch("custom_components.siegenia_door.api.RESPONSE_TIMEOUT", 0.05),
        pytest.raises(SiegeniaSessionOccupied),
    ):
        await client.async_start()


async def test_takeover_while_a_request_is_in_flight_fails_it(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A takeover mid-request fails the request rather than leaving it hanging."""
    await client.async_start()
    device.handlers["getDeviceParams"] = lambda ws, request: (
        device.push_session_occupied()
    )

    with pytest.raises(SiegeniaConnectionError):
        await client.async_get_device_params()


async def test_push_frame_invokes_the_callback(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """An unsolicited `deviceParams` frame reaches the push callback."""
    pushes: list[dict[str, Any]] = []
    client.set_callbacks(on_push=pushes.append)

    await client.async_start()
    device.push({"state": "OPEN"})
    await async_idle()

    assert pushes == [{"state": "OPEN"}]
    assert client.connected is True


async def test_probe_does_not_log_in(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """`getDevice` is answered before login, so probing must not authenticate."""
    assert await client.async_probe() == DEVICE_ACS

    assert device.commands == ["getDevice"]
    assert device.requests("login") == []
    assert client.connected is False
    assert device.socket.closed is True


async def test_set_device_params_sends_the_payload(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A parameter update is sent verbatim under `params`."""
    await client.async_start()

    await client.async_set_device_params({"daymode": True})

    assert device.requests("setDeviceParams")[0]["params"] == {"daymode": True}


async def test_command_without_connection_raises(client: SiegeniaClient) -> None:
    """Commanding a client that was never started fails immediately."""
    with pytest.raises(SiegeniaConnectionError):
        await client.async_get_device_params()


async def _drive_enrollment(client: SiegeniaClient, limit: int = 10) -> str | None:
    """Poll the enrollment state until it leaves the in-progress set."""
    state: str | None = None
    for _ in range(limit):
        state = await client.async_get_enrollment_state()
        if state not in ENROLLMENT_IN_PROGRESS:
            break
    return state


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, False),
        ({"acs_master": "io_smart"}, True),
        ({"bus_master": "io_smart"}, True),
        ({"acs_master": 0}, False),
        ({"acs_master": "something_else"}, False),
    ],
    ids=["absent", "acs_master", "bus_master", "zero", "other"],
)
def test_uses_multi_user_model(params: dict[str, Any], expected: bool) -> None:
    """Only an explicit `io_smart` master selects the unverified command family.

    Firmware 1.9.1.23 reports neither key at all, so treating an absent
    parameter as "unknown" would lock every working door out of user management.
    """
    assert uses_multi_user_model(params) is expected


def test_unsupported_error_is_a_siegenia_error() -> None:
    """Callers catching the base class still catch the unsupported-model error."""
    assert issubclass(SiegeniaUnsupportedError, SiegeniaError)


async def test_get_user_returns_the_user_details(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """`getUser` is unwrapped from its `userdetails` envelope."""
    await client.async_start()

    user = await client.async_get_user(1)

    assert user is not None
    assert user["username"] == "alice"
    assert user["ap"] == [{"apid": 2, "aptype": 0}, {"apid": 3, "aptype": 10}]
    assert device.requests("getUser")[0]["params"] == {"userid": 1}


async def test_get_user_returns_none_for_an_unused_id(
    client: SiegeniaClient, caplog: pytest.LogCaptureFixture
) -> None:
    """`not_existent` is an expected answer while enumerating, not a failure.

    Enumeration walks ids the door has never used, so raising here -- or logging
    it as an error -- would turn a normal scan into a wall of noise.
    """
    await client.async_start()

    with caplog.at_level(logging.DEBUG):
        assert await client.async_get_user(99) is None

    assert [record for record in caplog.records if record.levelno >= logging.INFO] == []


async def test_get_user_propagates_other_failures(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A refusal that is not `not_existent` still surfaces to the caller."""
    await client.async_start()
    device.handlers["getUser"] = lambda ws, request: ws.push_json(
        {"data": {}, "id": request["id"], "status": "command_not_found"}
    )

    with pytest.raises(SiegeniaCommandError) as err:
        await client.async_get_user(1)

    assert err.value.status == "command_not_found"


async def test_list_users_returns_every_user(client: SiegeniaClient) -> None:
    """Enumeration yields the whole store, in id order."""
    await client.async_start()

    users = await client.async_list_users(usercount=4)

    assert [user["userid"] for user in users] == [0, 1, 2, 3]
    assert [user["username"] for user in users] == ["Admin", "alice", "bob", "carol"]


async def test_list_users_stops_once_usercount_is_reached(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A known `usercount` bounds the scan instead of probing 64 ids."""
    await client.async_start()

    await client.async_list_users(usercount=4)

    assert len(device.requests("getUser")) == 4


async def test_list_users_scans_to_the_limit_without_a_usercount(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """Without `usercount` the scan runs to its bound rather than guessing."""
    await client.async_start()

    users = await client.async_list_users()

    assert len(users) == 4
    assert len(device.requests("getUser")) == USER_ID_SCAN_LIMIT


async def test_list_users_tolerates_a_gap(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A hole left by a deleted user does not hide the users behind it.

    Ids are dense in practice but nothing guarantees it, and stopping at the
    first `not_existent` would silently drop everybody above the gap.
    """
    await client.async_start()
    del device.users[1]

    users = await client.async_list_users(usercount=3)

    assert [user["userid"] for user in users] == [0, 2, 3]


async def test_create_user_sends_every_field(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """The full payload the app sends goes out, and the new id comes back."""
    await client.async_start()

    userid = await client.async_create_user("dave", "hunter2", starttime=1786453124)

    assert userid == 4
    assert device.requests("createUser")[0]["params"] == {
        "username": "dave",
        "password": "hunter2",
        "starttime": 1786453124,
        "duration": 86400,
        "usertype": USERTYPE_USER,
        "isdisabled": False,
        "isapp": False,
        "keyless": False,
    }


async def test_create_user_defaults_starttime_to_utc_now(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """`starttime` is a plain UTC epoch, with no local-time offset added.

    The ACS screens of the app send `getTimeInMillis() / 1000`; only the
    unrelated MultiUser screens shift it, and copying that would post-date every
    user by the local offset.
    """
    await client.async_start()

    with patch(
        "custom_components.siegenia_door.api.time.time", return_value=1786453124.9
    ):
        await client.async_create_user("dave", "hunter2")

    assert device.requests("createUser")[0]["params"]["starttime"] == 1786453124


async def test_create_admin_forces_app_and_keyless(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """An admin is created with the flags the app forces, not the ones passed."""
    await client.async_start()

    await client.async_create_user(
        "dave",
        "hunter2",
        usertype=USERTYPE_ADMIN,
        isdisabled=True,
        isapp=False,
        keyless=False,
    )

    params = device.requests("createUser")[0]["params"]
    assert params["isdisabled"] is False
    assert params["isapp"] is True
    assert params["keyless"] is True


async def test_create_user_without_a_userid_in_the_reply_raises(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A create whose id is missing fails rather than returning a made-up one."""
    await client.async_start()
    device.handlers["createUser"] = lambda ws, request: ws.push_json(
        {"data": {}, "id": request["id"], "status": "ok"}
    )

    with pytest.raises(SiegeniaError):
        await client.async_create_user("dave", "hunter2")


async def test_created_user_is_readable_and_counted(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A created user shows up in `getUser`, and the door's `usercount` grows."""
    await client.async_start()

    userid = await client.async_create_user("dave", "hunter2")
    user = await client.async_get_user(userid)

    assert user is not None
    assert user["username"] == "dave"
    assert user["ap"] == []
    assert device.params["usercount"] == 5


async def test_delete_user_removes_it(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A deleted user stops existing, and the door's `usercount` shrinks."""
    await client.async_start()

    await client.async_delete_user(2)

    assert device.requests("deleteUser")[0]["params"] == {"userid": 2}
    assert await client.async_get_user(2) is None
    assert device.params["usercount"] == 3


async def test_delete_user_raises_for_an_unused_id(client: SiegeniaClient) -> None:
    """Deleting nothing is a real failure, unlike reading nothing."""
    await client.async_start()

    with pytest.raises(SiegeniaCommandError) as err:
        await client.async_delete_user(99)

    assert err.value.status == "not_existent"


async def test_create_access_property_omits_the_code_for_a_fingerprint(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A fingerprint carries no payload; it is captured at the door itself."""
    await client.async_start()

    await client.async_create_access_property(2, 1)

    assert device.requests("createAccessProperty")[0]["params"] == {
        "userid": 2,
        "aptype": 1,
    }


async def test_create_access_property_sends_the_code_for_a_pin(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A PIN is the one credential whose digits travel with the request."""
    await client.async_start()

    await client.async_create_access_property(2, 20, code="123456")

    assert device.requests("createAccessProperty")[0]["params"] == {
        "userid": 2,
        "aptype": 20,
        "code": "123456",
    }


async def test_enrollment_finish_attaches_the_credential(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """The new `apid` is learnable only by re-reading the user after FINISH."""
    await client.async_start()

    await client.async_create_access_property(2, 1)
    state = await _drive_enrollment(client)
    user = await client.async_get_user(2)

    assert state == "FINISH"
    assert user is not None
    assert {"apid": 6, "aptype": 1} in user["ap"]


async def test_enrollment_allocates_apids_globally_not_per_user(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """Consecutive enrollments for different users take consecutive `apid`s.

    The door numbers access properties across the whole store, so an
    implementation that assumed per-user numbering would delete or address
    somebody else's credential.
    """
    await client.async_start()

    await client.async_create_access_property(2, 1)
    await _drive_enrollment(client)
    await client.async_create_access_property(3, 0)
    await _drive_enrollment(client)

    bob = await client.async_get_user(2)
    carol = await client.async_get_user(3)
    assert bob is not None
    assert carol is not None
    assert [ap["apid"] for ap in bob["ap"]] == [5, 6]
    assert [ap["apid"] for ap in carol["ap"]] == [7]


async def test_enrollment_can_stay_in_progress_forever(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A door waiting for a finger never finishes on its own.

    This is the case the client-side timeout exists for: nothing in the protocol
    ends it, so a poll loop without a deadline would run until the entry unloads.
    """
    await client.async_start()
    device.enrollment_script = ["START", "ENROLL"]

    await client.async_create_access_property(2, 1)
    states = [await client.async_get_enrollment_state() for _ in range(5)]

    assert states == ["START", "ENROLL", "ENROLL", "ENROLL", "ENROLL"]
    assert set(states) <= ENROLLMENT_IN_PROGRESS


async def test_abort_enrollment_sends_only_the_abort_flag(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """An abort reuses `createAccessProperty` and returns the empty sentinel."""
    await client.async_start()
    await client.async_create_access_property(2, 1)

    apid = await client.async_abort_enrollment()

    assert apid == APID_NONE
    assert device.requests("createAccessProperty")[1]["params"] == {"abort": True}
    assert await client.async_get_enrollment_state() == "NO_ENROLL_ACTIVE"


async def test_abort_enrollment_without_an_apid_reports_the_sentinel(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A reply with no `apid` still means nothing was created."""
    await client.async_start()
    device.handlers["createAccessProperty"] = lambda ws, request: ws.push_json(
        {"data": {}, "id": request["id"], "status": "ok"}
    )

    assert await client.async_abort_enrollment() == APID_NONE


async def test_delete_access_property_removes_one_credential(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """Deletion addresses the `apid`, leaving the user's other slots alone."""
    await client.async_start()

    await client.async_delete_access_property(2)

    assert device.requests("deleteAccessProperty")[0]["params"] == {"apid": 2}
    user = await client.async_get_user(1)
    assert user is not None
    assert user["ap"] == [{"apid": 3, "aptype": 10}]


async def test_get_enrollment_state_returns_the_raw_string(
    client: SiegeniaClient,
) -> None:
    """The resting value is reported as-is, not translated into a failure."""
    await client.async_start()

    assert await client.async_get_enrollment_state() == "NO_ENROLL_ACTIVE"


async def test_get_enrollment_state_without_a_state_returns_none(
    client: SiegeniaClient, device: FakeDevice
) -> None:
    """A reply carrying no state reads as unknown rather than as success."""
    await client.async_start()
    device.handlers["getEnrollmentState"] = lambda ws, request: ws.push_json(
        {"data": {}, "id": request["id"], "status": "ok"}
    )

    assert await client.async_get_enrollment_state() is None
