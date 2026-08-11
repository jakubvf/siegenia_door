"""Constants for the SIEGENIA Door integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "siegenia_door"

CONF_PORT: Final = "port"
CONF_USE_TLS: Final = "use_tls"

DEFAULT_PORT: Final = 443
DEFAULT_USE_TLS: Final = True

# Timings. The device is slow: a `setDeviceParams` reply means "accepted", not
# "done", and the physical cycle takes 5-10 seconds.
CONNECT_TIMEOUT: Final = 10
RESPONSE_TIMEOUT: Final = 10
KEEPALIVE_INTERVAL: Final = 10
RECONNECT_MAX_BACKOFF: Final = 60

# Poll cadence. ACS doors do push `state` changes unsolicited -- confirmed on
# firmware 1.9.1.23, which emitted four frames in sixty idle seconds as the door
# was used. Pushes are partial: a frame carries only the keys that changed, so
# they are merged into the cached state rather than replacing it. Polling stays
# as the backstop, relaxing once a push is seen and tightening mid-cycle.
#
# `daymode` changes are NOT pushed -- only `state` is -- so lock and unlock still
# depend on the poll, which is why the idle interval is not simply disabled.
POLL_INTERVAL_DEFAULT: Final = 5
POLL_INTERVAL_PUSH: Final = 60
POLL_INTERVAL_MOVING: Final = 2
PUSH_IDLE_TIMEOUT: Final = 60

# Device families, per the SIEGENIA protocol spec as mapped by ioBroker.siegenia.
# Only ACS is an automatic door; everything else is a window drive or a
# ventilation unit and must be rejected during the config flow.
DEVICE_TYPE_ACS: Final = 7

DEVICE_TYPES: Final[dict[int, str]] = {
    1: "AEROPAC",
    2: "AEROMAT VT",
    3: "DRIVE axxent Family",
    4: "SENSOAIR",
    5: "AEROVITAL",
    6: "MHS Family",
    7: "ACS",
    8: "AEROTUBE",
    9: "GENIUS B",
    10: "Universal Module",
    11: "enOcean Converter Module",
    12: "VT Upgrade",
    13: "DRIVE CL",
    14: "AEROPLUS",
}

# Door position, as reported in the flat `state` field of getDeviceParams.
#
# OPEN, CLOSED and CLOSED_NOT_LOCKED are all attested on firmware 1.9.1.23;
# UNDEFINED comes from a door with no sash sensor fitted. The enum combines leaf
# position with bolt status, so CLOSED means shut *and* bolted while
# CLOSED_NOT_LOCKED means shut with the bolt not thrown.
#
# UNDEFINED is a legitimate permanent value (the official SIEGENIA app shows no
# position for those doors either), so it must map to "unknown", never "closed".
STATE_OPEN: Final = "OPEN"
STATE_CLOSED: Final = "CLOSED"
STATE_CLOSED_NOT_LOCKED: Final = "CLOSED_NOT_LOCKED"
STATE_UNDEFINED: Final = "UNDEFINED"

# The enum is undocumented and this integration has already been surprised once
# by a value it had never seen, so position is derived from the prefix rather
# than from an exhaustive list. Any future CLOSED_* variant reads as closed
# instead of silently becoming unknown; anything unrecognised stays unknown.
STATE_PREFIX_OPEN: Final = "OPEN"
STATE_PREFIX_CLOSED: Final = "CLOSED"

# Parameter keys.
PARAM_DAYMODE: Final = "daymode"
PARAM_STATE: Final = "state"
PARAM_OPENCLOSE: Final = "openclose"
PARAM_MAC: Final = "mac"
PARAM_USERCOUNT: Final = "usercount"
PARAM_PINLENGTH: Final = "pinlength"
PARAM_ACS_MASTER: Final = "acs_master"
PARAM_BUS_MASTER: Final = "bus_master"

# `openclose` accepts a bare string on ACS (window drives take a per-sash map).
# Only OPEN is attested; an automatic door can be triggered but not driven shut.
OPENCLOSE_OPEN: Final = "OPEN"

# The official app picks between two mutually exclusive user APIs per device: the
# plain `createUser`/`deleteUser` family and a newer `...MultiUser` one. The
# discriminator is `acs_master`/`bus_master` carrying the string below. Firmware
# 1.9.1.23 reports neither parameter and rejects the MultiUser commands with
# `command_not_found`, so that branch is entirely unverified and is refused
# rather than guessed at.
ACS_MASTER_IO_SMART: Final = "io_smart"

# Returned by `getUser` and `deleteUser` for an id that holds no user. This is an
# ordinary result while enumerating, not a failure.
STATUS_NOT_EXISTENT: Final = "not_existent"

# `usertype`, sent to `createUser` as its integer value.
USERTYPE_NOT_SET: Final = 0
USERTYPE_ADMIN: Final = 1
USERTYPE_USER: Final = 2
USERTYPE_ONE_TIME: Final = 3
USERTYPE_INTERVAL: Final = 4

USERTYPES: Final[dict[int, str]] = {
    USERTYPE_NOT_SET: "Not set",
    USERTYPE_ADMIN: "Admin",
    USERTYPE_USER: "User",
    USERTYPE_ONE_TIME: "One-time",
    USERTYPE_INTERVAL: "Interval",
}

# An access property is one credential slot of a user. `aptype` says which slot
# it is and is chosen by the client; `apid` is the instance id the door
# allocates, globally unique across users rather than per user. Deleting takes
# the `apid`, so conflating the two would remove another person's credential.
APTYPE_FINGERPRINTS: Final[tuple[int, ...]] = (0, 1, 2, 3)
APTYPE_RFID_TAGS: Final[tuple[int, ...]] = (10, 11, 12)
APTYPE_PIN: Final = 20
APTYPE_BLUETOOTH_DEVICES: Final[tuple[int, ...]] = (30, 31, 32)
APTYPE_BLUETOOTH_CODES: Final[tuple[int, ...]] = (40, 41, 42)
APTYPE_BLUETOOTH_TRANSMITTERS: Final[tuple[int, ...]] = (50, 51, 52)
APTYPE_APP: Final = 60

APTYPE_NAMES: Final[dict[int, str]] = {
    0: "Fingerprint 1",
    1: "Fingerprint 2",
    2: "Fingerprint 3",
    3: "Fingerprint 4",
    10: "RFID tag 1",
    11: "RFID tag 2",
    12: "RFID tag 3",
    20: "PIN code",
    30: "Bluetooth device 1",
    31: "Bluetooth device 2",
    32: "Bluetooth device 3",
    40: "Bluetooth registration code 1",
    41: "Bluetooth registration code 2",
    42: "Bluetooth registration code 3",
    50: "Bluetooth transmitter 1",
    51: "Bluetooth transmitter 2",
    52: "Bluetooth transmitter 3",
    60: "App",
}

# 0xFFFF, returned by an aborted enrollment to say nothing was created.
APID_NONE: Final = 65535

# Enrollment runs as an asynchronous state machine on the door, polled with
# `getEnrollmentState`. Only FINISH is success; NO_ENROLL_ACTIVE is the resting
# value and may be seen briefly right after `createAccessProperty`, so it cannot
# be read as failure before the machine has been entered.
ENROLLMENT_START: Final = "START"
ENROLLMENT_ENROLL: Final = "ENROLL"
ENROLLMENT_SYNC: Final = "SYNC"
ENROLLMENT_FINISH: Final = "FINISH"
ENROLLMENT_ABORT: Final = "ABORT"
ENROLLMENT_NONE: Final = "NO_ENROLL_ACTIVE"

ENROLLMENT_IN_PROGRESS: Final[frozenset[str]] = frozenset(
    {ENROLLMENT_START, ENROLLMENT_ENROLL, ENROLLMENT_SYNC}
)

# The protocol mandates a one second poll. The door itself never times out -- it
# sat in ENROLL for over a minute with no finger presented -- so the client owns
# the deadline and must abort when it expires. A cooperative enrollment takes
# roughly twenty seconds.
ENROLLMENT_POLL_INTERVAL: Final = 1
ENROLLMENT_TIMEOUT: Final = 120

# User ids are dense from 0, but enumeration tolerates gaps, so it needs a bound
# for the case where `usercount` is unknown.
USER_ID_SCAN_LIMIT: Final = 64
