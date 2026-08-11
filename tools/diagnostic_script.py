#!/usr/bin/env python3
"""SIEGENIA door diagnostic script.

Standalone troubleshooting tool for the `siegenia_door` Home Assistant
integration. It talks to the door directly -- Home Assistant is not involved --
and prints one paste-able report describing exactly what the door supports.

Run it, then paste the whole output into a GitHub issue:

    pip install websocket-client
    python3 diagnostic_script.py --host 192.168.1.50 --username admin

The password is prompted for if it is not passed on the command line, and MAC
addresses, serial numbers and session tokens are partially redacted so the
report is safe to post publicly.

Protocol notes that shape this script (no public spec exists; all of this is
attested in captures of the official SIEGENIA Comfort App):

* The endpoint is `wss://<host>:443/WebSocket`. The path is case-sensitive.
* The certificate is self-signed, so TLS verification must be disabled.
* `getDevice` answers *before* login, so the device family can be reported even
  when the credentials turn out to be wrong.
* One frame may carry several concatenated JSON objects, which `json.loads`
  rejects outright; frames are therefore parsed incrementally.
* Only one session per account is allowed. A second client (usually the phone
  app) causes an unsolicited `{"id": -1, "status": "session_occupied"}`.
* The device closes with a close frame that violates RFC 6455, which makes
  `websocket-client` raise on close. That is a normal disconnect, not an error.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import ssl
import sys
from typing import Any

try:
    import websocket  # provided by the `websocket-client` package
except ImportError:  # pragma: no cover - trivial guard
    websocket = None  # type: ignore[assignment]

# =============================================================================
# PROTOCOL CONSTANTS
# =============================================================================

# Device families, per the SIEGENIA protocol as mapped by ioBroker.siegenia.
DEVICE_TYPES: dict[int, str] = {
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

# The KFV automatic door (the only thing this integration supports) is type 7.
DEVICE_TYPE_ACS = 7

# Type 6 is the closest look-alike and the most likely mistake: it is a window
# or door *drive*, and its parameters use a per-sash `states` object
# (`{"states": {"0": "CLOSED"}}`) instead of the flat scalar `state` that the
# ACS family reports. The two shapes are not interchangeable.
DEVICE_TYPE_MHS = 6

# Fields carried by getDevice. `mac` is NOT one of them -- it lives in
# getDeviceParams. An earlier version of this script looked for it here and
# reported a false "mac: MISSING" on every door.
DEVICE_FIELDS = (
    "type",
    "variant",
    "subvariant",
    "serialnr",
    "systemname",
    "softwareversion",
    "hardwareversion",
    "initialized",
    # Also present on firmware 1.9.1.23; listed so they are not reported as
    # unexpected extras on every door.
    "fallback",
    "firmware_update",
    "multiadminpwinit",
    "systemfloor",
    "systemlocation",
)

# Fields carried by getDeviceParams. `daymode` is deliberately absent from this
# list: whether it exists at all is the single most interesting finding, and it
# is reported separately as a capability rather than as a missing field.
PARAM_FIELDS = (
    "state",
    "mac",
    "security",
    "impulse",
    "impulseduration",
    "iskeylessenabled",
    "isvdsenabled",
    "vdsstatus",
    "usercount",
    "pinlength",
    "commaster",
    "timestamp",
    "timezone",
    "warnings",
    "systemname",
    "systemfloor",
    "systemlocation",
    "ip",
    "isdisabled",
    # Newer firmware only.
    "remote_access",
    "cn",
)

# Door position, as reported in the flat `state` field of getDeviceParams. The
# enum combines leaf position with bolt status: CLOSED is shut and bolted,
# CLOSED_NOT_LOCKED is shut with the bolt not thrown. All three of OPEN, CLOSED
# and CLOSED_NOT_LOCKED are attested on firmware 1.9.1.23.
STATE_OPEN = "OPEN"
STATE_CLOSED = "CLOSED"
STATE_CLOSED_NOT_LOCKED = "CLOSED_NOT_LOCKED"
STATE_UNDEFINED = "UNDEFINED"

KNOWN_STATES = (STATE_OPEN, STATE_CLOSED, STATE_CLOSED_NOT_LOCKED)

# Keys whose values are masked before anything is printed.
SECRET_KEYS = ("mac", "serialnr", "token", "cn")

# Exit codes.
EXIT_OK = 0
EXIT_CONNECT_FAILED = 1
EXIT_AUTH_FAILED = 2
EXIT_SESSION_OCCUPIED = 3

_DECODER = json.JSONDecoder()


class DiagnosticError(Exception):
    """A step of the diagnostic could not be completed."""


# =============================================================================
# FRAME PARSING
# =============================================================================


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
            break
        if isinstance(obj, dict):
            objects.append(obj)

    return objects


# =============================================================================
# REDACTION
# =============================================================================


def mask_value(value: Any, keep: int = 4) -> Any:
    """Mask all but the last `keep` characters of a string-ish value."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        return value
    text = str(value)
    if len(text) <= keep:
        return "*" * len(text)
    return "*" * (len(text) - keep) + text[-keep:]


