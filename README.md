# SIEGENIA Door

Home Assistant custom integration for SIEGENIA automatic doors (KFV, ACS family).

Exposes your door as a [lock entity](https://www.home-assistant.io/integrations/lock/):
lock and unlock it by switching night/day mode, and trigger the door opener.

---

### Features

* Lock entity with lock, unlock and latch release.
* **Door user management**: add and remove the users stored on the door, and
  enrol fingerprints, without reaching for the SIEGENIA app.
* Live door status: the door pushes position changes over a persistent
  connection, so they appear without waiting for the next poll.
* Reconnects on its own if the door or your network drops out.
* Config flow with re-authentication and reconfiguration, so credential changes
  never require editing YAML or removing the integration.
* Downloadable diagnostics, with identifiers redacted, for bug reports.

### Requirements

* The door must be reachable on your local network (Wi-Fi or wired).
* You need its IP address and the credentials of a user account configured on it.
* Home Assistant 2026.3.0 or newer.

> **Use a dedicated door account for Home Assistant.**
> The door allows only **one session per user account**. If Home Assistant signs
> in with the same account as the SIEGENIA Comfort app, the two will keep kicking
> each other off. Create a second user on the door and give it to Home Assistant.

### Installation

**HACS**

1. HACS → three-dot menu → Custom repositories
2. Add `https://github.com/jakubvf/siegenia_door`, category **Integration**
3. Install, then restart Home Assistant
4. Settings → Devices & services → Add integration → **SIEGENIA Door**

**Manual**

Copy `custom_components/siegenia_door/` into your Home Assistant `config/custom_components/`
directory and restart.

### Supported hardware

This integration supports the **ACS** device family (device type 7) — the KFV
automatic door. SIEGENIA uses the same protocol for its ventilation units and
window drives, but their parameters are shaped differently, so setup will refuse
those with a message naming what it found instead.

Not supported: MHS Family and DRIVE window drives, AEROPAC / AEROVITAL /
AEROPLUS and other ventilation units. For the AEROPLUS WRG see
[schmidbeni/home-assistant-siegenia-Aeroplus-WRG](https://github.com/schmidbeni/home-assistant-siegenia-Aeroplus-WRG).

### Door state

The door reports leaf position and bolt status in a single field, exposed
verbatim as the `door_state` attribute because the lock entity's open/closed
flag cannot represent the difference:

| `door_state` | Meaning | Reads as |
| --- | --- | --- |
| `OPEN` | Leaf open | open |
| `CLOSED` | Shut and bolted | closed |
| `CLOSED_NOT_LOCKED` | Shut, bolt not thrown | closed |
| `UNDEFINED` | No sash sensor fitted | unknown |

Automations that need to distinguish "shut" from "shut and bolted" should use
`state_attr('lock.<your_door>', 'door_state')` rather than the entity state.

### Door users and fingerprints

Settings → Devices & services → SIEGENIA Door → **Configure** manages the users
the door itself stores. You can add a user, delete one, enrol a fingerprint and
remove a credential. Everything happens on the door and takes effect at once —
none of it is stored in Home Assistant.

This lives behind *Configure* rather than being exposed as actions on purpose.
Config and options flows are restricted to Home Assistant administrators, while
actions are callable by any automation; enrolling a credential on a front door
belongs in the first category.

**Enrolling a fingerprint**

Be standing at the door before you submit the slot form. The door begins waiting
for a finger the instant the form is submitted, and it waits *indefinitely* —
it has no timeout of its own. A successful enrolment takes about twenty seconds.

The dialog cancels the enrolment at the door if you close it. If Home Assistant
loses its connection or the browser tab is closed outright, a timeout cancels it
instead, after two minutes.

Each user has four fingerprint slots. Only the free ones are offered, and a slot
claimed by somebody else while your form was open is rejected rather than
overwritten.

**Limitations**

* Newer doors that report `acs_master: io_smart` use a different set of user
  management commands. There was no such door available to test against, so the
  flow refuses them rather than guessing — use the SIEGENIA app for those.
* Only fingerprints can be enrolled from here. RFID tags, PIN codes and
  Bluetooth credentials use the same mechanism in the app but have not been
  verified on hardware.
* Editing an existing user is not offered, for the same reason.

The protocol behind this is written up in
[`docs/acs-user-protocol.md`](docs/acs-user-protocol.md), including which parts
are confirmed on hardware and which are not.

### Firmware differences

Doors do not all expose the same capabilities, and the difference is not
cosmetic:

| Firmware | Day/night mode | Door position |
| --- | --- | --- |
| 1.9.x (variant 1) | Yes — lock and unlock work | Reported |
| 1.11.x (variant 3) | **Not exposed** | Often `UNDEFINED` |

Verified against two KFV doors on firmware 1.9.1.23 (type 7, variant 1).

On firmware 1.11 and later the door no longer publishes `daymode` over the local
interface. Those doors show an unknown lock state, and lock/unlock returns a
clear error rather than failing silently — the **open** action still works. If
your door has no sash sensor fitted, its position reads as unknown; the official
SIEGENIA app behaves the same way, so this is the door, not the integration.

### Notes

* **Open releases the latch — it does not swing the door.** It actuates the lock
  the way a door buzzer does, so somebody still has to push. This is exactly
  what `lock.open` means in Home Assistant.
* Physical operations take roughly 5–10 seconds. The entity shows
  `locking` / `unlocking` / `opening` in the meantime and settles once the door
  confirms. Because a latch release usually leaves `door_state` unchanged,
  `opening` clears on a short timer instead of waiting for a confirmation that
  will never arrive.
* Acknowledging a command is not the same as completing it, so state is
  reconciled from the device rather than assumed.

### Troubleshooting

Run the diagnostic script against your door and paste its output into a GitHub
issue — it reports your firmware, device family and which capabilities are
present, with identifiers redacted:

```bash
pip install websocket-client
python3 tools/diagnostic_script.py --host 192.168.1.50 --username myuser
```

For an already-configured integration, Settings → Devices & services →
SIEGENIA Door → **Download diagnostics** gives the same picture.

### Screenshot

<img width="1084" alt="screenshot" src="https://github.com/user-attachments/assets/52bdf81d-e47c-404f-b5c5-eb9d3f1f48c7">

### Credits

Protocol groundwork by @Apollon77, @EvotecIT and @CaeruleusAqua:

* https://github.com/Apollon77/ioBroker.siegenia
* https://github.com/EvotecIT/homebridge-siegenia
* https://github.com/CaeruleusAqua/Sigenia-GENIUS

Brand artwork from [home-assistant/brands](https://github.com/home-assistant/brands).

### License

[MIT](LICENSE)
