from zenapi import ZendureAPI as zapp
import random, json, time, logging, sys, requests, os
from datetime import datetime
from functools import reduce
from paho.mqtt import client as mqtt_client
from collections import namedtuple
from flask import Flask, render_template, request
from flask_socketio import SocketIO
from random import random
from threading import Lock

FORMAT = '%(asctime)s:%(levelname)s: %(message)s'
logging.basicConfig(stream=sys.stdout, level="INFO", format=FORMAT)
log = logging.getLogger("")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

ZEN_USER = os.environ.get('ZEN_USER',None)
ZEN_PASSWD = os.environ.get('ZEN_PASSWD',None)

if ZEN_USER is None or ZEN_PASSWD is None:
    log.error("No username and password environment variable set!")
    sys.exit(0)

ZenAuth = namedtuple("ZenAuth",["productKey","deviceKey","clientId"])

# MQTT broker where we subscribe to all the telemetry data we need to steer
broker = 'mq.zen-iot.com'
port = 1883
client: mqtt_client
auth: ZenAuth
device_details = {}


# Flask SocketIO background task
thread = None
thread_lock = Lock()

app = Flask(__name__)
#app.config['SECRET_KEY'] = 'donsky!'
socketio = SocketIO(app, cors_allowed_origins='*')

def get_current_datetime():
    now = datetime.now()
    return now.strftime("%H:%M:%S")

def on_solarflow_update(msg):
    global device_details
    payload = json.loads(msg)
    log.info(payload["properties"])
    if "outputHomePower" in payload["properties"]:
        socketio.emit('updateSensorData', {'metric': 'outputHome', 'value': payload["properties"]["outputHomePower"], 'date': get_current_datetime()})
    if "solarInputPower" in payload["properties"]:
        socketio.emit('updateSensorData', {'metric': 'solarInput', 'value': payload["properties"]["solarInputPower"], 'date': get_current_datetime()})
    if "outputPackPower" in payload["properties"]:
        socketio.emit('updateSensorData', {'metric': 'outputPack', 'value': payload["properties"]["outputPackPower"], 'date': get_current_datetime()})
    if "electricLevel" in payload["properties"]:
        socketio.emit('updateSensorData', {'metric': 'electricLevel', 'value': payload["properties"]["electricLevel"], 'date': get_current_datetime()})
    if "outputLimit" in payload["properties"]:
        socketio.emit('updateLimit', {'property': 'outputLimit', 'value': f'{payload["properties"]["outputLimit"]} W'})
    if "inputLimit" in payload["properties"]:
        socketio.emit('updateLimit', {'property': 'inputLimit', 'value': f'{payload["properties"]["inputLimit"]} W'})
    if "socSet" in payload["properties"]:
        socketio.emit('updateLimit', {'property': 'socSet', 'value': f'{payload["properties"]["socSet"]/10} %'})
    if "minSoc" in payload["properties"]:
        socketio.emit('updateLimit', {'property': 'minSoc', 'value': f'{payload["properties"]["minSoc"]/10} %'})
        
    if "packData" in payload:
        log.info(payload["packData"])    
        if len(payload["packData"]) >= 1:
            for pack in payload["packData"]:
                if "socLevel" in pack:
                    socketio.emit('updateSensorData', {'metric': 'socLevel', 'value': pack["socLevel"], 'date': pack["sn"]})
                if "maxTemp" in pack:
                    socketio.emit('updateSensorData', {'metric': 'maxTemp', 'value': pack["maxTemp"]/100, 'date': pack["sn"]})
            for dev_pack in device_details["packDataList"]:
                for pack in payload["packData"]:
                    if "socLevel" in pack:
                        if dev_pack["sn"] == pack["sn"]:
                            dev_pack["socLevel"] = pack["socLevel"]
                    if "maxTemp" in pack:
                        if dev_pack["sn"] == pack["sn"]:
                            dev_pack["maxTemp"] = pack["maxTemp"]


def on_message(client, userdata, msg):
    on_solarflow_update(msg.payload.decode())

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT Broker!")
    else:
        log.error("Failed to connect, return code %d\n", rc)

def on_disconnect(client, userdata, rc):
    if rc != 0:
        log.warning("Unexpected disconnection.")
        mqtt_background_task()

def connect_mqtt(client_id) -> mqtt_client:
    global client
    client = mqtt_client.Client(client_id)
    client.username_pw_set(username="zenApp", password="oK#PCgy6OZxd")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect(broker, port)
    return client

def subscribe(client: mqtt_client, auth: ZenAuth):
    # list of topics to subscribe
    report_topic = f'/{auth.productKey}/{auth.deviceKey}/properties/report'
    iot_topic = f'iot/{auth.productKey}/{auth.deviceKey}/#'
    client.subscribe(report_topic)
    client.subscribe(iot_topic)
    client.on_message = on_message

def get_auth() -> ZenAuth:
    global auth
    global device_details
    with zapp.ZendureAPI() as api:
        token = api.authenticate(ZEN_USER,ZEN_PASSWD)
        devices = api.get_device_ids()
        for dev_id in devices:
            device = api.get_device_details(dev_id)
            device_details = device
            auth = ZenAuth(device["productKey"],device["deviceKey"],token)
        
            # send initial data imediately, like battery stack info
            socketio.emit('updateSensorData', {'metric': 'electricLevel', 'value': device["electricLevel"], 'date': get_current_datetime()})

            for battery in device["packDataList"]:
                socketio.emit('updateSensorData', {'metric': 'socLevel', 'value': battery["socLevel"], 'date': battery["sn"]})
                socketio.emit('updateSensorData', {'metric': 'maxTemp', 'value': battery["maxTemp"]/10, 'date': battery["sn"]})

        log.info(f'Zendure Auth: {auth}')
        return auth

def mqtt_background_task():
    auth = get_auth()
    client = connect_mqtt(auth.clientId)
    subscribe(client,auth)
    client.loop_start()

@app.route('/')
def index():
    global devices
    return render_template('index.html', **device_details )

@socketio.on('connect')
def connect():
    global device_details
    log.info('Client connected')

    #emit device info we have collected on startup (may not be the full accurate data)
    socketio.emit('updateSensorData', {'metric': 'electricLevel', 'value': device_details["electricLevel"], 'date': get_current_datetime()})

    for battery in device_details["packDataList"]:
        socketio.emit('updateSensorData', {'metric': 'socLevel', 'value': battery["socLevel"], 'date': battery["sn"]})
        socketio.emit('updateSensorData', {'metric': 'maxTemp', 'value': battery["maxTemp"]/10, 'date': battery["sn"]})


@socketio.on('setLimit')
def setLimit(msg):
    global client
    
    jmsg = json.loads(msg)
    log.info(jmsg)
    payload = {"properties": { jmsg["property"]: int(jmsg["value"]) }}
    log.info(json.dumps(payload))
    client.publish(f'iot/{auth.productKey}/{auth.deviceKey}/properties/write', json.dumps(payload))

@socketio.on('disconnect')
def disconnect():
    log.info('Client disconnected')

if __name__ == '__main__':
    # starting mqtt network loop
    mqtt_background_task()
    
    socketio.run(app,host="0.0.0.0",allow_unsafe_werkzeug=True)
