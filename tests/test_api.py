"""Tests for the SIEGENIA WebSocket client."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.siegenia_door.api import (
    SiegeniaAuthError,
    SiegeniaClient,
    SiegeniaConnectionError,
    SiegeniaSessionOccupied,
    iter_json_objects,
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
