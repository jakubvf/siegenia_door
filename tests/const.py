"""Payloads captured from a real SIEGENIA door, reused across the test suite."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

HOST: Final = "192.168.1.50"
USERNAME: Final = "homeassistant"
PASSWORD: Final = "correct-horse"
SERIAL: Final = "020300160Cxxxx"
MAC: Final = "3c:71:bf:da:11:cc"
SYSTEM_NAME: Final = "Haustür"

USER_INPUT: Final[dict[str, Any]] = {
    CONF_HOST: HOST,
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
}

ENTRY_DATA: Final[dict[str, Any]] = {**USER_INPUT, CONF_PORT: 443}

# `getDevice` answers before login. Type 7 is ACS, the supported automatic door.
DEVICE_ACS: Final[dict[str, Any]] = {
    "type": 7,
    "variant": 3,
    "subvariant": 0,
    "serialnr": SERIAL,
    "systemname": SYSTEM_NAME,
    "softwareversion": "1.11.1.23",
    "hardwareversion": "24071201.24022601.21100401",
    "initialized": True,
}

# Type 6 is the MHS window drive family, which must be rejected.
DEVICE_MHS: Final[dict[str, Any]] = {**DEVICE_ACS, "type": 6, "variant": 1}

# Firmware before 1.11 reports day/night mode and a real sash position.
PARAMS_OLD_FIRMWARE: Final[dict[str, Any]] = {
    "daymode": False,
    "state": "CLOSED",
    "mac": MAC,
    "security": 1,
    "usercount": 4,
    "warnings": [],
}

# Firmware 1.11 and later drop `daymode` and report an undefined position.
PARAMS_NEW_FIRMWARE: Final[dict[str, Any]] = {
    "state": "UNDEFINED",
    "mac": MAC,
    "security": 1,
    "usercount": 4,
    "warnings": [],
}

LOCK_ENTITY_ID: Final = "lock.haustur"

# `getUser` output for a door with four users, shaped like the real thing but
# with invented names. Userid 0 is the built-in admin, which is the one user the
# door reports without `starttime`/`duration`.
#
# The `apid`s are deliberately not dense and not per-user: the door allocates
# them monotonically across all users, so alice's second credential is 3 while
# bob's only one is 5. A fixture that numbered them from zero per user would let
# a per-user allocation bug pass unnoticed.
USERS_ACS: Final[list[dict[str, Any]]] = [
    {
        "userid": 0,
        "username": "Admin",
        "usertype": 1,
        "isapp": True,
        "isdisabled": False,
        "keyless": True,
        "ap": [],
    },
    {
        "userid": 1,
        "username": "alice",
        "usertype": 2,
        "isapp": True,
        "isdisabled": False,
        "keyless": True,
        "starttime": 1699093719,
        "duration": 86400,
        "ap": [{"apid": 2, "aptype": 0}, {"apid": 3, "aptype": 10}],
    },
    {
        "userid": 2,
        "username": "bob",
        "usertype": 2,
        "isapp": False,
        "isdisabled": False,
        "keyless": False,
        "starttime": 1699093720,
        "duration": 86400,
        "ap": [{"apid": 5, "aptype": 0}],
    },
    {
        "userid": 3,
        "username": "carol",
        "usertype": 3,
        "isapp": False,
        "isdisabled": True,
        "keyless": False,
        "starttime": 1699093721,
        "duration": 86400,
        "ap": [],
    },
]
