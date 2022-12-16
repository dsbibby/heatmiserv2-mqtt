#!/usr/bin/env python3.9

import asyncio
import random
import re
import sys
import json
from itertools import chain
from configparser import ConfigParser
from datetime import datetime
from paho.mqtt import client as mqtt_client
from heatmiser.network import HeatmiserNetwork
from heatmiser.device import HeatmiserDevice
from heatmiser.logging import log


HM_TASK = None
CONFIG = None
MQTT = None
BASE_TOPIC = None
hm_entity_id = None

""" Generate a range from a hyphonated,comma seperated
string of numbers: e.g. 1,2,5-7,9
"""
def rangeString(commaString):
    def hyphenRange(hyphenString):
        x = [int(x) for x in hyphenString.split('-')]
        return range(x[0], x[-1]+1)
    return chain(*[hyphenRange(r) for r in commaString.split(',')])


def hm_advertise_device(device):
    log('debug', f'Advertising device with ID {device.id}')
    payload = {
        "~": f"{BASE_TOPIC}/{device.id}",
        "name": f"Heatmiser {device.TYPE_STR} {device.id}",
        "unique_id": f"{hm_entity_id}-{device.id}",
        "modes": ["off", "heat", "cool"],
        "min_temp": 5,
        "max_temp": 35,
        "curr_temp_t": "~/room_temp/state",
        "mode_stat_t": "~/mode/state",
        "temperature_state_topic": "~/set_temp/state",
        "action_topic": "~/heating_state/state"
    }
    log('debug', f"{BASE_TOPIC}/{device.id}/config -> {json.dumps(payload)}")
    MQTT.publish(f"{BASE_TOPIC}/{device.id}/config", json.dumps(payload))
    MQTT.subscribe(f"{BASE_TOPIC}/{device.id}/+/set")


def hm_device_updated(device, param_name, value):
    global MQTT
    log('info', f"HM Device Updated - ID: {device.id}, {param_name} = {value}")
    if MQTT is not None:
        if param_name == "enabled":
            param_name = "mode"
            if value:
                value = "cool" if device.frost_mode else "heat"
            else:
                value = "off"
        elif param_name == "frost_mode":
            param_name = "mode"
            value = "cool" if value else "heat"
        elif param_name == "heating_state":
            value = "heating" if value else "idle"
        elif param_name == "datetime":
            value = value.timestamp()
        MQTT.publish(f"{BASE_TOPIC}/{device.id}/{param_name}/state", value)


def connect_mqtt(client_id, broker, port, username, password):
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log('info', "Connected to MQTT Broker!")
        else:
            log('warn', f"Failed to connect, return code {rc}")
    # Set Connecting Client ID
    client = mqtt_client.Client(client_id)
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.connect(broker, port)
    return client


def handle_mqtt_message(client, userdata, msg):
    log('info', f'Received `{msg.payload.decode()}` from `{msg.topic}` topic')


async def mqtt_monitor():
    global MQTT

    log('info', 'MQTT Started')

    while True:
        MQTT.loop()
        await asyncio.sleep(1)
        if not MQTT.is_connected():
            break

    log('info', 'MQTT End')


async def main():
    global MQTT, BASE_TOPIC, hm_entity_id

    HeatmiserDevice.on_param_change = hm_device_updated

    try:
        hm_config = CONFIG['Heatmiser']
        serial_port = hm_config['serial_port']
        device_ids = hm_config['device_ids']
        mqtt_config = CONFIG['MQTT']
        broker = mqtt_config['broker']
        port = int(mqtt_config['port'])
        username = mqtt_config['username']
        password = mqtt_config['password']
    except KeyError as e:
        print(e)
        sys.exit()

    client_id = f'heatmiser-mqtt-{random.randint(0, 1000)}'
    hm_entity_id = re.sub("[^a-zA-Z0-9_-]", "", serial_port)
    BASE_TOPIC = f"{mqtt_config['topic']}/climate/{hm_entity_id}"

    while True:
        # Open the serial connection to the Heatmiser network and start up the monitors
        hmn = HeatmiserNetwork(serial_port, rangeString(device_ids))
        hmn.on_device_discovered = hm_advertise_device
        MQTT = connect_mqtt(client_id, broker, port, username, password)
        MQTT.on_message = handle_mqtt_message
        tasks = set()
        tasks.add(asyncio.create_task(hmn.run()))
        tasks.add(asyncio.create_task(mqtt_monitor()))
        # Monitor tasks
        ended = False
        while not ended:
            await asyncio.sleep(5)
            for task in tasks:
                if task.done():
                    ended = True

        # At least one of the tasks has ended. Close the rest cleanly
        hmn.close()
        MQTT.disconnect()
        for task in tasks:
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Pause then attempt to restart from the top
        log('info', 'Tasks ended... reconnecting in 10...')
        await asyncio.sleep(10)


if __name__ == "__main__":
    CONFIG = ConfigParser()
    CONFIG.read('config.ini')

    asyncio.run(main())
    print("Done")
