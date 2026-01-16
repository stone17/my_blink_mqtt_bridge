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
        # Save images in /config/images so they persist and user can see them
        self.images_dir = "/config/images"
        os.makedirs(self.images_dir, exist_ok=True)

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
            # Send command to all known sync modules
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
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails()

    async def download_thumbnails(self):
        """Downloads thumbnails for ALL cameras found in raw homescreen data."""
        if not self.blink: return
        
        print("DEBUG: Caching latest thumbnails to /config/images...")
        
        # Collect all raw devices
        all_devices = []
        if hasattr(self.blink, 'homescreen'):
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                all_devices.extend(self.blink.homescreen.get(category, []))

        for dev in all_devices:
            cam_id = str(dev.get('id'))
            thumb_url = dev.get('thumbnail')
            
            if thumb_url:
                if not thumb_url.startswith('http'):
                    base = "https://rest-prod.immedia-semi.com"
                    if hasattr(self.blink, 'urls'): base = self.blink.urls.base_url
                    thumb_url = f"{base}{thumb_url}"
                
                try:
                    # Save as {ID}.jpg to avoid name collisions
                    path = f"{self.images_dir}/{cam_id}.jpg"
                    async with self.session.get(thumb_url) as resp:
                        if resp.status == 200:
                            with open(path, 'wb') as f:
                                f.write(await resp.read())
                except Exception as e:
                    print(f"DEBUG: Failed to download thumb for ID {cam_id}: {e}")

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
        
        # Build Camera List from Raw Data (Handles duplicates & missing items)
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
            
            # Display Name Logic
            display_name = original_name
            if name_counts[original_name] > 1:
                dev_type = dev.get('type', 'cam')
                display_name = f"{original_name} ({dev_type})"
            
            online = True
            if 'status' in dev:
                online = (dev['status'] != 'offline')

            # Try to get Temp from library object if it exists
            temp = 0
            for _, c_obj in self.blink.cameras.items():
                if str(c_obj.camera_id) == cam_id:
                    temp = c_obj.attributes.get('temperature', 0)
                    break

            cameras.append({
                "name": display_name,
                "id": cam_id,           # Crucial: ID is now the key
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
        """
        Takes a picture using Camera ID. 
        Reconstructs camera object if missing from library due to duplicates.
        """
        if not self.blink: return None
        target_id = str(target_id)
        
        print(f"DEBUG: Looking for camera ID {target_id} to snap...")

        # 1. Try finding in loaded library objects
        target_cam = None
        for _, cam in self.blink.cameras.items():
            if str(cam.camera_id) == target_id:
                target_cam = cam
                break
        
        # 2. If not found (overwritten duplicate), reconstruct it manually
        if not target_cam:
            print(f"DEBUG: Camera ID {target_id} not in library dict. Reconstructing...")
            # Find raw data
            raw_data = None
            if hasattr(self.blink, 'homescreen'):
                for cat in ['owls', 'cameras', 'doorbells', 'chickadees']:
                    for item in self.blink.homescreen.get(cat, []):
                        if str(item.get('id')) == target_id:
                            raw_data = item
                            break
            
            if raw_data:
                # Create temporary object to perform the API call
                target_cam = BlinkCamera(self.blink)
                target_cam.name = raw_data.get('name')
                target_cam.camera_id = raw_data.get('id')
                target_cam.network_id = raw_data.get('network_id')
                target_cam.serial = raw_data.get('serial')
                target_cam.product_type = raw_data.get('type')
            else:
                print("DEBUG: Could not find raw data for this ID.")
                return None

        # 3. Perform Snap
        try:
            print(f"DEBUG: Snapping picture for {target_cam.name} (ID: {target_id})...")
            await target_cam.snap_picture()
            
            # Refresh and download just this image
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails() 
            
            return f"/images/{target_id}.jpg"
        except Exception as e:
            print(f"DEBUG: Snapshot failed: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()