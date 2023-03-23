#!/usr/bin/env python3.9

import ast
import asyncio
import random
import re
import sys
import json
import yaml
from itertools import chain
from configparser import ConfigParser
from datetime import datetime
from paho.mqtt import client as mqtt_client
from heatmiser.network import HeatmiserNetwork
from heatmiser.device import HeatmiserDevice
from heatmiser.logging import log
import heatmiser.logging

HM_TASK = None
CONFIG = None
MQTT = None
DISCOVERY_TOPIC = "homeassistant"
BASE_TOPIC = None
hm_entity_id = None
hmn = None
heatmiser.logging.LOG_LEVEL = 1


def rangeString(commaString):
    """ Generate a range from a hyphonated,comma seperated
    string of numbers: e.g. 1,2,5-7,9
    """
    def hyphenRange(hyphenString):
        x = [int(x) for x in hyphenString.split('-')]
        return range(x[0], x[-1]+1)
    return chain(*[hyphenRange(r) for r in commaString.split(',')])


def hm_advertise_device(device):
    log('debug', f'Advertising device with ID {device.id}')

    if device.id in CONFIG['heatmiser'].get('device_names', {}).keys():
        name = CONFIG['heatmiser']['device_names'][device.id]
    else:
        name = f"Heatmiser {device.TYPE_STR} {device.id}"
    
    DEVICE_TOPIC = f"{BASE_TOPIC}/{hm_entity_id}/{device.id}"
        
    parent_device = {
        "identifiers": [ f"{hm_entity_id}_{device.id}"],
        "manufacturer": "Heatmiser",
        "model": device.TYPE_STR,
        "name": name,
        "via_device": "Heatmiser UH1"
    }
    
    payloads = [
        {
            "type": "climate",
            "data": {
                "~": DEVICE_TOPIC,
                "name": f"{name} Thermostat",
                "unique_id": f"stat_{hm_entity_id}_{device.id}",
                "modes": ["off", "heat", "cool"],
                "min_temp": 5,
                "max_temp": 35,
                "current_temperature_topic": "~/room_temp/state",
                "mode_state_topic": "~/mode/state",
                "mode_command_topic": "~/mode/set",
                "temperature_state_topic": "~/set_temp/state",
                "temperature_command_topic": "~/set_temp/set",
                "action_topic": "~/heating_state/state",
                #"power_command_topic": "~/enabled/set",
                "payload_on": "True",
                "payload_off": "False",
                "device": parent_device
            }
        },
        {
            "type": "switch",
            "data":{
                "~": DEVICE_TOPIC,
                "name": f"{name} Lock",
                "unique_id": f"lock_{hm_entity_id}_{device.id}",
                "command_topic": "~/lock_state/set",
                "state_topic": "~/lock_state/state",
                "payload_on": "True",
                "payload_off": "False",
                "icon": "mdi:lock",
                "device": parent_device
            }
        },
        {
            "type": "sensor",
            "data":{
                "~": DEVICE_TOPIC,
                "name": f"{name} Time",
                "unique_id": f"time_{hm_entity_id}_{device.id}",
                "state_topic": "~/datetime/state",
                "value_template": '{{ value | int | timestamp_custom("%a %H:%M") }}',
                "icon": "mdi:clock",
                "device": parent_device
            }
        }
    ]
    
    if device.TYPE_STR == 'PRTHW':
        payloads.append({
            "type": "switch",
            "data":{
                "~": DEVICE_TOPIC,
                "name": f"{name} Manual HW",
                "unique_id": f"manual_hw_{hm_entity_id}_{device.id}",
                "command_topic": "~/manual_hw_state/set",
                "state_topic": "~/manual_hw_state/state",
                "payload_on": "True",
                "payload_off": "False",
                "icon": "mdi:heat-wave",
                "device": parent_device
            }
        })
        payloads.append({
            "type": "binary_sensor",
            "data":{
                "~": DEVICE_TOPIC,
                "name": f"{name} HW State",
                "unique_id": f"hw_{hm_entity_id}_{device.id}",
                "state_topic": "~/hw_state/state",
                "payload_on": "True",
                "payload_off": "False",
                "icon": "mdi:heat-wave",
                "device": parent_device
            }
        })
    
    for payload in payloads:
        uid = payload["data"]["unique_id"]
        component = payload["type"]
        if len(payload["data"]) == 1:
            data = {}
        else:
            data = payload["data"]
        log('debug1', f'{DISCOVERY_TOPIC}/{component}/{uid}/config -> {json.dumps(data)}')
        MQTT.publish(f'{DISCOVERY_TOPIC}/{component}/{uid}/config', json.dumps(data), retain=True)
    MQTT.subscribe(f'{DEVICE_TOPIC}/+/set')


def hm_device_updated(device, param_name, value):
    global MQTT
    log('info', f"HM Device Updated - ID: {device.id}, {param_name} = {value}")
    if MQTT is not None:
        component = "climate"
        property = param_name
        if param_name == "enabled":
            property = "mode"
            if value:
                value = "cool" if device.frost_mode else "heat"
            else:
                value = "off"
        elif param_name == "frost_mode":
            property = "mode"
            value = "cool" if value else "heat"
        elif param_name == "heating_state":
            value = "heating" if value else "idle"
        elif param_name == "datetime":
            value = value.timestamp()
        MQTT.publish(f"{BASE_TOPIC}/{hm_entity_id}/{device.id}/{property}/state", value)


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
    
    if msg.topic.startswith(BASE_TOPIC):
        log('debug', f'Received `{msg.payload.decode()}` from `{msg.topic}` topic')
        rx_entity_id, device_id, param, _ = msg.topic[len(BASE_TOPIC) + 1:].split("/")
        val = msg.payload.decode()
        device = hmn.device(int(device_id))
        
        if param == "mode":
            if val == "off":
                param = "enabled"
                val = False
            else:
                device.enabled = True
                param = "frost_mode"
                val = False if val == "heat" else True
        else:
            val = ast.literal_eval(val)
        if isinstance(val, float):
            val = int(val)
        
        if device:
            asyncio.create_task(device._send_param_update(param, val))


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
    global MQTT, BASE_TOPIC, hm_entity_id, hmn

    HeatmiserDevice.on_param_change = hm_device_updated

    try:
        hm_config = CONFIG['heatmiser']
        serial_port = hm_config['serial_port']
        device_ids = str(hm_config['device_ids'])
        mqtt_config = CONFIG['mqtt']
        broker = mqtt_config.get('broker', '127.0.0.1')
        port = int(mqtt_config.get('port', 1883))
        username = mqtt_config.get('username')
        password = mqtt_config.get('password')
    except KeyError as e:
        print(f'Missing required config: {e}')
        sys.exit()

    client_id = f'heatmiser-mqtt-{random.randint(0, 1000)}'
    hm_entity_id = re.sub("[^a-zA-Z0-9_-]", "", broker)
    BASE_TOPIC = f"{mqtt_config['topic']}"

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
    with open('config.yaml', 'r') as file:
        CONFIG = yaml.safe_load(file)

    asyncio.run(main())
    print("Done")
