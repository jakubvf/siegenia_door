"""Fixtures and protocol fakes for the SIEGENIA Door test suite.

The device speaks JSON over a TLS WebSocket. Rather than opening a real socket,
the tests drive a `FakeDevice` that answers the handful of commands the
integration sends, echoing back the `id` of every request the way the real
firmware does. `async_get_clientsession` is patched wherever the integration
imports it, so both the config flow and the runtime get the fake transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from copy import deepcopy
from itertools import count
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.siegenia_door.api import SiegeniaClient
from custom_components.siegenia_door.const import DOMAIN

from .const import (
    DEVICE_ACS,
    ENTRY_DATA,
    HOST,
    PARAMS_OLD_FIRMWARE,
    PASSWORD,
    SERIAL,
    SYSTEM_NAME,
    USERNAME,
    USERS_ACS,
)

# Number of event loop iterations granted to background tasks when a test needs
# an unsolicited frame to be dispatched. Cheap, and never a real sleep.
_IDLE_ITERATIONS = 10


def pytest_configure(config: pytest.Config) -> None:
    """Register the markers used to shape the fake device per test."""
    config.addinivalue_line(
        "markers", "device_info(payload): getDevice payload the fake device reports"
    )
    config.addinivalue_line(
        "markers",
        "device_params(payload): getDeviceParams payload the fake device reports",
    )


async def async_idle() -> None:
    """Let background tasks run without advancing the clock."""
    for _ in range(_IDLE_ITERATIONS):
        await asyncio.sleep(0)


class FakeWebSocket:
    """A scripted stand-in for `aiohttp.ClientWebSocketResponse`."""

    def __init__(self, device: FakeDevice) -> None:
        """Start an open socket bound to the device that answers for it."""
        self._device = device
        self._queue: asyncio.Queue[aiohttp.WSMessage | None] = asyncio.Queue()
        self._exception: BaseException | None = None
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_str(self, data: str) -> None:
        """Record an outgoing request and let the device answer it."""
        if self.closed:
            raise ConnectionResetError("Cannot write to closing transport")
        request = json.loads(data)
        self.sent.append(request)
        self._device.sent.append(request)
        self._device.handle(self, request)

    def push_raw(self, raw: str) -> None:
        """Queue a verbatim text frame, which may hold several JSON objects."""
        self._queue.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, raw, ""))

    def push_json(self, payload: dict[str, Any]) -> None:
        """Queue a single JSON object as one text frame."""
        self.push_raw(json.dumps(payload))

    def push_error(self, error: BaseException) -> None:
        """Queue a transport error frame, as aiohttp reports a broken link."""
        self._exception = error
        self._queue.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, error, ""))

    def drop(self) -> None:
        """Close the socket from the device side, mid-flight if need be."""
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(None)

    async def close(self, **kwargs: Any) -> None:
        """Close the socket from the client side."""
        self.drop()

    def exception(self) -> BaseException | None:
        """Return the transport error, if the socket failed."""
        return self._exception

    def __aiter__(self) -> FakeWebSocket:
        """Iterate incoming frames."""
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        """Return the next frame, ending iteration when the socket closes."""
        message = await self._queue.get()
        if message is None:
            raise StopAsyncIteration
        return message


class FakeDevice:
    """A SIEGENIA door that answers commands over a `FakeWebSocket`."""

    def __init__(self) -> None:
        """Start with an ACS door on old firmware and valid credentials."""
        self.device: dict[str, Any] = dict(DEVICE_ACS)
        self.params: dict[str, Any] = dict(PARAMS_OLD_FIRMWARE)
        self.username = USERNAME
        self.password = PASSWORD
        self.connect_error: Exception | None = None
        self.apply_set_device_params = False
        self.sent: list[dict[str, Any]] = []
        self.sockets: list[FakeWebSocket] = []
        self.handlers: dict[str, Callable[[FakeWebSocket, dict[str, Any]], None]] = {}

        # ACS user store, keyed by userid, holding exactly what `getUser` reports.
        self.users: dict[int, dict[str, Any]] = {
            user["userid"]: deepcopy(user) for user in USERS_ACS
        }
        # The door allocates `apid`s monotonically across *all* users, so the
        # fake keeps a single counter rather than one per user. A per-user
        # counter would make every test of that property vacuous.
        self.next_apid = 1 + max(
            (ap["apid"] for user in self.users.values() for ap in user["ap"]),
            default=-1,
        )

        # States handed out by successive `getEnrollmentState` calls. The last
        # one sticks, so a script ending in ENROLL never finishes -- which is
        # exactly how the door behaves when nobody presents a finger, and is how
        # tests reach the client-side timeout.
        self.enrollment_script: list[str] = ["START", "ENROLL", "FINISH"]
        self.enrollment_state = "NO_ENROLL_ACTIVE"
        self._enrollment_remaining: list[str] = []
        self._enrolling: dict[str, Any] | None = None

        self._sync_usercount()

    @property
    def socket(self) -> FakeWebSocket:
        """Return the most recently opened socket."""
        return self.sockets[-1]

    @property
    def commands(self) -> list[str | None]:
        """Return the command name of every request the device ever received."""
        return [request.get("command") for request in self.sent]

    def requests(self, command: str) -> list[dict[str, Any]]:
        """Return every request received for one command."""
        return [request for request in self.sent if request.get("command") == command]

    def handle(self, ws: FakeWebSocket, request: dict[str, Any]) -> None:
        """Answer one request, echoing back its id the way the firmware does."""
        command = request.get("command")
        if (handler := self.handlers.get(str(command))) is not None:
            handler(ws, request)
            return

        message_id = request.get("id")

        if command == "getDevice":
            ws.push_json({"data": dict(self.device), "id": message_id, "status": "ok"})
        elif command == "login":
            # `user` and `password` are top-level on this command, not in params.
            if (
                request.get("user") == self.username
                and request.get("password") == self.password
            ):
                ws.push_json(
                    {
                        "data": {
                            "isadmin": True,
                            "token": "1234567",
                            "user": self.username,
                            "userid": 0,
                        },
                        "id": message_id,
                        "status": "ok",
                    }
                )
            else:
                ws.push_json(
                    {"data": {}, "id": message_id, "status": "authentication_error"}
                )
        elif command == "getDeviceParams":
            if self._enrolling is not None:
                # Confirmed on firmware 1.9.1.23: a door capturing a credential
                # refuses everything except `getEnrollmentState` (and keepAlive)
                # with a bare `error` for as long as it takes. A fake that kept
                # answering would hide the entity flapping that causes.
                ws.push_json({"data": {}, "id": message_id, "status": "error"})
            else:
                ws.push_json(
                    {"data": dict(self.params), "id": message_id, "status": "ok"}
                )
        elif command == "setDeviceParams":
            if self.apply_set_device_params:
                self.params.update(request.get("params") or {})
            ws.push_json({"id": message_id, "status": "ok"})
        elif command == "keepAlive":
            ws.push_json({"id": message_id, "status": "ok"})
        elif command == "getUser":
            self._handle_get_user(ws, request)
        elif command == "createUser":
            self._handle_create_user(ws, request)
        elif command == "deleteUser":
            self._handle_delete_user(ws, request)
        elif command == "createAccessProperty":
            self._handle_create_access_property(ws, request)
        elif command == "deleteAccessProperty":
            self._handle_delete_access_property(ws, request)
        elif command == "getEnrollmentState":
            self._handle_get_enrollment_state(ws, request)
        else:
            ws.push_json({"id": message_id, "status": "error"})

    def _handle_get_user(self, ws: FakeWebSocket, request: dict[str, Any]) -> None:
        """Answer `getUser`, reporting an id that holds no user as absent."""
        message_id = request.get("id")
        userid = (request.get("params") or {}).get("userid")

        if self._enrolling is not None:
            # Refused mid-enrollment, exactly as the real door refuses it.
            ws.push_json({"data": {}, "id": message_id, "status": "error"})
            return

        if (user := self.users.get(userid)) is None:
            ws.push_json({"data": {}, "id": message_id, "status": "not_existent"})
            return

        ws.push_json(
            {"data": {"userdetails": deepcopy(user)}, "id": message_id, "status": "ok"}
        )

    def _handle_create_user(self, ws: FakeWebSocket, request: dict[str, Any]) -> None:
        """Store a new user under the id the door would have allocated."""
        params = dict(request.get("params") or {})
        userid = next(candidate for candidate in count() if candidate not in self.users)

        # `getUser` echoes every field back except the password, which the door
        # never reports again.
        params.pop("password", None)
        self.users[userid] = {"userid": userid, **params, "ap": []}
        self._sync_usercount()

        ws.push_json(
            {"data": {"userid": userid}, "id": request.get("id"), "status": "ok"}
        )

    def _handle_delete_user(self, ws: FakeWebSocket, request: dict[str, Any]) -> None:
        """Delete a user and everything enrolled against it."""
        message_id = request.get("id")
        userid = (request.get("params") or {}).get("userid")

        if self.users.pop(userid, None) is None:
            ws.push_json({"data": {}, "id": message_id, "status": "not_existent"})
            return

        self._sync_usercount()
        ws.push_json({"data": {"userid": userid}, "id": message_id, "status": "ok"})

    def _handle_create_access_property(
        self, ws: FakeWebSocket, request: dict[str, Any]
    ) -> None:
        """Start a scripted enrollment, or abort the running one."""
        message_id = request.get("id")
        params = request.get("params") or {}

        if params.get("abort"):
            self._enrolling = None
            self._enrollment_remaining = []
            self.enrollment_state = "NO_ENROLL_ACTIVE"
            # 0xFFFF: the sentinel for "nothing was created".
            ws.push_json({"data": {"apid": 65535}, "id": message_id, "status": "ok"})
            return

        self._enrolling = {
            "userid": params.get("userid"),
            "aptype": params.get("aptype"),
        }
        self._enrollment_remaining = list(self.enrollment_script)

        # Confirmed on hardware: the reply carries no apid. The new one is only
        # learnable from `getUser` once the state machine reaches FINISH.
        ws.push_json({"data": {}, "id": message_id, "status": "ok"})

    def _handle_delete_access_property(
        self, ws: FakeWebSocket, request: dict[str, Any]
    ) -> None:
        """Remove one access property, wherever in the store it lives."""
        apid = (request.get("params") or {}).get("apid")

        for user in self.users.values():
            user["ap"] = [ap for ap in user["ap"] if ap["apid"] != apid]

        ws.push_json({"data": {"apid": apid}, "id": request.get("id"), "status": "ok"})

    def _handle_get_enrollment_state(
        self, ws: FakeWebSocket, request: dict[str, Any]
    ) -> None:
        """Hand out the next scripted state, sticking on the last one."""
        if self._enrollment_remaining:
            # Popping all but the last entry makes the script's tail the resting
            # state of this enrollment, so a script that never reaches FINISH
            # keeps the client polling exactly as the real door does.
            if len(self._enrollment_remaining) > 1:
                self.enrollment_state = self._enrollment_remaining.pop(0)
            else:
                self.enrollment_state = self._enrollment_remaining[0]
            if self.enrollment_state == "FINISH":
                self._finish_enrollment()

        ws.push_json(
            {
                "data": {"enrollmentstate": self.enrollment_state},
                "id": request.get("id"),
                "status": "ok",
            }
        )

    def _finish_enrollment(self) -> None:
        """Attach the credential the enrollment created to its user."""
        if self._enrolling is None:
            return

        if (user := self.users.get(self._enrolling["userid"])) is not None:
            user["ap"].append(
                {"apid": self.next_apid, "aptype": self._enrolling["aptype"]}
            )
            self.next_apid += 1

        self._enrolling = None

    def _sync_usercount(self) -> None:
        """Keep the reported `usercount` in step with the store."""
        self.params["usercount"] = len(self.users)

    def push(self, data: dict[str, Any]) -> None:
        """Send an unsolicited `deviceParams` update over the live socket.

        This is the exact envelope captured from firmware 1.9.1.23, `id` and
        all. The id matters: a real push shares `id: -1` with a takeover notice,
        so omitting it here would route the frame differently in the client than
        it is routed in production, and these tests would prove nothing about
        the path the device actually exercises.
        """
        self.socket.push_json(
            {"command": "deviceParams", "data": data, "id": -1, "status": "update"}
        )

    def push_session_occupied(self) -> None:
        """Announce that another client has taken the account over."""
        self.socket.push_json({"id": -1, "status": "session_occupied"})


class FakeClientSession:
    """A stand-in for `aiohttp.ClientSession` that hands out fake sockets."""

    def __init__(self, device: FakeDevice) -> None:
        """Bind the session to the device it connects to."""
        self._device = device
        self.urls: list[str] = []
        self.connect_kwargs: list[dict[str, Any]] = []
        self.closed = False

    async def ws_connect(self, url: str, **kwargs: Any) -> FakeWebSocket:
        """Open a socket, or fail the way an unreachable host does."""
        self.urls.append(url)
        self.connect_kwargs.append(kwargs)
        if self._device.connect_error is not None:
            raise self._device.connect_error
        ws = FakeWebSocket(self._device)
        self._device.sockets.append(ws)
        return ws

    async def close(self) -> None:
        """Close the session."""
        self.closed = True

    def detach(self) -> None:
        """Detach the connector, as Home Assistant does on shutdown."""
        self.closed = True


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make the integration under test loadable by Home Assistant."""