def mask_mac(value: Any) -> Any:
    """Mask a MAC address, keeping the last two octets for identification."""
    if not isinstance(value, str):
        return value
    for separator in (":", "-"):
        if separator in value:
            octets = value.split(separator)
            if len(octets) > 2:
                return separator.join(["**"] * (len(octets) - 2) + octets[-2:])
            return value
    return mask_value(value, keep=4)


def redact(key: str, value: Any) -> Any:
    """Return `value` with secrets masked, based on its field name."""
    if key == "mac":
        return mask_mac(value)
    if key in SECRET_KEYS:
        return mask_value(value, keep=4)
    return value


def redact_payload(payload: Any) -> Any:
    """Recursively mask every secret field in a decoded response."""
    if isinstance(payload, dict):
        return {
            key: redact_payload(redact(key, value)) for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    return payload


# =============================================================================
# TRANSPORT
# =============================================================================


def connect(host: str, timeout: float) -> dict[str, Any]:
    """Open a TLS WebSocket to the door and return a session dictionary.

    The session carries the socket, the outgoing message counter and any
    messages received out of order, so responses can be matched by their id.
    """
    if websocket is None:
        raise DiagnosticError(
            "The 'websocket-client' package is not installed. "
            "Install it with: pip install websocket-client"
        )

    url = f"wss://{host}:443/WebSocket"
    try:
        ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
        ws.settimeout(timeout)
        ws.connect(url)
    except Exception as err:  # noqa: BLE001 - any failure here is fatal and reportable
        raise DiagnosticError(f"Could not connect to {url}: {err}") from err

    return {"ws": ws, "next_id": 1, "queue": [], "session_occupied": False}


def _receive_frame(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Read one frame and return every JSON object it contained."""
    try:
        raw = session["ws"].recv()
    except Exception as err:  # noqa: BLE001 - includes the non-RFC close frame
        raise DiagnosticError(
            f"Connection lost while waiting for a reply: {err}"
        ) from err

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        raise DiagnosticError("Connection closed by the device.")

    return iter_json_objects(raw)


def request(session: dict[str, Any], command: str, **extra: Any) -> dict[str, Any]:
    """Send one command and return the response whose id matches it.

    Unrelated messages are queued so a later request can still find them, and an
    unsolicited `session_occupied` is recorded on the session as it goes past.
    """
    message_id = session["next_id"]
    session["next_id"] += 1
    frame = {"command": command, **extra, "id": message_id}

    try:
        session["ws"].send(json.dumps(frame))
    except Exception as err:  # noqa: BLE001
        raise DiagnosticError(f"Could not send {command!r}: {err}") from err

    # The device answers promptly; a handful of frames is a generous allowance
    # for interleaved pushes before giving up on this command.
    for _ in range(20):
        for message in _receive_frame(session):
            if message.get("id") == -1 and message.get("status") == "session_occupied":
                session["session_occupied"] = True
                continue
            if message.get("id") == message_id:
                return message
            session["queue"].append(message)

    raise DiagnosticError(f"No reply to {command!r} after 20 frames.")


def login(session: dict[str, Any], username: str, password: str) -> dict[str, Any]:
    """Authenticate. Raises DiagnosticError with a specific reason on failure."""
    response = request(
        session,
        "login",
        user=username,
        password=password,
        long_life=False,
    )
    status = response.get("status")

    if status == "ok":
        return response.get("data") or {}
    if status == "session_occupied":
        session["session_occupied"] = True
        raise DiagnosticError("session_occupied")
    if status == "authentication_error":
        raise DiagnosticError("authentication_error")
    raise DiagnosticError(f"Login refused with status {status!r}: {response}")


def try_query(
    session: dict[str, Any], command: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Run one best-effort query. Returns (data, error) -- never raises.

    A failure of any single command must not abort the report; a door that
    refuses `getAcsDeviceIds` is still worth describing.
    """
    try:
        response = request(session, command)
    except DiagnosticError as err:
        return None, str(err)

    status = response.get("status")
    if status != "ok":
        return None, f"device returned status {status!r}"

    data = response.get("data")
    if not isinstance(data, dict):
        return None, f"unexpected payload: {data!r}"
    return data, None


def disconnect(session: dict[str, Any]) -> None:
    """Close the socket, tolerating the device's malformed close frame."""
    # The device violates RFC 6455 on close, which raises in websocket-client.
    with contextlib.suppress(Exception):
        session["ws"].close()


# =============================================================================
# ANALYSIS
# =============================================================================


def device_family_name(type_id: Any) -> str:
    """Return the human-readable family name for a getDevice `type` value."""
    if isinstance(type_id, bool) or not isinstance(type_id, int):
        return "unknown"
    return DEVICE_TYPES.get(type_id, f"unknown (type {type_id})")


def compatibility_verdict(device_data: dict[str, Any]) -> tuple[bool, list[str]]:
    """Judge whether this device is the family the integration supports.

    Returns (supported, lines) where `lines` are ready-to-print report lines.
    """
    type_id = device_data.get("type")
    name = device_family_name(type_id)
    variant = device_data.get("variant")
    subvariant = device_data.get("subvariant")

    lines = [
        f"Device family : {name} (type {type_id!r})",
        f"Variant       : {variant!r}  Subvariant: {subvariant!r}",
    ]

    if type_id == DEVICE_TYPE_ACS:
        lines.append("✅ This IS the supported family (ACS = KFV automatic door).")
        return True, lines

    if type_id == DEVICE_TYPE_MHS:
        lines.append("❌ This is NOT the supported family.")
        lines.append(
            "   Type 6 (MHS Family) is a window/door drive, not an automatic door. "
            'It reports a per-sash object ("states": {"0": "CLOSED"}) where the '
            'ACS family reports a flat "state" string, so the parameter shapes are '
            "incompatible and this integration cannot drive it."
        )
        return False, lines

    if type_id is None:
        lines.append(
            "❌ The device did not report a type. This is not a SIEGENIA device, "
            "or getDevice was answered by something else."
        )
        return False, lines

    lines.append("❌ This is NOT the supported family.")
    lines.append(
        f"   Only type {DEVICE_TYPE_ACS} (ACS) automatic doors are supported. "
        f"{name} is a ventilation unit or a drive and uses a different parameter shape."
    )
    return False, lines


def capability_findings(param_data: dict[str, Any]) -> list[str]:
    """Describe what this door can and cannot do, from its parameters.

    The two findings that matter are whether `daymode` exists (older firmware
    has it, newer firmware dropped it) and whether a door position sensor is
    fitted (`state` is permanently `UNDEFINED` when it is not).
    """
    lines: list[str] = []

    # --- Day/night mode, i.e. lock/unlock -----------------------------------
    if "daymode" in param_data:
        daymode = param_data["daymode"]
        meaning = "day mode / UNLOCKED" if daymode else "night mode / LOCKED"
        lines.append(
            f"✅ Day/night mode  : supported (daymode={daymode!r}, currently {meaning})"
        )
        lines.append("   Lock and unlock will work on this door.")
    else:
        lines.append(
            "ℹ️  Day/night mode  : NOT exposed by this firmware (no 'daymode' key)"
        )
        lines.append(
            "   This is a capability difference, not a fault. This door does not expose "
            "day/night mode, so lock/unlock will be unavailable; only the open/trigger "
            "function will work. Firmware around 1.11.x (variant 3) dropped the key that "
            "firmware around 1.1.x (variant 1) still reports."
        )

    # --- Door position sensor ------------------------------------------------
    if "state" not in param_data:
        lines.append(
            "❌ Door position   : no 'state' field at all -- unexpected for an ACS door."
        )
        return lines

    state = param_data["state"]
    if state == STATE_UNDEFINED:
        lines.append(
            f"ℹ️  Door position   : reported as {state!r} -- no door position sensor detected"
        )
        lines.append(
            "   This is normal and permanent on doors with no sash sensor fitted. The "
            "official SIEGENIA app shows no open/closed position for these doors either, "
            "so the integration reports the position as unknown rather than closed."
        )
    elif state in KNOWN_STATES:
        lines.append(f"✅ Door position   : sensor present, currently {state!r}")
        if state == STATE_CLOSED_NOT_LOCKED:
            lines.append(
                "   The leaf is shut but the bolt is not thrown. This device reports leaf "
                "position and bolt status in one field, so this is a normal value."
            )
    elif isinstance(state, dict):
        lines.append(f"❌ Door position   : reported as an object ({state!r}).")
        lines.append(
            "   That is the per-sash shape used by the MHS drive family, not the flat "
            "string an ACS door reports. Please include this in your issue."
        )
    else:
        lines.append(
            f"❓ Door position   : unrecognised value {state!r} -- please report this."
        )

    return lines


# =============================================================================
# REPORT
# =============================================================================


def section(title: str) -> None:
    """Print a section header."""
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def print_fields(data: dict[str, Any], known: tuple[str, ...]) -> None:
    """Print the known fields of a payload, then anything unexpected."""
    for field in known:
        if field in data:
            print(f"  ✅ {field}: {redact(field, data[field])!r}")
        else:
            print(f"  ⬜ {field}: not reported")

    extras = sorted(set(data) - set(known) - {"daymode"})
    if extras:
        print("\n  Extra fields this script did not expect (please report these):")
        for field in extras:
            print(f"  ❔ {field}: {redact(field, data[field])!r}")


def report_extra_queries(session: dict[str, Any]) -> dict[str, Any]:
    """Run the optional queries and print a short summary of each."""
    section("ADDITIONAL QUERIES (best effort)")
    results: dict[str, Any] = {}

    for command in ("getDeviceState", "getUserIds", "getAcsDeviceIds"):
        data, error = try_query(session, command)
        if error is not None:
            print(f"  ⚠️  {command}: {error}")
            continue
        results[command] = data
        print(f"  ✅ {command}: {json.dumps(redact_payload(data))}")

    print(
        "\n  Note: getUser and getProtocolData require an admin account and are not "
        "queried here to keep the report short."
    )
    return results


def print_raw_dump(label: str, payload: Any) -> None:
    """Print one redacted raw response for the maintainer."""
    print(f"\n--- {label} ---")
    print(json.dumps(redact_payload(payload), indent=2, sort_keys=True))


# =============================================================================
# MAIN
# =============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="diagnostic_script.py",
        description=(
            "Query a SIEGENIA door and print a paste-able compatibility report "
            "for the siegenia_door Home Assistant integration."
        ),
        epilog=(
            "Example: python3 diagnostic_script.py --host 192.168.1.50 --username admin\n"
            "Secrets (MAC, serial number, session token) are partially redacted, so the "
            "output is safe to paste into a public GitHub issue."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host", required=True, help="IP address or hostname of the door"
    )
    parser.add_argument(
        "--username", required=True, help="Door user name (as used in the SIEGENIA app)"
    )
    parser.add_argument(
        "--password",
        help="Door password. Omit this to be prompted, which keeps it out of your shell history.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Socket timeout in seconds (default: %(default)s). The script never blocks forever.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the full diagnostic and return a process exit code."""
    args = parse_args(argv)

    if websocket is None:
        print("❌ The 'websocket-client' package is not installed.")
        print("   Install it with:  pip install websocket-client")
        return EXIT_CONNECT_FAILED

    password = args.password
    if not password:
        password = getpass.getpass(f"Password for {args.username}@{args.host}: ")

    print("SIEGENIA door diagnostic report")
    print("=" * 70)
    print(f"Host: {args.host}   User: {args.username}   Timeout: {args.timeout}s")

    # --- Connect -------------------------------------------------------------
    section("1. CONNECTION")
    try:
        session = connect(args.host, args.timeout)
    except DiagnosticError as err:
        print(f"  ❌ {err}")
        print("\n  Check that the door is powered, on the same network, and reachable.")
        print("  The endpoint is wss://<host>:443/WebSocket (capital W and S).")
        return EXIT_CONNECT_FAILED
    print(
        f"  ✅ Connected to wss://{args.host}:443/WebSocket (TLS verification disabled;"
    )
    print("     the device uses a self-signed certificate).")

    device_data: dict[str, Any] = {}
    param_data: dict[str, Any] = {}
    supported = False
    exit_code = EXIT_OK

    try:
        # --- Device identity, before login ----------------------------------
        # getDevice is answered without authentication, so the family can be
        # reported even when the credentials turn out to be wrong.
        section("2. DEVICE IDENTITY (getDevice, no login required)")
        data, error = try_query(session, "getDevice")
        if error is not None:
            print(f"  ❌ getDevice failed: {error}")
        else:
            device_data = data
            print_fields(device_data, DEVICE_FIELDS)

        section("3. COMPATIBILITY VERDICT")
        supported, verdict_lines = compatibility_verdict(device_data)
        for line in verdict_lines:
            print(f"  {line}")

        # --- Login ------------------------------------------------------------
        section("4. AUTHENTICATION")
        try:
            login_data = login(session, args.username, password)
        except DiagnosticError as err:
            reason = str(err)
            if reason == "authentication_error":
                print(
                    "  ❌ Credentials rejected by the door (status: authentication_error)."
                )
                print(
                    "     The connection itself was fine -- the user name or password is wrong."
                )
                print("     Use the same credentials as the SIEGENIA Comfort App.")
                return EXIT_AUTH_FAILED
            if reason == "session_occupied" or session["session_occupied"]:
                print("  ❌ Another client already holds a session for this account.")
                print(
                    "     The door allows only ONE session per user. Close the SIEGENIA app"
                )
                print(
                    "     (or log it out) and try again, or create a dedicated door user"
                )
                print("     for Home Assistant.")
                return EXIT_SESSION_OCCUPIED
            print(f"  ❌ Login failed: {reason}")
            return EXIT_AUTH_FAILED

        print("  ✅ Login successful.")
        print(
            f"     admin rights: {login_data.get('isadmin')!r}   user id: {login_data.get('userid')!r}"
        )
        if session["session_occupied"]:
            print(
                "  ⚠️  A 'session_occupied' notice also arrived: another client (usually the"
            )
            print("     SIEGENIA phone app) is competing for this account.")

        # --- Parameters --------------------------------------------------------
        section("5. DEVICE PARAMETERS (getDeviceParams)")
        data, error = try_query(session, "getDeviceParams")
        if error is not None:
            print(f"  ❌ getDeviceParams failed: {error}")
        else:
            param_data = data
            print_fields(param_data, PARAM_FIELDS)

        # --- Capabilities -------------------------------------------------------
        section("6. CAPABILITY FINDINGS")
        if param_data:
            for line in capability_findings(param_data):
                print(f"  {line}")
        else:
            print(
                "  ⚠️  No parameters were returned, so capabilities cannot be determined."
            )

        # --- Extras ------------------------------------------------------------
        extras = report_extra_queries(session)

        # --- Raw dump ----------------------------------------------------------
        section("7. RAW RESPONSES (for the maintainer)")
        print_raw_dump("getDevice", device_data)
        print_raw_dump("getDeviceParams", param_data)
        for command, payload in extras.items():
            print_raw_dump(command, payload)
        if session["queue"]:
            # Anything the device sent that was not a reply we asked for, e.g. a
            # `deviceParams` push. Worth seeing: pushes are unconfirmed for ACS.
            print_raw_dump("unsolicited messages", session["queue"])

    finally:
        disconnect(session)

    # --- Summary ---------------------------------------------------------------
    section("DIAGNOSTIC COMPLETE")
    if supported:
        print("  Device family is supported by this integration.")
    else:
        print("  Device family is NOT supported by this integration -- see section 3.")
    print("  Please paste this entire report into your GitHub issue.")
    print()
    print(
        "  Redacted for safety: MAC address (last two octets kept), serial number and"
    )
    print("  certificate name (last 4 characters kept), and the session token. Your")
    print(
        "  password was never printed or stored. Everything else is shown verbatim, so"
    )
    print("  remove the room/floor names yourself if you consider them private.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
