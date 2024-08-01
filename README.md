# Siegenia door

Home Assistant custom component for SIEGENIA Automatic Door KFV.

---

### Features

* Exposes [Home Assistant's Lock entity](https://www.home-assistant.io/integrations/lock/) to control your door.
* View the current status of the door.

### Requirements

* The door must be connected to your local network via WiFi.
* You need to know the IP address of your door.
* Door user account's username/password.

### HACS installation
1. Install the integration via HACS (Home Assistant Community Store)
   * Open HACS -> Three dots in the top right -> Custom repositories
   * Paste https://github.com/jakubvf/siegenia_door
   * Select Integration
2. Restart Home Assistant
3. Jablotron integration should be available in the integrations UI


### Notes

* Actions take approximately 5-10 seconds to propagate.
* The open/closed sensor is not instant. (Possible improvements left unexplored)
* Tested on two doors only; may not work with other models.


### Screenshot
<img width="1084" alt="screenshot" src="https://github.com/user-attachments/assets/52bdf81d-e47c-404f-b5c5-eb9d3f1f48c7">

### To-do

* Additional features like HA auto-discovery and user management are possible, but not implemented.

### Credits
Thanks to @Apollon77 and @EvotecIT for documenting the protocol.
 * https://github.com/EvotecIT/homebridge-siegenia
 * https://github.com/Apollon77/ioBroker.siegenia
