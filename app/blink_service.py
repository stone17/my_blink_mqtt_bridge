import aiohttp
import logging
import os
import json
import shutil
from blinkpy.blinkpy import Blink
from blinkpy.camera import BlinkCamera
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.helpers.util import json_load

_LOGGER = logging.getLogger(__name__)

class BlinkService:
    def __init__(self, creds_path):
        self.creds_path = creds_path
        self.session = None
        self.blink = None
        # Save images in /config/images so they persist
        self.images_dir = "/config/images"
        
        print(f"DEBUG: Initializing BlinkService. Image Directory: {self.images_dir}")
        try:
            os.makedirs(self.images_dir, exist_ok=True)
        except Exception as e:
            print(f"DEBUG: CRITICAL - Could not create image dir: {e}")

    async def start_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
            self.blink = Blink(session=self.session)

    async def login(self, username=None, password=None):
        await self.start_session()
        
        auth_data = {}
        if os.path.exists(self.creds_path):
            try:
                auth_data = await json_load(self.creds_path)
            except Exception as e: 
                print(f"DEBUG: Failed to load existing credentials file: {e}")
                auth_data = {}

        if not isinstance(auth_data, dict):
            auth_data = {}

        if username:
            auth_data["username"] = username
        if password:
            auth_data["password"] = password

        if not auth_data.get("username") or not auth_data.get("password"):
            return "CONFIG_REQUIRED"

        self.blink.auth = Auth(auth_data, session=self.session, no_prompt=True)

        try:
            await self.blink.start()
            await self.blink.save(self.creds_path)
            if getattr(self.blink, 'urls', None) and getattr(self.blink.urls, 'base_url', None):
                print(f"DEBUG: Blink Base URL determined as: {self.blink.urls.base_url}")
            return "SUCCESS"
        except BlinkTwoFARequiredError:
            print("DEBUG: 2FA required for Blink login")
            return "2FA_REQUIRED"
        except Exception as e:
            print(f"DEBUG: Login failed: {e}")
            return "FAILED"

    async def validate_2fa(self, code):
        if not self.blink or not self.blink.auth:
            print("DEBUG: 2FA Validation Failed: Blink or Auth not initialized")
            return False
        try:
            print("DEBUG: Sending 2FA code...")
            res = await self.blink.send_2fa_code(code)
            if res:
                await self.blink.save(self.creds_path)
                print("DEBUG: 2FA validation successful, credentials saved")
                return True
            else:
                print("DEBUG: 2FA verification returned False")
                return False
        except Exception as e:
            print(f"DEBUG: 2FA Validation Exception: {e}")
            return False

    async def arm_system(self, arm=True):
        if not self.blink: return False
        print(f"DEBUG: COMMAND -> {'ARM' if arm else 'DISARM'} System")
        try:
            if getattr(self.blink, 'sync', None):
                for sync_name, sync_module in self.blink.sync.items():
                    await sync_module.async_arm(arm)
            
            await self.blink.refresh(force_cache=True)
            return True
        except Exception as e:
            print(f"DEBUG: Arming Exception: {e}")
            raise

    async def refresh(self):
        if self.blink:
            print("DEBUG: Refreshing Blink Data...")
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails()

    async def download_thumbnails(self):
        """Downloads thumbnails for ALL cameras found in raw homescreen data."""
        if not self.blink: return
        
        print(f"DEBUG: Starting Thumbnail Download to {self.images_dir}...")
        
        headers = self.blink.auth.header if getattr(self.blink, 'auth', None) else None

        all_devices = []
        homescreen = getattr(self.blink, 'homescreen', None)
        if homescreen and isinstance(homescreen, dict):
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                devs = homescreen.get(category, [])
                if isinstance(devs, list):
                    all_devices.extend(devs)

        for dev in all_devices:
            if not isinstance(dev, dict):
                continue
            cam_id = str(dev.get('id'))
            name = dev.get('name', 'Unknown')
            thumb_url = dev.get('thumbnail')
            
            if thumb_url:
                if not thumb_url.startswith('http'):
                    base = "https://rest-prod.immedia-semi.com"
                    if getattr(self.blink, 'urls', None) and getattr(self.blink.urls, 'base_url', None): 
                        base = self.blink.urls.base_url
                    
                    if base.endswith('/') and thumb_url.startswith('/'):
                        thumb_url = thumb_url[1:]
                    
                    full_url = f"{base}{thumb_url}"
                else:
                    full_url = thumb_url

                try:
                    path = f"{self.images_dir}/{cam_id}.jpg"
                    
                    async with self.session.get(full_url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            with open(path, 'wb') as f:
                                f.write(data)
                            print(f"DEBUG:   > SAVED: {name} -> {path} ({len(data)} bytes)")
                        else:
                            print(f"DEBUG:   > ERROR fetching {name}: HTTP {resp.status}")
                except Exception as e:
                    print(f"DEBUG:   > EXCEPTION downloading {name}: {e}")

    async def get_status(self):
        if not self.blink: return {}

        is_armed = False
        cameras = []
        
        homescreen = getattr(self.blink, 'homescreen', None)
        if homescreen and isinstance(homescreen, dict) and 'networks' in homescreen:
            networks = homescreen.get('networks')
            if isinstance(networks, list):
                for net in networks:
                    if isinstance(net, dict) and net.get('armed') is True:
                        is_armed = True
                        break
        
        raw_devices = []
        if homescreen and isinstance(homescreen, dict):
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                items = homescreen.get(category, [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item_copy = item.copy()
                            item_copy['category_type'] = category
                            raw_devices.append(item_copy)

        name_counts = {}
        for d in raw_devices:
            n = d.get('name', 'Unknown')
            name_counts[n] = name_counts.get(n, 0) + 1

        for dev in raw_devices:
            original_name = dev.get('name', 'Unknown')
            cam_id = str(dev.get('id'))
            
            display_name = original_name
            if name_counts[original_name] > 1:
                dev_type = dev.get('type', 'cam')
                display_name = f"{original_name} ({dev_type})"
            
            online = True
            if 'status' in dev:
                online = (dev['status'] != 'offline')

            temp = 0
            if getattr(self.blink, 'cameras', None):
                for _, c_obj in self.blink.cameras.items():
                    if str(getattr(c_obj, 'camera_id', '')) == cam_id:
                        temp = getattr(c_obj, 'attributes', {}).get('temperature', 0)
                        break

            cameras.append({
                "name": display_name,
                "id": cam_id,
                "serial": dev.get('serial'),
                "temperature": temp,
                "online": online,
                "raw_json": json.dumps(dev, indent=2, default=str)
            })

        debug_data = {
            "networks_raw": homescreen.get('networks', []) if (homescreen and isinstance(homescreen, dict)) else "No Data",
            "all_raw_devices": raw_devices
        }

        return {
            "armed": is_armed,
            "status_str": "Armed" if is_armed else "Disarmed",
            "cameras": cameras,
            "raw_json": json.dumps(debug_data, indent=2, default=str)
        }

    async def snap_picture(self, target_id):
        if not self.blink: return None
        target_id = str(target_id)
        
        print(f"DEBUG: Requesting SNAP for Camera ID {target_id}...")

        target_cam = None
        if getattr(self.blink, 'cameras', None):
            for _, cam in self.blink.cameras.items():
                if str(getattr(cam, 'camera_id', '')) == target_id:
                    target_cam = cam
                    break
        
        if not target_cam:
            print(f"DEBUG: Reconstructing camera object for ID {target_id}...")
            raw_data = None
            homescreen = getattr(self.blink, 'homescreen', None)
            if homescreen and isinstance(homescreen, dict):
                for cat in ['owls', 'cameras', 'doorbells', 'chickadees']:
                    items = homescreen.get(cat, [])
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict) and str(item.get('id')) == target_id:
                                raw_data = item
                                break
                    if raw_data:
                        break
            
            if raw_data:
                target_cam = BlinkCamera(self.blink)
                target_cam.name = raw_data.get('name')
                target_cam.camera_id = raw_data.get('id')
                target_cam.network_id = raw_data.get('network_id')
                target_cam.serial = raw_data.get('serial')
                target_cam.product_type = raw_data.get('type')
            else:
                return None

        try:
            await target_cam.snap_picture()
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails() 
            return f"/images/{target_id}.jpg"
        except Exception as e:
            print(f"DEBUG: Snapshot Exception: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()