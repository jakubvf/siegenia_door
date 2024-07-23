from __future__ import annotations

import asyncio
import random
import websocket
import ssl
import json
from .const import DOMAIN

from homeassistant.core import HomeAssistant

class SiegeniaDoor:
    """Handles door comunication"""

    def __init__(self, hass: HomeAssistant, host: str, username: str, password: str) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._hass = hass

    def login(self) -> WebSocket:
        ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
        ws.connect(f"wss://{self._host}/WebSocket")
        login_command = {
            'command': 'login',
            'user': self._username,
            'password': self._password,
            'long_life': False,
            'id': 2,
        }
        ws.send(json.dumps(login_command))
        response = ws.recv()
        response = json.loads(response)
        status = response.get('status')
        if status != 'ok':
            raise Exception('login failed')
        return ws

    def get_device(self) -> dict:
        ws = self.login()
        ws.send('{"command":"getDevice", "id":3}')
        response = json.loads(ws.recv())
        ws.close()

        status = response.get('status')
        if status != 'ok':
            raise Exception('getDevice failed')

        return response

    def open(self):
        ws = self.login()
        ws.send('{"command":"setDeviceParams", "params":{"openclose":"OPEN"}, "id":3}')
        response = json.loads(ws.recv())
        ws.close()

        status = response.get('status')
        if status != 'ok':
            raise Exception('setDeviceParams failed')

    def set_day_mode(self, enabled):
        ws = self.login()
        command = {
            'command': 'setDeviceParams',
            'params': {
                'daymode': enabled,
            },
            'id': 3,
        }
        ws.send(json.dumps(command))
        response = json.loads(ws.recv())
        ws.close()

        status = response.get('status')
        if status != 'ok':
            raise Exception('setDeviceParams failed')


    def get_device_params(self) -> dict:
        ws = self.login()
        ws.send('{"command":"getDeviceParams", "id":3}')
        response = json.loads(ws.recv())
        ws.close()

        status = response.get('status')
        if status != 'ok':
            raise Exception('getDeviceParams failed')

        return response

    def get_device_info(self) -> dict:
        response = self.get_device()
        return {
            'identifiers': {
                (DOMAIN, response['data']['serialnr'])
            },
            'name': response['data']['systemname'],
            'manufacturer': 'Siegenia',
            'sw_version': response['data']['softwareversion'],
            'serial_number': response['data']['serialnr'],
            'hw_version': response['data']['hardwareversion'],
        }


    async def test_connection(self) -> bool:
        ws = self.login()

        if ws != None:
            ws.close()
            return True

        return False