@pytest.fixture
def device(request: pytest.FixtureRequest) -> FakeDevice:
    """Return a fake door, shaped by the `device_info`/`device_params` markers."""
    fake = FakeDevice()
    if (marker := request.node.get_closest_marker("device_info")) is not None:
        fake.device = dict(marker.args[0])
    if (marker := request.node.get_closest_marker("device_params")) is not None:
        fake.params = dict(marker.args[0])
    return fake


@pytest.fixture
def fake_session(device: FakeDevice) -> Generator[FakeClientSession]:
    """Patch client session creation everywhere the integration uses it."""
    session = FakeClientSession(device)
    with (
        patch(
            "custom_components.siegenia_door.async_get_clientsession",
            return_value=session,
        ),
        patch(
            "custom_components.siegenia_door.config_flow.async_get_clientsession",
            return_value=session,
        ),
    ):
        yield session


@pytest.fixture
async def client(fake_session: FakeClientSession) -> AsyncGenerator[SiegeniaClient]:
    """Return an unconnected client, guaranteed to be stopped afterwards."""
    instance = SiegeniaClient(fake_session, HOST, USERNAME, PASSWORD)  # type: ignore[arg-type]
    yield instance
    await instance.async_stop()


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Skip runtime setup so config flow tests only exercise the flow."""
    with patch(
        "custom_components.siegenia_door.async_setup_entry", return_value=True
    ) as mocked:
        yield mocked


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the door the fake device impersonates."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=SYSTEM_NAME,
        unique_id=SERIAL,
        data=dict(ENTRY_DATA),
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_session: FakeClientSession,
) -> AsyncGenerator[MockConfigEntry]:
    """Set the integration up, and unload it again so no tasks linger."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    yield mock_config_entry

    if mock_config_entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
