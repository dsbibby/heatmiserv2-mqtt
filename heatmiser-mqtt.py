#!/usr/bin/env python3.9

import asyncio
from configparser import ConfigParser
from heatmiser.network import HeatmiserNetwork
from heatmiser.device import HeatmiserDevice
from heatmiser.logging import log
from itertools import chain
from paho.mqtt import client as mqtt_client
import random


hm_task = None
config = None
mqtt = None


""" Generate a range from a hyphonated,comma seperated
string of numbers: e.g. 1,2,5-7,9
"""
def rangeString(commaString):
    def hyphenRange(hyphenString):
        x = [int(x) for x in hyphenString.split('-')]
        return range(x[0], x[-1]+1)
    return chain(*[hyphenRange(r) for r in commaString.split(',')])


def hm_device_updated(device, param_name, value):
    log('info', f"HM Device Updated - ID: {device.id}, {param_name} = {value}")
    mqtt.publish(f"{config['MQTT']['pub_topic']}/{device.id}/{param_name}", value)


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

 
#async def mqtt_monitor():
#    log('info', 'MQTT Started')
#    await asyncio.sleep(15)
#    log('info', 'MQTT End')


async def main():
    HeatmiserDevice.on_param_change = hm_device_updated
    try:
        hm_config = config['Heatmiser']
        serial_port = hm_config['serial_port']
        device_ids = hm_config['device_ids']
        mqtt_config = config['MQTT']
        broker = mqtt_config['broker']
        port = mqtt_config['port']
        username = mqtt_config['username']
        password = mqtt_config['password']
    except KeyError as e:
        print(e)
        exit()
    client_id = f'heatmiser-mqtt-{random.randint(0, 1000)}'

    while True:
        # Open the serial connection to the Heatmiser network and start up the monitors
        hmn = HeatmiserNetwork(serial_port, rangeString(device_ids))
        mqtt = connect_mqtt(client_id, broker, port, username, password)
        mqtt.on_message = handle_mqtt_message
        mqtt.subscribe(mqtt_config['sub_topic'])
        tasks = set()
        tasks.add(asyncio.create_task(hmn.run()))
        tasks.add(asyncio.create_task(mqtt.loop_forever()))
        # Monitor tasks
        ended = False
        while not ended:
            await asyncio.sleep(5)
            for task in tasks:
                if task.done():
                    ended = True

        # At least one of the tasks has ended. Close the rest cleanly
        hmn.close()
        mqtt.disconnect()
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
    config = ConfigParser()
    config.read('config.ini')

    asyncio.run(main())
    print("Done")
