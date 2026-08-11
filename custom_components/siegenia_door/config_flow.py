"""Config and options flows for the SIEGENIA Door integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from time import monotonic
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    UnknownEntry,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    SiegeniaAuthError,
    SiegeniaClient,
    SiegeniaError,
    SiegeniaSessionOccupied,
    uses_multi_user_model,
)
from .const import (
    APTYPE_FINGERPRINTS,
    APTYPE_NAMES,
    CONF_USE_TLS,
    DEFAULT_PORT,
    DEFAULT_USE_TLS,
    DEVICE_TYPE_ACS,
    DEVICE_TYPES,
    DOMAIN,
    ENROLLMENT_FINISH,
    ENROLLMENT_IN_PROGRESS,
    ENROLLMENT_NONE,
    ENROLLMENT_POLL_INTERVAL,
    ENROLLMENT_TIMEOUT,
    PARAM_MAC,
    PARAM_USERCOUNT,
    USERTYPE_NOT_SET,
    USERTYPE_USER,
    USERTYPES,
)
from .coordinator import SiegeniaConfigEntry

_LOGGER = logging.getLogger(__name__)

# Form fields of the options flow. They are named after the protocol fields they
# carry so that the schema, the door request and the translation key all agree.
CONF_APID = "apid"
CONF_APTYPE = "aptype"
CONF_USERID = "userid"
CONF_USERTYPE = "usertype"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


STEP_ADD_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_USERTYPE, default=str(USERTYPE_USER)): SelectSelector(
            SelectSelectorConfig(
                # "Not set" is a value the door reports but that nothing should
                # deliberately create, so it is offered nowhere.
                options=[
                    SelectOptionDict(value=str(value), label=label)
                    for value, label in USERTYPES.items()
                    if value != USERTYPE_NOT_SET
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)


class WrongDeviceType(Exception):
    """The host is a SIEGENIA device, but not an automatic door."""

    def __init__(self, device_type: int | None) -> None:
        """Record the reported device family."""
        super().__init__(device_type)
        self.name = DEVICE_TYPES.get(device_type, f"type {device_type}")


class EnrollmentFailed(Exception):
    """An enrollment ended without reaching FINISH."""

    def __init__(self, reason: str) -> None:
        """Record the abort reason the flow should show for this failure."""
        super().__init__(reason)
        self.reason = reason


class SiegeniaDoorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-facing setup of a SIEGENIA door."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: SiegeniaConfigEntry,
    ) -> SiegeniaDoorOptionsFlow:
        """Return the flow that manages the door's users.

        The entry is deliberately dropped: `config_entry` is a read-only
        property on `OptionsFlow` and is unavailable until the flow is
        initialised anyway.
        """
        return SiegeniaDoorOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect connection details and verify them against the device."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await self._async_validate(user_input)
            except WrongDeviceType as err:
                errors["base"] = "wrong_device_type"
                placeholders["device_type"] = err.name
            except SiegeniaSessionOccupied:
                errors["base"] = "session_occupied"
            except SiegeniaAuthError:
                errors["base"] = "invalid_auth"
            except SiegeniaError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error setting up a SIEGENIA door")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured(updates=dict(user_input))
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Begin re-authentication after the device rejected the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect fresh credentials for an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            candidate = {**entry.data, **user_input}
            try:
                await self._async_validate(candidate)
            except SiegeniaSessionOccupied:
                errors["base"] = "session_occupied"
            except SiegeniaAuthError:
                errors["base"] = "invalid_auth"
            except (SiegeniaError, WrongDeviceType):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reauthenticating a SIEGENIA door")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data=candidate)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_SCHEMA, {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            errors=errors,
            description_placeholders={CONF_HOST: entry.data[CONF_HOST]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user correct the host, port or credentials of an entry."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                info = await self._async_validate(user_input)
            except WrongDeviceType as err:
                errors["base"] = "wrong_device_type"
                placeholders["device_type"] = err.name
            except SiegeniaSessionOccupied:
                errors["base"] = "session_occupied"
            except SiegeniaAuthError:
                errors["base"] = "invalid_auth"
            except SiegeniaError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reconfiguring a SIEGENIA door")
                errors["base"] = "unknown"
            else:
                # Keep the entry pointed at the same physical door.
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or entry.data
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Confirm the host is a door we can log in to, and identify it.

        `getDevice` answers before authentication, so the device family is
        checked first: pointing the integration at a ventilation unit or a window
        drive fails with a useful message instead of a credentials error.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        client = SiegeniaClient(
            session,
            user_input[CONF_HOST],
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
            port=user_input.get(CONF_PORT, DEFAULT_PORT),
            use_tls=user_input.get(CONF_USE_TLS, DEFAULT_USE_TLS),
        )

        device = await client.async_probe()
        if (device_type := device.get("type")) != DEVICE_TYPE_ACS:
            raise WrongDeviceType(device_type)

        await client.async_start()
        try:
            params = await client.async_get_device_params()
        finally:
            await client.async_stop()

        serial = device.get("serialnr")
        mac = params.get(PARAM_MAC)
        unique_id = serial or (format_mac(mac) if mac else None)
        if unique_id is None:
            # Nothing stable to key on; fall back to the address the user gave.
            unique_id = (
                f"{user_input[CONF_HOST]}:{user_input.get(CONF_PORT, DEFAULT_PORT)}"
            )

        title = (
            device.get("systemname")
            or params.get("systemname")
            or f"SIEGENIA Door ({user_input[CONF_HOST]})"
        )

        return {"unique_id": unique_id, "title": title}


class SiegeniaDoorOptionsFlow(OptionsFlow):
    """Manage the users the door itself stores, and their credentials.

    Users live on the door rather than in `entry.options`, so nothing here
    writes options: every step ends by handing the door a command and finishing
    the flow with an empty, listener-free `async_create_entry`.

    The flow deliberately defines no `__init__` -- `config_entry` is a read-only
    property and the state below is scalar, so class-level defaults are enough
    and cannot leak between flows.
    """

    _userid: int | None = None
    _username: str = ""
    _aptype: int | None = None
    _task: asyncio.Task[None] | None = None
    _error: str | None = None
    # True from the moment the door accepts `createAccessProperty` until either
    # the enrollment finishes or an abort has been handed back to it.
    _enrolling: bool = False

    @property
    def _client(self) -> SiegeniaClient:
        """Return the running client.

        Read from `runtime_data` on every use: it is deleted when the entry
        unloads, and the door permits a single session per account, so building
        a second client here would evict the coordinator's.
        """
        return self.config_entry.runtime_data.client

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the choice between adding a user and managing one."""
        try:
            coordinator = self.config_entry.runtime_data
        except AttributeError:
            return self.async_abort(reason="entry_not_loaded")

        if uses_multi_user_model(coordinator.data or {}):
            # The `...MultiUser` command family is entirely unverified; guessing
            # at it could create or delete the wrong thing on someone's door.
            return self.async_abort(reason="unsupported_user_model")

        return self.async_show_menu(
            step_id="init", menu_options=["add_user", "select_user"]
        )

    async def async_step_add_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a user on the door."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._client.async_create_user(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    usertype=int(user_input[CONF_USERTYPE]),
                )
            except SiegeniaError as err:
                _LOGGER.debug("Creating a door user failed: %s", err)
                errors["base"] = "create_user_failed"
            else:
                return await self.async_step_init()

        # The form is never pre-filled: a rejected attempt must not echo the
        # password back into the dialog.
        return self.async_show_form(
            step_id="add_user", data_schema=STEP_ADD_USER_SCHEMA, errors=errors
        )

    async def async_step_select_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which of the door's users to manage."""
        if user_input is not None:
            self._userid = int(user_input[CONF_USERID])
            return await self.async_step_user_menu()

        try:
            coordinator = self.config_entry.runtime_data
            users = await coordinator.client.async_list_users(
                (coordinator.data or {}).get(PARAM_USERCOUNT)
            )
        except SiegeniaError as err:
            _LOGGER.debug("Listing door users failed: %s", err)
            return self.async_abort(reason="cannot_connect")

        if not users:
            return self.async_abort(reason="no_users")

        return self.async_show_form(
            step_id="select_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=str(user["userid"]),
                                    label=_user_label(user),
                                )
                                for user in users
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_user_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer what can be done to the selected user."""
        try:
            user = await self._async_selected_user()
        except SiegeniaError as err:
            _LOGGER.debug("Reading a door user failed: %s", err)
            return self.async_abort(reason="cannot_connect")

        if user is None:
            return self.async_abort(reason="user_gone")

        return self.async_show_menu(
            step_id="user_menu",
            menu_options=["enroll_fingerprint", "remove_credential", "delete_user"],
            description_placeholders={"username": self._username},
        )

    async def async_step_enroll_fingerprint(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a free fingerprint slot and start the door enrolling it."""
        try:
            user = await self._async_selected_user()
        except SiegeniaError as err:
            _LOGGER.debug("Reading a door user failed: %s", err)
            return self.async_abort(reason="cannot_connect")

        if user is None:
            return self.async_abort(reason="user_gone")

        taken = {ap.get("aptype") for ap in user.get("ap") or []}
        free = [slot for slot in APTYPE_FINGERPRINTS if slot not in taken]
        if not free:
            return self.async_abort(reason="no_free_fingerprint_slots")

        errors: dict[str, str] = {}

        if user_input is not None:
            # The slots were read again above, so a slot that someone else took
            # while this form was open is caught rather than enrolled over.
            if (slot := int(user_input[CONF_APTYPE])) in free:
                return await self._async_start_enrollment(slot)
            errors[CONF_APTYPE] = "slot_taken"

        return self.async_show_form(
            step_id="enroll_fingerprint",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APTYPE, default=str(free[0])): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=str(slot), label=APTYPE_NAMES[slot]
                                )
                                for slot in free
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={"username": self._username},
        )

    async def async_step_enroll(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the door's progress while the enrollment poll runs."""
        if self._task is None:
            # Started lazily and kept: the flow manager compares progress tasks
            # by identity and orphans -- rather than cancels -- any it is handed
            # a second time.
            self._task = self.hass.async_create_task(
                self._async_run_enrollment(),
                name="SIEGENIA door fingerprint enrollment",
                # Eager start would let a fast poll finish before the check
                # below, so the operator would never see the instruction.
                eager_start=False,
            )

        if not self._task.done():
            return self.async_show_progress(
                step_id="enroll",
                progress_action="enrolling",
                progress_task=self._task,
                description_placeholders={
                    "username": self._username,
                    "slot": APTYPE_NAMES.get(self._aptype, "the fingerprint"),
                },
            )

        # A progress step may only move to progress or progress-done, and an
        # exception raised here pins the dialog at "in progress" forever, so the
        # outcome is stashed and turned into an abort by a step of its own.
        try:
            self._task.result()
        except EnrollmentFailed as err:
            self._error = err.reason
        except asyncio.CancelledError:
            self._error = "enrollment_failed"
        except Exception:
            _LOGGER.exception("Unexpected error enrolling a fingerprint")
            self._error = "unknown"

        if self._error is not None:
            return self.async_show_progress_done(next_step_id="enroll_failed")
        return self.async_show_progress_done(next_step_id="enroll_done")

    async def async_step_enroll_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish after the door reported FINISH."""
        return self._async_finish()

    async def async_step_enroll_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Report why an enrollment did not complete."""
        return self.async_abort(reason=self._error or "enrollment_failed")

    async def async_step_remove_credential(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete one of the selected user's access properties."""
        if user_input is not None:
            try:
                await self._client.async_delete_access_property(
                    int(user_input[CONF_APID])
                )
            except SiegeniaError as err:
                _LOGGER.debug("Deleting an access property failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            return self._async_finish()

        try:
            user = await self._async_selected_user()
        except SiegeniaError as err:
            _LOGGER.debug("Reading a door user failed: %s", err)
            return self.async_abort(reason="cannot_connect")

        if user is None:
            return self.async_abort(reason="user_gone")

        if not (properties := user.get("ap") or []):
            return self.async_abort(reason="no_credentials")

        return self.async_show_form(
            step_id="remove_credential",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APID): SelectSelector(
                        SelectSelectorConfig(
                            # The value is the `apid`, not the `aptype`: the
                            # door allocates ids globally, so deleting by slot
                            # number would hit another user's credential.
                            options=[
                                SelectOptionDict(
                                    value=str(ap["apid"]),
                                    label=APTYPE_NAMES.get(
                                        ap.get("aptype"),
                                        f"Slot {ap.get('aptype')}",
                                    ),
                                )
                                for ap in properties
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            description_placeholders={"username": self._username},
        )

    async def async_step_delete_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm, then delete the selected user and its credentials."""
        if user_input is not None:
            try:
                await self._client.async_delete_user(self._userid)  # type: ignore[arg-type]
            except SiegeniaError as err:
                _LOGGER.debug("Deleting a door user failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            return self._async_finish()

        return self.async_show_form(
            step_id="delete_user",
            data_schema=vol.Schema({}),
            description_placeholders={"username": self._username},
        )

    @callback
    def async_remove(self) -> None:
        """Leave no enrollment running when the dialog is closed mid-poll.

        The manager cancels the progress task *before* calling this, so a
        `finally:` inside the worker cannot be relied on to reach the door. This
        also runs on normal completion, hence the flag.
        """
        if not self._enrolling:
            return
        self._async_set_enrolling(False)

        try:
            client = self._client
        except (UnknownEntry, AttributeError):
            # The entry was removed or unloaded first; its session is gone with
            # it, and the door drops the enrollment when the socket closes.
            return

        self.hass.async_create_task(
            _async_abort_at_door(client), name="SIEGENIA door enrollment abort"
        )

    async def _async_start_enrollment(self, aptype: int) -> ConfigFlowResult:
        """Ask the door to begin capturing a fingerprint for a slot."""
        client = self._client

        try:
            state = await client.async_get_enrollment_state()

            # A second Home Assistant dialog or the phone app may already have
            # the door waiting for a finger. Starting another would abort theirs.
            if state in ENROLLMENT_IN_PROGRESS:
                return self.async_abort(reason="enrollment_already_running")

            if state is not None and state != ENROLLMENT_NONE:
                # Defensive, not observed: firmware 1.9.1.23 always settles back
                # to NO_ENROLL_ACTIVE, both after a FINISH and after an abort.
                # But the poll below takes the first FINISH it sees as this
                # run's success, so a firmware that left a terminal state
                # lying around would make it report success in under a second
                # without the reader having been touched. Clearing it first
                # costs one command and removes that whole class of failure.
                await client.async_abort_enrollment()
                if await client.async_get_enrollment_state() != ENROLLMENT_NONE:
                    # Refuse rather than poll against a baseline that cannot be
                    # trusted to distinguish this enrollment from the last one.
                    return self.async_abort(reason="enrollment_not_idle")

            # Flagged before the command lands, because the door stops
            # answering anything else the moment it does.
            self._async_set_enrolling(True)
            await client.async_create_access_property(self._userid, aptype)  # type: ignore[arg-type]
        except SiegeniaError as err:
            self._async_set_enrolling(False)
            _LOGGER.debug("Starting a fingerprint enrollment failed: %s", err)
            return self.async_abort(reason="cannot_connect")

        self._aptype = aptype
        return await self.async_step_enroll()

    @callback
    def _async_set_enrolling(self, enrolling: bool) -> None:
        """Record that an enrollment is running, here and on the coordinator.

        The coordinator needs to know so it can ride out the poll failures the
        door produces throughout, instead of reporting the door as unavailable
        for as long as a credential takes to capture.
        """
        self._enrolling = enrolling
        with contextlib.suppress(UnknownEntry, AttributeError):
            self.config_entry.runtime_data.enrollment_active = enrolling

    async def _async_run_enrollment(self) -> None:
        """Poll the enrollment to its end, aborting at the door if it fails."""
        try:
            await self._async_poll_enrollment()
        except EnrollmentFailed:
            # The door waits for a finger indefinitely, so every unsuccessful
            # exit owes it an abort or the slot stays half open.
            self._async_set_enrolling(False)
            await _async_abort_at_door(self._client)
            raise

        self._async_set_enrolling(False)

    async def _async_poll_enrollment(self) -> None:
        """Follow the door's enrollment state machine until it settles."""
        started = False
        last_state: str | None = None
        deadline = monotonic() + ENROLLMENT_TIMEOUT

        while (remaining := deadline - monotonic()) > 0:
            await asyncio.sleep(ENROLLMENT_POLL_INTERVAL)

            try:
                state = await self._client.async_get_enrollment_state()
            except SiegeniaError as err:
                raise EnrollmentFailed("cannot_connect") from err

            if state != last_state:
                # The whole run is four or five transitions, so logging each one
                # is cheap and is the only record of what the door actually did.
                _LOGGER.debug("Enrollment state: %s -> %s", last_state, state)
                last_state = state

            if state == ENROLLMENT_FINISH:
                # Trustworthy only because the machine was proven to be at rest
                # before `createAccessProperty`; a FINISH here cannot be a
                # leftover from an earlier enrollment.
                return
            if state in ENROLLMENT_IN_PROGRESS:
                started = True
            elif started or state != ENROLLMENT_NONE:
                # NO_ENROLL_ACTIVE is the resting value and may be seen briefly
                # before the door reaches START, so it only means failure once
                # the machine has been entered. Anything else is terminal.
                raise EnrollmentFailed("enrollment_failed")

            self.async_update_progress(1 - remaining / ENROLLMENT_TIMEOUT)

        raise EnrollmentFailed("enrollment_timeout")

    async def _async_selected_user(self) -> dict[str, Any] | None:
        """Re-read the selected user, so slots and credentials stay current."""
        if self._userid is None:
            return None

        user = await self._client.async_get_user(self._userid)
        if user is not None:
            self._username = str(user.get("username") or self._userid)
        return user

    @callback
    def _async_finish(self) -> ConfigFlowResult:
        """Close the flow without touching the entry.

        The options are written back unchanged, which `async_update_entry`
        recognises as a no-op: there is no update listener and nothing to
        reload, because the users live on the door.
        """
        return self.async_create_entry(title="", data=dict(self.config_entry.options))


def _user_label(user: dict[str, Any]) -> str:
    """Describe one door user for a dropdown, without leaking its password."""
    username = user.get("username") or f"User {user['userid']}"
    usertype = USERTYPES.get(user.get("usertype"), "Unknown type")
    return f"{username} ({usertype})"


async def _async_abort_at_door(client: SiegeniaClient) -> None:
    """Cancel a running enrollment, tolerating a door that has gone away."""
    with contextlib.suppress(SiegeniaError):
        await client.async_abort_enrollment()
