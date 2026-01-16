import asyncio
import json
import logging
import os
import yaml
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import paho.mqtt.client as mqtt_client

from app.blink_service import BlinkService

# --- CONFIG ---
CONFIG_PATH = os.getenv("CONFIG_PATH", "config/blink_config.yaml")
CREDS_PATH = "/config/blink_credentials.json"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("BlinkBridge")

# --- GLOBAL STATE ---
blink_svc = BlinkService(CREDS_PATH)
latest_data = {"armed": False, "status_str": "Unknown", "cameras": []}
system_state = "STARTING" 
running = True

# --- CONFIG MANAGER ---
class ConfigManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {
            "mqtt_broker": os.getenv("MQTT_BROKER", "192.168.0.100"),
            "mqtt_port": int(os.getenv("MQTT_PORT", 1883)),
            "mqtt_username": "",
            "mqtt_password": "",
            "poll_interval": 3600, 
            "blink_email": "",
            "blink_password": ""
        }
        self.load()
        # Force save on startup to ensure file exists
        self.save()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    self.data.update(yaml.safe_load(f) or {})
            except Exception as e: logger.error(f"Config load error: {e}")

    def save(self):
        try:
            with open(self.filepath, 'w') as f:
                yaml.dump(self.data, f)
        except Exception as e: logger.error(f"Config save error: {e}")

cfg = ConfigManager(CONFIG_PATH)

# --- MQTT HANDLER ---
class MqttHandler:
    def __init__(self):
        self.client = mqtt_client.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self):
        try:
            broker = cfg.data['mqtt_broker']
            logger.info(f"Connecting to MQTT Broker: {broker}")
            
            user = cfg.data.get('mqtt_username')
            pwd = cfg.data.get('mqtt_password')
            if user and pwd:
                self.client.username_pw_set(user, pwd)
                
            self.client.connect(broker, int(cfg.data['mqtt_port']), 60)
            self.client.loop_start()
        except Exception as e: logger.error(f"MQTT Error: {e}")

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT Connected.")
            client.subscribe("blink/command") 
            client.subscribe("blink/camera/+/snap")
            self.publish_discovery()
        else:
            logger.error(f"MQTT Connect Failed code={rc}")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode().upper()
        
        if topic == "blink/command":
            if payload in ["ARM", "ARM_AWAY"]:
                asyncio.run_coroutine_threadsafe(perform_action("arm"), loop)
            elif payload == "DISARM":
                asyncio.run_coroutine_threadsafe(perform_action("disarm"), loop)
        
        if "snap" in topic:
            try:
                cam_name = topic.split("/")[2]
                asyncio.run_coroutine_threadsafe(trigger_snap(cam_name), loop)
            except: pass

    def publish_discovery(self):
        disc_prefix = "homeassistant"
        payload = {
            "name": "Blink System",
            "unique_id": "blink_hub_main",
            "command_topic": "blink/command",
            "state_topic": "blink/state",
            "availability_topic": "blink/status",
            "payload_disarm": "DISARM",
            "payload_arm_away": "ARM_AWAY",
            "device": {"identifiers": ["blink_hub"], "name": "Blink Hub", "manufacturer": "Blink"}
        }
        self.client.publish(f"{disc_prefix}/alarm_control_panel/blink_hub/config", json.dumps(payload), retain=True)

    def publish_state(self):
        state = "armed_away" if latest_data["armed"] else "disarmed"
        self.client.publish("blink/state", state, retain=True)
        self.client.publish("blink/status", "online", retain=True)
        for cam in latest_data["cameras"]:
            clean_name = cam['name'].replace(" ", "_").lower()
            self.client.publish(f"blink/sensor/{clean_name}/temp", cam['temperature'])

mqtt = MqttHandler()

# --- ACTIONS ---
async def update_data():
    global latest_data, system_state
    try:
        await blink_svc.refresh()
        latest_data = await blink_svc.get_status()
        mqtt.publish_state()
        system_state = "CONNECTED"
    except Exception as e:
        logger.error(f"Update Data Failed: {e}")

async def perform_action(action_type):
    if action_type == "arm": await blink_svc.arm_system(True)
    elif action_type == "disarm": await blink_svc.arm_system(False)
    await update_data()

async def trigger_snap(cam_name):
    await blink_svc.snap_picture(cam_name)
    await update_data()

async def poll_blink():
    global system_state
    while running:
        if system_state == "WAITING_2FA":
            await asyncio.sleep(5)
            continue
            
        if system_state != "CONNECTED":
            # Pass saved credentials if available
            u = cfg.data.get("blink_email")
            p = cfg.data.get("blink_password")
            
            res = await blink_svc.login(username=u, password=p)
            
            if res == "SUCCESS":
                system_state = "CONNECTED"
                # Clear raw password from memory/config if desired, 
                # but keep for now in case of re-login needs
                await update_data()
            elif res == "2FA_REQUIRED":
                system_state = "WAITING_2FA"
            elif res == "CONFIG_REQUIRED":
                system_state = "CONFIG_REQUIRED"
                # Wait until config is saved
                await asyncio.sleep(2)
                continue
            else:
                system_state = "ERROR"
                await asyncio.sleep(30)
                continue

        interval = cfg.data.get("poll_interval", 3600)
        await asyncio.sleep(interval)
        
        if system_state == "CONNECTED":
            try: await update_data()
            except: system_state = "ERROR"

# --- FASTAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    mqtt.start()
    task = asyncio.create_task(poll_blink())
    yield
    running = False
    mqtt.client.loop_stop()
    await blink_svc.close()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")
app.mount("/images", StaticFiles(directory="/app/images"), name="images")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request, "state": system_state, "data": latest_data, "config": cfg.data
    })

@app.post("/verify_2fa")
async def verify_2fa(code: str = Form(...)):
    global system_state
    if await blink_svc.validate_2fa(code):
        system_state = "CONNECTED"
        await update_data()
    return RedirectResponse("/", status_code=303)

@app.post("/snap/{name}")
async def snap_route(name: str):
    await trigger_snap(name)
    return RedirectResponse("/", status_code=303)

@app.post("/arm")
async def arm_route(action: str = Form(...)):
    await perform_action("arm" if action == "ARM" else "disarm")
    return RedirectResponse("/", status_code=303)

@app.post("/save_config")
async def save_config(
    mqtt_broker: str = Form(...), mqtt_username: str = Form(""), 
    mqtt_password: str = Form(""), poll_interval: int = Form(...),
    blink_email: str = Form(""), blink_password: str = Form("")
):
    global system_state
    
    cfg.data["mqtt_broker"] = mqtt_broker
    cfg.data["mqtt_username"] = mqtt_username
    cfg.data["mqtt_password"] = mqtt_password
    cfg.data["poll_interval"] = int(poll_interval)
    
    # Update Blink creds if provided
    if blink_email: cfg.data["blink_email"] = blink_email
    if blink_password: cfg.data["blink_password"] = blink_password
    
    cfg.save()
    
    # Restart MQTT
    mqtt.client.disconnect()
    mqtt.start()
    
    # Trigger retry in loop
    if system_state in ["ERROR", "CONFIG_REQUIRED"]:
        system_state = "STARTING"
        
    return RedirectResponse("/", status_code=303)