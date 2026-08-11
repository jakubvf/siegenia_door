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
            ws.push_json({"data": dict(self.params), "id": message_id, "status": "ok"})
        elif command == "setDeviceParams":
            if self.apply_set_device_params:
                self.params.update(request.get("params") or {})
            ws.push_json({"id": message_id, "status": "ok"})
        elif command == "keepAlive":
            ws.push_json({"id": message_id, "status": "ok"})
        else:
            ws.push_json({"id": message_id, "status": "error"})

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
