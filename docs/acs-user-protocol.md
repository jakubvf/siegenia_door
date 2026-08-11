# ACS user and access-property protocol

Notes on the parts of the SIEGENIA local WebSocket protocol that manage door
users and their access properties (fingerprints, RFID tags, PIN codes). None of
this is implemented by the integration yet; the integration currently speaks only
`login`, `keepAlive`, `getDevice`, `getDeviceParams` and `setDeviceParams`.

### Provenance

Two sources, and it matters which is which:

* **Static.** The official **SIEGENIA Comfort 1.75.3** Android app, decompiled
  with jadx 1.5.6. This is where the command names and the enrollment state
  machine come from.
* **Live.** One door — type 7, variant 1, subvariant 1, firmware **1.9.1.23**,
  the same hardware the rest of this integration was built against. A full
  create → enroll → delete cycle was exercised against it, including a real
  fingerprint.

Everything marked *confirmed* was seen on that door. What remains unverified is
called out in the last section — chiefly the `…MultiUser` command family, which
this firmware does not implement at all.

The app is a plain Kotlin/Android app — the protocol is built in Java, not in the
bundled `libnative-si.so` (which is only a BoringSSL build). Message
construction lives in the class jadx names `p087v2/b.java`; response parsing
lives in one very large method in `com/siegenia/si_comfort/backend/H.java`, which
jadx cannot decompile normally. To read that method:

```bash
jadx -m fallback --single-class com.siegenia.si_comfort.backend.H \
     --single-class-output H_fallback.java com.siegenia.si_comfort.apk
```

### Wire format

Identical to what `api.py` already sends — *confirmed*. The app's message
factory builds:

```json
{"command": "<name>", "params": { ... }, "id": <int>}
```

with a process-wide incrementing `id` starting at 1, and `login` as the one
special case that carries `user`, `password` and `long_life` at the top level
rather than inside `params`. So `SiegeniaClient.async_command()` can send every
command below unchanged — this is a matter of adding methods, not of touching
the transport.

Two failure statuses beyond those `api.py` already knows, both *confirmed*:

| Status | Meaning |
| --- | --- |
| `not_existent` | The requested `userid` does not exist |
| `command_not_found` | This firmware does not implement the command at all |

Both surface through the existing `SiegeniaCommandError`, which carries the
status string, so no new exception plumbing is needed — but `not_existent` is a
normal, expected result when enumerating users and must not be logged as an
error.

### Command inventory

| Command | Purpose | Status |
| --- | --- | --- |
| `getUser` | Read one user, by `userid` | *confirmed* |
| `createUser` | Create a user | *confirmed* |
| `deleteUser` | Delete a user, by `userid` | *confirmed* |
| `setUser` | Modify a user | untested |
| `createAccessProperty` | Enroll a fingerprint, tag or PIN | *confirmed* |
| `deleteAccessProperty` | Remove an access property, by `apid` | *confirmed* |
| `getEnrollmentState` | Poll the enrollment state machine | *confirmed* |
| `getMultiUsers` / `setMultiUser` / `deleteMultiUser` | Newer-firmware user family | `command_not_found` here |
| `registerKeylessDevice` | Register a Bluetooth device by code | untested |
| `getPersistentEvents` | Read the door's access log | untested |
| `setPassword` | Change a user's password | untested |
| `getAcsDeviceIds`, `getAcsDeviceDetails`, `setAcsDeviceDetails` | Bus peripherals | untested |
| `setAdminDeviceParams` | Admin-only parameter writes | untested |

### Two user models

The app supports two mutually exclusive user APIs and picks between them per
device, as a ternary on an internal flag — `deleteMultiUser` vs `deleteUser`,
`setMultiUser` vs `createUser`, and so on. Missing this is easy: the command
names never appear as plain literals, only as ternary branches.

The discriminator is the device parameter **`acs_master`** (or `bus_master`):
when its value is the string `"io_smart"` the app uses the `…MultiUser` family,
otherwise the plain family. On firmware 1.9.1.23 *neither parameter is present*
in `getDeviceParams`, so the flag keeps its default of 0 and the plain family is
used. `getMultiUsers` returning `command_not_found` *confirms* this.

