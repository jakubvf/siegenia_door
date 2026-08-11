"""Async WebSocket client for SIEGENIA devices.

The device speaks newline-free JSON over a TLS WebSocket at `/WebSocket`, using
a self-signed certificate. There is no public protocol specification; this
implementation follows behaviour attested in packet captures of the official
SIEGENIA Comfort App and in the ioBroker/homebridge reference implementations.

Three protocol quirks drive the shape of this module:

* The device may pack several JSON objects into a single WebSocket text frame,
  so frames are parsed incrementally rather than with a plain `json.loads`.
* Responses are correlated by an echoed `id`. Unsolicited traffic uses `id: -1`
  for both parameter pushes and takeover notices, which are told apart only by
  their status -- `update` versus `session_occupied`. Captured verbatim from
  firmware 1.9.1.23:

      {"command": "deviceParams", "data": {"state": "OPEN"}, "id": -1,
       "status": "update"}

  Pushes are partial: a frame carries only the keys that changed.
* The device permits only one session per account, and closes the connection
  with a close frame that violates RFC 6455. Both are normal events here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from itertools import count
import json
import logging
from typing import Any

import aiohttp

from .const import (
    CONNECT_TIMEOUT,
    KEEPALIVE_INTERVAL,
    RECONNECT_MAX_BACKOFF,
    RESPONSE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

_DECODER = json.JSONDecoder()

# The device reports failures as a status string; there are no numeric codes.
_AUTH_STATUSES = {"authentication_error", "not_authenticated", "loginRequired"}
_SESSION_OCCUPIED = "session_occupied"
_PUSH_COMMANDS = {"deviceParams", "getDeviceParams"}


class SiegeniaError(Exception):
    """Base class for all errors raised by this client."""


class SiegeniaConnectionError(SiegeniaError):
    """The device is unreachable, or an established connection was lost."""


class SiegeniaAuthError(SiegeniaError):
    """The device rejected the supplied credentials."""


class SiegeniaSessionOccupied(SiegeniaAuthError):
    """Another client already holds a session for this account.

    The device allows a single concurrent session per user, so the phone app and
    Home Assistant will evict each other unless they log in as different users.
    """


class SiegeniaCommandError(SiegeniaError):
    """The device accepted the connection but refused a command."""

    def __init__(self, command: str, status: str | None) -> None:
        """Record which command failed and the status the device returned."""
        super().__init__(f"Command {command!r} failed with status {status!r}")
        self.command = command
        self.status = status


def iter_json_objects(raw: str) -> list[dict[str, Any]]:
    """Split one WebSocket text frame into the JSON objects it contains.

    A single frame may hold several concatenated objects, e.g.
    `{"id":1,"status":"ok"} {"id":2,"status":"ok"}`, which `json.loads` rejects
    outright. Trailing garbage is dropped rather than discarding the whole frame.
    """
    objects: list[dict[str, Any]] = []
    index = 0
    length = len(raw)

    while index < length:
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            # raw_decode returns an *absolute* offset into `raw`, not a relative one.
            obj, index = _DECODER.raw_decode(raw, index)
        except ValueError:
            _LOGGER.debug("Discarding undecodable remainder of frame: %s", raw[index:])
            break
        if isinstance(obj, dict):
            objects.append(obj)

    return objects


class SiegeniaClient:
    """Maintains a persistent, authenticated session with one SIEGENIA device."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 443,
        use_tls: bool = True,
        on_push: Callable[[dict[str, Any]], None] | None = None,
        on_availability: Callable[[bool], None] | None = None,
    ) -> None:
        """Initialise the client without opening a connection."""
        self._session = session
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._use_tls = use_tls
        self._on_push = on_push
        self._on_availability = on_availability

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = count(1)
        self._command_lock = asyncio.Lock()

        self._receiver: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._stopping = False
        self._logged_in = False

    def set_callbacks(
        self,
        *,
        on_push: Callable[[dict[str, Any]], None] | None = None,
        on_availability: Callable[[bool], None] | None = None,
    ) -> None:
        """Attach the consumer's callbacks after construction.

        The coordinator needs the client to exist before it can offer its own
        handlers, so they are wired up in a second step.
        """
        self._on_push = on_push
        self._on_availability = on_availability

    @property
    def url(self) -> str:
        """Return the WebSocket URL. The path is case-sensitive."""
        scheme = "wss" if self._use_tls else "ws"
        return f"{scheme}://{self._host}:{self._port}/WebSocket"

    @property
    def connected(self) -> bool:
        """Return whether an authenticated session is currently established."""
        return self._logged_in and self._ws is not None and not self._ws.closed

    async def async_probe(self) -> dict[str, Any]:
        """Return `getDevice` output without authenticating.

        The device answers `getDevice` before login, which lets the config flow
        identify the hardware and reject non-door models before asking the user
        for credentials.
        """
        await self._async_open()
        try:
            return await self.async_command("getDevice")
        finally:
            await self._async_close()

    async def async_start(self) -> None:
        """Connect and authenticate, then supervise the session in the background.

        Raises if the initial connection fails, so that setup can surface a
        precise error. Once established, drops are retried indefinitely.
        """
        self._stopping = False
        await self._async_connect()
        self._supervisor = asyncio.create_task(self._async_supervise())

    async def async_stop(self) -> None:
        """Tear down the session and stop reconnecting."""
        self._stopping = True
        self._closed.set()

        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None

        await self._async_close()

    async def async_command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a command and return the `data` object from its response.

        Commands are serialised: the device mishandles overlapping writes, and
        its actuators need real time between them.
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise SiegeniaConnectionError("Not connected to the device")

        message_id = next(self._ids)
        payload: dict[str, Any] = {"command": command, "id": message_id}
        if params is not None:
            payload["params"] = params
        if extra is not None:
            payload.update(extra)

        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )

        async with self._command_lock:
            self._pending[message_id] = future
            try:
                await ws.send_str(json.dumps(payload))
                async with asyncio.timeout(RESPONSE_TIMEOUT):
                    response = await future
            except TimeoutError as err:
                raise SiegeniaConnectionError(
                    f"Timed out waiting for a response to {command!r}"
                ) from err
            except aiohttp.ClientError as err:
                raise SiegeniaConnectionError(
                    f"Failed to send {command!r}: {err}"
                ) from err
            finally:
                self._pending.pop(message_id, None)

        status = response.get("status")
        if status == "ok":
            data = response.get("data")
            return data if isinstance(data, dict) else {}
        if status == _SESSION_OCCUPIED:
            raise SiegeniaSessionOccupied(
                "Another client is already logged in with this account"
            )
        if status in _AUTH_STATUSES:
            raise SiegeniaAuthError(f"Device rejected {command!r}: {status}")
        raise SiegeniaCommandError(command, status)

    async def async_get_device(self) -> dict[str, Any]:
        """Return static device information: type, variant, serial, versions."""
        return await self.async_command("getDevice")

    async def async_get_device_params(self) -> dict[str, Any]:
        """Return the current mutable device state."""
        return await self.async_command("getDeviceParams")

    async def async_set_device_params(self, params: dict[str, Any]) -> None:
        """Apply a partial parameter update.

        A successful reply means the device *accepted* the change, not that the
        physical operation finished. Reading state back immediately will still
        return the old value.
        """
        await self.async_command("setDeviceParams", params)

    async def _async_connect(self) -> None:
        """Open a connection and authenticate on it."""
        await self._async_open()
        try:
            await self._async_login()
        except Exception:
            await self._async_close()
            raise

        self._keepalive = asyncio.create_task(self._async_keepalive_loop())
        self._notify_availability(True)

    async def _async_open(self) -> None:
        """Open the socket and start reading from it.

        The receive loop must be running before anything is sent, or the first
        response is lost. No WebSocket-level heartbeat is configured: the device
        is not reliably conformant, and `keepAlive` serves that purpose instead.
        """
        self._closed.clear()
        try:
            self._ws = await self._session.ws_connect(
                self.url,
                timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT),
                ssl=False,
                # Sent by every reference implementation; harmless, and some
                # firmware revisions appear to expect an Origin header.
                headers={"Origin": self.url.rsplit("/WebSocket", 1)[0]},
            )
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise SiegeniaConnectionError(
                f"Cannot connect to {self._host}: {err}"
            ) from err

        self._receiver = asyncio.create_task(self._async_receive_loop())

    async def _async_login(self) -> None:
        """Authenticate the open socket.

        Any non-ok status here is treated as an authentication failure: the set
        of statuses this device can return is not fully documented, and every
        observed failure mode on `login` has been credential-related.
        """
        try:
            await self.async_command(
                "login",
                extra={
                    "user": self._username,
                    "password": self._password,
                    "long_life": False,
                },
            )
        except SiegeniaCommandError as err:
            raise SiegeniaAuthError(
                f"Device rejected the credentials: {err.status}"
            ) from err

        self._logged_in = True

    async def _async_close(self) -> None:
        """Close the socket and cancel its helper tasks."""
        self._logged_in = False

        if self._keepalive is not None:
            self._keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive
            self._keepalive = None

        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(aiohttp.ClientError, OSError):
                await self._ws.close()

        if self._receiver is not None:
            self._receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receiver
            self._receiver = None

        self._ws = None
        self._fail_pending(SiegeniaConnectionError("Connection closed"))
        self._closed.set()

    async def _async_receive_loop(self) -> None:
        """Dispatch incoming frames until the connection ends."""
        ws = self._ws
        if ws is None:
            return

        try:
            async for message in ws:
                if message.type is aiohttp.WSMsgType.TEXT:
                    for obj in iter_json_objects(message.data):
                        self._handle_message(obj)
                elif message.type is aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug("WebSocket reported an error: %s", ws.exception())
                    break
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            # The device closes with a close frame that violates RFC 6455, which
            # surfaces here as a protocol error. That is an ordinary disconnect.
            _LOGGER.debug("Connection to %s ended: %s", self._host, err)
        finally:
            self._logged_in = False
            self._fail_pending(SiegeniaConnectionError("Connection closed"))
            self._closed.set()

    async def _async_keepalive_loop(self) -> None:
        """Hold the session open.

        The session expires after roughly a minute of silence. A failure here
        means the link is dead, so the socket is closed to trigger a reconnect.
        """
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                await self.async_command("keepAlive")
            except asyncio.CancelledError:
                raise
            except SiegeniaError as err:
                _LOGGER.debug("Keepalive to %s failed: %s", self._host, err)
                if self._ws is not None and not self._ws.closed:
                    with contextlib.suppress(aiohttp.ClientError, OSError):
                        await self._ws.close()
                return

    async def _async_supervise(self) -> None:
        """Reconnect whenever the session drops, with capped linear backoff."""
        while not self._stopping:
            await self._closed.wait()
            if self._stopping:
                return

            self._notify_availability(False)
            await self._async_close()

            # Retry here rather than falling back to the outer wait: a failed
            # attempt leaves the "closed" event cleared, so waiting on it again
            # would block until a connection that never opened drops.
            attempt = 0
            while not self._stopping:
                attempt += 1
                delay = min(attempt * 5 + 5, RECONNECT_MAX_BACKOFF)
                _LOGGER.debug(
                    "Reconnecting to %s in %s seconds (attempt %s)",
                    self._host,
                    delay,
                    attempt,
                )
                await asyncio.sleep(delay)
                if self._stopping:
                    return

                try:
                    await self._async_connect()
                except SiegeniaError as err:
                    _LOGGER.debug("Reconnect to %s failed: %s", self._host, err)
                    continue

                _LOGGER.debug("Reconnected to %s", self._host)
                break

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Route one decoded message to its waiter, or treat it as a push."""
        # Unsolicited frames and takeover notices both arrive as `id: -1`, and
        # are told apart only by their status. Outgoing ids start at 1, so -1
        # can never match a pending request and falls through to the push
        # handling below -- but only as long as the id counter stays positive.
        status = message.get("status")
        message_id = message.get("id")
        takeover = status == _SESSION_OCCUPIED

        # Deliver to a waiting caller first, even for a takeover: the device
        # usually announces one unsolicited with `id: -1`, but when it answers a
        # command instead, swallowing it here would leave that caller blocked
        # until its timeout rather than failing with the real reason.
        if isinstance(message_id, int):
            future = self._pending.pop(message_id, None)
            if future is not None and not future.done():
                future.set_result(message)
                if takeover:
                    self._handle_takeover()
                return

        if takeover:
            self._handle_takeover()
            return

        if status == "update" or message.get("command") in _PUSH_COMMANDS:
            data = message.get("data")
            if isinstance(data, dict) and self._on_push is not None:
                self._on_push(data)
            return

        _LOGGER.debug("Ignoring unmatched message from %s: %s", self._host, message)

    def _handle_takeover(self) -> None:
        """Give up the session after another client claimed the account.

        Dropping the socket lets the supervisor log back in, which reclaims the
        session from whichever client took it.
        """
        _LOGGER.warning(
            "Another client logged in to %s with user %r; Home Assistant will "
            "reconnect. Use a dedicated device account to avoid fighting over "
            "the session",
            self._host,
            self._username,
        )
        self._logged_in = False
        self._closed.set()

    def _fail_pending(self, error: Exception) -> None:
        """Resolve every in-flight request so no caller waits forever."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _notify_availability(self, available: bool) -> None:
        """Report a connection state change to the coordinator."""
        if self._on_availability is not None:
            self._on_availability(available)
