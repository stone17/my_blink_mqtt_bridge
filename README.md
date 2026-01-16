# Blink MQTT Bridge

++

A robust Dockerized bridge connecting Blink Camera Systems to MQTT, designed for seamless integration with Home Assistant, Domoticz, and other smart home platforms.

++

It features a user-friendly Web Dashboard for managing 2FA, viewing camera snapshots, and monitoring system status.

++

## ✨ Features

++

* Web Dashboard: View camera thumbnails, temperatures, and online status in a clean interface.

* Two-Factor Authentication (2FA): Handle Blink's SMS/Email verification codes directly in the browser.

* MQTT Control: Arm/Disarm your system and trigger snapshots via MQTT commands.

* Home Assistant Discovery: Automatically creates Alarm Panel and Camera entities in Home Assistant.

* Live Snapshots: Trigger new image captures from the UI or MQTT.

* Debug Mode: Inspect raw JSON data from Blink servers to troubleshoot missing attributes.

* Smart Polling: Configurable polling intervals to keep the connection alive without blocking your account.

++

## 🚀 Quick Start

++

### 1. Create Directories

++

Create a folder for the project and a config subdirectory:

++

bash mkdir -p blink-bridge/config cd blink-bridge 

++

### 2. Docker Compose

++

Create a docker-compose.yaml file:

++

yaml version: '3.8' services:   blink-bridge:     image: blink-bridge:latest     build: .     container_name: blink-bridge     restart: unless-stopped     network_mode: host  # Recommended for local MQTT discovery     volumes:       - ./config:/config       - ./app/images:/app/images     environment:       - MQTT_BROKER=192.168.0.100  # Replace with your Broker IP       - MQTT_PORT=1883       - CONFIG_PATH=/config/blink_config.yaml 

++

### 3. Build & Run

++

bash docker-compose up -d --build 

++

### 4. Initial Setup

++

1. Open your browser and go to http://<YOUR_IP>:8000.

2. Click ⚙️ Config in the top right.

3. Enter your Blink Email and Password.

4. Enter your MQTT Broker details.

5. Click Save & Connect.

6. If prompted, enter the 2FA Code sent to your phone/email in the yellow banner.

++

## 📡 MQTT Topics

++

| Topic | Payload | Description |

| :--- | :--- | :--- |

| blink/command | ARM, DISARM | Arm or Disarm the entire system |

| blink/state | armed_away, disarmed | Current System Status |

| blink/camera/<NAME>/snap | PRESS | Trigger a new snapshot for a specific camera |

| blink/sensor/<NAME>/temp | 21.5 | Current Temperature |

++

## 🏠 Home Assistant Integration

++

Ensure your Home Assistant MQTT integration has discovery: true enabled. The bridge will automatically create:

++

* Alarm Control Panel: alarm_control_panel.blink_system

* Sensors: Temperature sensors for each camera.

++

To view the images in Home Assistant, you can use the Generic Camera integration pointing to:

++

http://<BRIDGE_IP>:8000/images/<CAMERA_NAME>.jpg

++

## 🐞 Troubleshooting

++

* Status always Armed?

  * The Blink API sometimes reports "Sync Modules" as armed even when the system is disarmed. This bridge uses the global Network status to determine the true state.

  * Click the 🐞 Debug button in the Web UI to see the raw JSON data received from Blink.

++

* Cameras showing Offline?

  * The bridge checks the raw status attribute from the Blink homescreen data. Use the ⓘ info button on the camera card to verify the raw values.

++

* 2FA Requests on Restart?

  * Credentials are saved to /config/blink_credentials.json after a successful login. Ensure your Docker volume mapping is correct so this file persists.

++

## 📄 License

++

MIT License