**In practice: absent `acs_master`/`bus_master` means the plain
`createUser` / `deleteUser` family.** Treat `…MultiUser` as the newer-firmware
branch — entirely unverified, and best left to fail loudly rather than guessed
at.

### Creating and deleting users

`createUser` — *confirmed*, including that it **returns the new `userid`**, so
there is no need to enumerate afterwards:

```json
{"command": "createUser", "params": {
  "username": "hatest",
  "password": "...",
  "starttime": 1786453124,
  "duration": 86400,
  "usertype": 2,
  "isdisabled": false,
  "isapp": false,
  "keyless": false}}

{"data": {"userid": 6}, "status": "ok"}
```

Every field is echoed back verbatim by a subsequent `getUser`. `isapp` and
`keyless` may both be `false`; this does **not** prevent fingerprint enrollment.

`deleteUser` takes `userid` and returns it — *confirmed*. It is not necessary to
remove a user's access properties first, though doing so is harmless:

```json
{"command": "deleteUser", "params": {"userid": 6}}
{"data": {"userid": 6}, "status": "ok"}
```

`starttime` and `duration` are Unix seconds and a *relative* length. The ACS
screens send a plain UTC epoch (`getTimeInMillis() / 1000`); only the non-ACS
`MultiUserDetailActivity` screens add a local-time offset, so **UTC** is correct
here.

`usertype` is an enum, sent as its integer value:

| Value | Type |
| --- | --- |
| 0 | not set |
| 1 | admin |
| 2 | user |
| 3 | one-time |
| 4 | interval |

Creating an admin forces `isdisabled: false`, `isapp: true`, `keyless: true`
regardless of what the UI shows.

### Reading users

`getDeviceParams` carries **`usercount`**, and user ids are dense from 0, so
enumeration is `getUser` for `userid` 0, 1, 2, … until `not_existent`.

`getUser` response, *confirmed*:

```json
{"data": {"userdetails": {
    "userid": 1,
    "username": "example",
    "usertype": 1,
    "isapp": true,
    "isdisabled": false,
    "keyless": true,
    "starttime": 1699093719,
    "duration": 86400,
    "ap": [{"apid": 2, "aptype": 0}, {"apid": 3, "aptype": 1}]}},
 "status": "ok"}
```

`starttime` and `duration` are absent on the built-in `Admin` (userid 0) and
present on every other user. Every ordinary user on the probed door had
`duration: 86400` despite being permanent, so do not read it as an expiry
without checking `usertype` — types 3 (one-time) and 4 (interval) are
presumably where it bites.

### Access properties

An access property is one credential slot belonging to a user. Two distinct
identifiers, both *confirmed*, and conflating them would delete the wrong
person's fingerprint:

* **`aptype`** — *which kind of slot*, from the fixed table below. Passed to
  `createAccessProperty`.
* **`apid`** — the instance id, allocated by the door, **globally unique across
  users rather than per user**, and monotonically increasing (a new enrollment
  took `apid` 9 when the existing maximum was 8). Passed to
  `deleteAccessProperty`.

| Slot | `aptype` |
| --- | --- |
| Fingerprints 1–4 | 0, 1, 2, 3 |
| RFID tags 1–3 | 10, 11, 12 |
| PIN code | 20 |
| Bluetooth device 1–3 | 30, 31, 32 |
| Bluetooth registration code 1–3 | 40, 41, 42 |
| Bluetooth transmitter 1–3 | 50, 51, 52 |
| App | 60 |

Adding a fingerprint means picking a free `aptype` for that user — read the
user's `ap` array and choose a fingerprint slot (0–3) not already in it.

`deleteAccessProperty` takes `apid` and returns it — *confirmed*:

```json
{"command": "deleteAccessProperty", "params": {"apid": 9}}
{"data": {"apid": 9}, "status": "ok"}
```

### Fingerprint enrollment

Enrollment is an asynchronous state machine on the door, not a single call.

1. Send `createAccessProperty` with `userid` and `aptype`. For a PIN code
   (`aptype` 20) also send the digits as `code`; fingerprints and tags send no
   payload, because the credential is captured at the door itself. The response
   is `{"data": {}, "status": "ok"}` — it carries **no `apid`**.
