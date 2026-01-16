import aiohttp
import logging
import os
import json
import shutil
from unittest.mock import patch
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
            print(f"DEBUG: Verified/Created {self.images_dir}")
        except Exception as e:
            print(f"DEBUG: CRITICAL - Could not create image dir: {e}")

    async def start_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
            self.blink = Blink(session=self.session)

    async def login(self, username=None, password=None):
        await self.start_session()
        
        auth_data = None
        if os.path.exists(self.creds_path):
            try:
                auth_data = await json_load(self.creds_path)
            except Exception as e: 
                print(f"DEBUG: Failed to load existing credentials file: {e}")

        if not auth_data and username and password:
            auth_data = {"username": username, "password": password}

        if not auth_data:
            return "CONFIG_REQUIRED"

        self.blink.auth = Auth(auth_data, session=self.session, no_prompt=True)

        try:
            await self.blink.start()
            await self.blink.save(self.creds_path)
            # Print Region Info to debug URL issues
            if hasattr(self.blink, 'urls'):
                print(f"DEBUG: Blink Base URL determined as: {self.blink.urls.base_url}")
            return "SUCCESS"
        except BlinkTwoFARequiredError:
            return "2FA_REQUIRED"
        except Exception as e:
            print(f"DEBUG: Login failed: {e}")
            return "FAILED"

    async def validate_2fa(self, code):
        if not self.blink or not self.blink.auth: return False
        try:
            if hasattr(self.blink.auth, "send_auth_key"):
                await self.blink.auth.send_auth_key(self.blink, code)
                await self.blink.setup_post_verify()
            else:
                with patch('builtins.input', side_effect=[code]):
                    await self.blink.prompt_2fa()

            await self.blink.save(self.creds_path)
            return True
        except Exception as e:
            print(f"DEBUG: 2FA Validation Failed: {e}")
            return False

    async def arm_system(self, arm=True):
        if not self.blink: return False
        print(f"DEBUG: COMMAND -> {'ARM' if arm else 'DISARM'} System")
        try:
            for name, camera in self.blink.cameras.items():
                sync_module_name = camera.attributes['sync_module']
                if sync_module_name in self.blink.sync:
                    await self.blink.sync[sync_module_name].async_arm(arm)
            
            await self.blink.refresh(force_cache=True)
            return True
        except Exception as e:
            print(f"DEBUG: Arming Exception: {e}")
            return False

    async def refresh(self):
        if self.blink:
            print("DEBUG: Refreshing Blink Data...")
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails()

    async def download_thumbnails(self):
        """Downloads thumbnails for ALL cameras found in raw homescreen data."""
        if not self.blink: return
        
        print(f"DEBUG: Starting Thumbnail Download to {self.images_dir}...")
        
        # Collect all raw devices
        all_devices = []
        if hasattr(self.blink, 'homescreen'):
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                devs = self.blink.homescreen.get(category, [])
                all_devices.extend(devs)
                print(f"DEBUG: Found {len(devs)} devices in category '{category}'")

        if not all_devices:
            print("DEBUG: WARNING - No devices found in homescreen data to fetch images for.")

        for dev in all_devices:
            cam_id = str(dev.get('id'))
            name = dev.get('name', 'Unknown')
            thumb_url = dev.get('thumbnail')
            
            print(f"DEBUG: Processing Image for '{name}' (ID: {cam_id})...")
            
            if thumb_url:
                # Construct Full URL
                if not thumb_url.startswith('http'):
                    base = "https://rest-prod.immedia-semi.com"
                    if hasattr(self.blink, 'urls') and self.blink.urls.base_url: 
                        base = self.blink.urls.base_url
                    
                    # Ensure no double slashes
                    if base.endswith('/') and thumb_url.startswith('/'):
                        thumb_url = thumb_url[1:]
                    
                    full_url = f"{base}{thumb_url}"
                else:
                    full_url = thumb_url

                print(f"DEBUG:   > Constructed URL: {full_url}")

                try:
                    path = f"{self.images_dir}/{cam_id}.jpg"
                    async with self.session.get(full_url) as resp:
                        print(f"DEBUG:   > HTTP Status: {resp.status}")
                        
                        if resp.status == 200:
                            data = await resp.read()
                            size = len(data)
                            with open(path, 'wb') as f:
                                f.write(data)
                            print(f"DEBUG:   > SAVED: {path} ({size} bytes)")
                        else:
                            print(f"DEBUG:   > ERROR: Failed to fetch. Status {resp.status}")
                            # Optional: print body if 404/403 to see error message
                            # print(await resp.text())
                except Exception as e:
                    print(f"DEBUG:   > EXCEPTION downloading ID {cam_id}: {e}")
            else:
                print(f"DEBUG:   > NO THUMBNAIL URL found for this device.")

    async def get_status(self):
        if not self.blink: return {}
        
        try:
            await self.refresh() 
        except Exception as e:
            print(f"DEBUG: Refresh failed: {e}")

        is_armed = False
        cameras = []
        
        # Check Global Arm Status
        if hasattr(self.blink, 'homescreen') and 'networks' in self.blink.homescreen:
            for net in self.blink.homescreen['networks']:
                if net.get('armed') is True:
                    is_armed = True
                    break
        
        # Build Camera List from Raw Data
        raw_devices = []
        if hasattr(self.blink, 'homescreen'):
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                for item in self.blink.homescreen.get(category, []):
                    item['category_type'] = category
                    raw_devices.append(item)

        # Handle duplicate names for display
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
            for _, c_obj in self.blink.cameras.items():
                if str(c_obj.camera_id) == cam_id:
                    temp = c_obj.attributes.get('temperature', 0)
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
            "networks_raw": self.blink.homescreen.get('networks', []) if hasattr(self.blink, 'homescreen') else "No Data",
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

        # 1. Try finding in loaded library objects
        target_cam = None
        for _, cam in self.blink.cameras.items():
            if str(cam.camera_id) == target_id:
                target_cam = cam
                break
        
        # 2. Reconstruct if missing
        if not target_cam:
            print(f"DEBUG: Camera ID {target_id} not found in library objects. Attempting reconstruction...")
            raw_data = None
            if hasattr(self.blink, 'homescreen'):
                for cat in ['owls', 'cameras', 'doorbells', 'chickadees']:
                    for item in self.blink.homescreen.get(cat, []):
                        if str(item.get('id')) == target_id:
                            raw_data = item
                            break
            
            if raw_data:
                target_cam = BlinkCamera(self.blink)
                target_cam.name = raw_data.get('name')
                target_cam.camera_id = raw_data.get('id')
                target_cam.network_id = raw_data.get('network_id')
                target_cam.serial = raw_data.get('serial')
                target_cam.product_type = raw_data.get('type')
                print(f"DEBUG: Reconstructed camera object for {target_cam.name}")
            else:
                print("DEBUG: ERROR - Could not find raw data for this ID to reconstruct.")
                return None

        try:
            print(f"DEBUG: Triggering API Snap command...")
            await target_cam.snap_picture()
            
            print("DEBUG: Snap command sent. Waiting for refresh...")
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails() 
            
            return f"/images/{target_id}.jpg"
        except Exception as e:
            print(f"DEBUG: Snapshot Exception: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()