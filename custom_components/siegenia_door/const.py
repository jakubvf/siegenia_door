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

# `openclose` accepts a bare string on ACS (window drives take a per-sash map).
# Only OPEN is attested; an automatic door can be triggered but not driven shut.
OPENCLOSE_OPEN: Final = "OPEN"