2. Poll `getEnrollmentState` **once per second**. It takes no parameters at all
   — sent with no `params` key, the way `keepAlive` is — *confirmed*.
3. On `FINISH`, re-read the user with `getUser`. This is the only way to learn
   the new `apid`.

Abort by sending `createAccessProperty` with `abort: true` and nothing else. It
returns `{"apid": 65535}` — `0xFFFF`, the "nothing was created" sentinel —
*confirmed*.

The state is a plain string under `data`:

```json
{"data": {"enrollmentstate": "NO_ENROLL_ACTIVE"}, "status": "ok"}
```

The app maps it to an internal integer, which is what its UI branches on:

| `enrollmentstate` | Internal | Meaning |
| --- | --- | --- |
| `START` | 1 | In progress |
| `ENROLL` | 2 | In progress — waiting for the finger |
| `SYNC` | 3 | In progress |
| `FINISH` | 4 | **Success** |
| `ABORT` | 5 | Terminal, failed |
| `NO_ENROLL_ACTIVE` | 6 | Terminal, nothing running |
| *(unrecognised)* | 0 | Terminal, treated as failure |

Only `FINISH` is success. A *confirmed* successful run:

```
 0.0s  START
 2.3s  ENROLL      <- door is waiting for the finger
20.4s  FINISH
```

Roughly twenty seconds with a cooperative user. `SYNC` did **not** appear on the
successful path; it was seen only on a run where no finger was ever presented,
where it showed up after about a minute of `ENROLL`. Treat `SYNC` as
"in progress" rather than as progress toward success.

Three things an implementation must get right:

* **`NO_ENROLL_ACTIVE` is the resting value**, so it cannot be treated as failure
  before anything has started, and may be observed briefly right after
  `createAccessProperty` before the door reaches `START`.
* **The door waits indefinitely.** Left alone in `ENROLL` it does not time out on
  its own within a minute, so the client owns the timeout and must send the
  abort. Budget well over twenty seconds; a minute is not generous.
* **Polling is mandatory.** `enrollmentstate` does not appear in
  `getDeviceParams` on this firmware, so the existing push handling cannot
  substitute. Ordinary `deviceParams` pushes do continue to arrive during
  enrollment and must not be mistaken for enrollment progress.

### Other useful device parameters

From `getDeviceParams`, relevant to a user-management UI:

| Key | Value seen | Use |
| --- | --- | --- |
| `usercount` | 6 | Bound for user enumeration |
| `pinlength` | 6 | Validate PIN length before `createAccessProperty` |
| `iskeylessenabled` | true | Whether the `keyless` user flag is meaningful |
| `security` | 3 | Unknown; possibly a security level |
| `commaster` | 0 | Unknown |
| `multiadminpwinit` | true (in `getDevice`) | Whether admin passwords are initialised |

### Open questions

* **The `io_smart` / `…MultiUser` branch** in its entirety — no access to such a
  door, and this firmware rejects the commands outright.
* **`setUser`** — never sent. Editing an existing user is therefore unverified,
  including whether it accepts partial updates the way `setMultiUser` appears to.
* **Whether `password` is optional** in `createUser` for a user who will only
  ever present a fingerprint, and what the door does with a weak or empty one.
* **Non-fingerprint enrollment** — RFID (`aptype` 10–12) and PIN (`aptype` 20,
  with `code`) follow the same state machine in the app but were not exercised.
* **Permissions.** Everything above was done as `admin`. Which commands a
  non-admin account may issue is untested, and this integration's README
  recommends configuring it with a dedicated account.
* **Whether `apid`s are ever reused** after deletion.
* **What the state becomes after `FINISH`** — whether the door settles back to
  `NO_ENROLL_ACTIVE` on its own was never observed, because the successful run
  stopped polling as soon as it saw `FINISH`. A client must therefore treat
  `FINISH` as the terminal success signal rather than waiting for a return to
  rest.
* **Whether `getUser` ever echoes `password` back.** Every other field sent to
  `createUser` came back verbatim, but the captured `userdetails` carries no
  `password` at all. Assume it is write-only.
