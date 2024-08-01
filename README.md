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

### Notes

* Actions take approximately 5-10 seconds to propagate.
* The open/closed sensor is not instant. (Possible improvements left unexplored)
* Tested on two doors only; may not work with other models.

### To-do

* Additional features like HA auto-discovery and user management are possible, but not implemented.

