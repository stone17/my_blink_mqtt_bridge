import aiohttp
import logging
import os
import json
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
        self.images_dir = "/config/images"
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

    def _get_all_cameras(self):
        """
        Helper: Generates BlinkCamera objects for ALL devices in raw data.
        This includes duplicates that might be missing from blink.cameras.
        """
        if not self.blink or not hasattr(self.blink, 'homescreen'): 
            return []

        all_objs = []
        # Categories that contain cameras
        for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
            raw_list = self.blink.homescreen.get(category, [])
            for raw_data in raw_list:
                # Instantiate a clean BlinkCamera object using the library
                cam = BlinkCamera(self.blink)
                # Populate it using the library's internal update method
                cam.update(raw_data)
                all_objs.append(cam)
        return all_objs

    async def download_thumbnails(self):
        """
        Uses the native blinkpy image_to_file method to save thumbnails.
        """
        print(f"DEBUG: Downloading thumbnails to {self.images_dir}...")
        
        cameras = self._get_all_cameras()
        if not cameras:
            print("DEBUG: No cameras found to download.")
            return

        for cam in cameras:
            try:
                # Save as {ID}.jpg
                path = f"{self.images_dir}/{cam.camera_id}.jpg"
                
                # NATIVE METHOD: Handles auth, headers, and URLs automatically
                await cam.image_to_file(path)
                
                print(f"DEBUG: Saved {cam.name} -> {path}")
            except Exception as e:
                print(f"DEBUG: Failed to save image for {cam.name}: {e}")

    async def arm_system(self, arm=True):
        if not self.blink: return False
        print(f"DEBUG: COMMAND -> {'ARM' if arm else 'DISARM'} System")
        try:
            # Send command to all sync modules found
            cameras = self._get_all_cameras()
            processed_syncs = set()
            
            for cam in cameras:
                # We need to find the sync module associated with this camera
                sync_name = cam.attributes.get('sync_module')
                if sync_name and sync_name in self.blink.sync and sync_name not in processed_syncs:
                    await self.blink.sync[sync_name].async_arm(arm)
                    processed_syncs.add(sync_name)
            
            await self.blink.refresh(force_cache=True)
            return True
        except Exception as e:
            print(f"DEBUG: Arming Exception: {e}")
            return False

    async def refresh(self):
        if self.blink:
            print("DEBUG: Refreshing Data & Thumbnails...")
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails()

    async def get_status(self):
        if not self.blink: return {}
        
        try:
            await self.refresh()
        except Exception as e:
            print(f"DEBUG: Refresh failed: {e}")

        is_armed = False
        
        # Check Global Arm Status
        if hasattr(self.blink, 'homescreen') and 'networks' in self.blink.homescreen:
            for net in self.blink.homescreen['networks']:
                if net.get('armed') is True:
                    is_armed = True
                    break
        
        # Build Camera List using our robust helper
        cameras_list = []
        raw_objs = self._get_all_cameras()
        
        # Handle duplicate names for display
        name_counts = {}
        for c in raw_objs:
            name_counts[c.name] = name_counts.get(c.name, 0) + 1

        for cam in raw_objs:
            display_name = cam.name
            if name_counts[cam.name] > 1:
                display_name = f"{cam.name} ({cam.product_type})"
            
            # Robust online check
            # library 'status' attribute is sometimes messy, check raw if needed
            online = True
            if cam.attributes.get('status') == 'offline':
                online = False
            
            cameras_list.append({
                "name": display_name,
                "id": str(cam.camera_id),
                "serial": cam.serial,
                "temperature": cam.attributes.get('temperature', 0),
                "online": online,
                "raw_json": json.dumps(cam.attributes, indent=2, default=str)
            })

        debug_data = {
            "networks_raw": self.blink.homescreen.get('networks', []) if hasattr(self.blink, 'homescreen') else "No Data",
            "camera_count": len(raw_objs)
        }

        return {
            "armed": is_armed,
            "status_str": "Armed" if is_armed else "Disarmed",
            "cameras": cameras_list,
            "raw_json": json.dumps(debug_data, indent=2, default=str)
        }

    async def snap_picture(self, target_id):
        if not self.blink: return None
        target_id = str(target_id)
        
        print(f"DEBUG: Requesting SNAP for Camera ID {target_id}...")

        # Find the correct camera object
        target_cam = None
        for cam in self._get_all_cameras():
            if str(cam.camera_id) == target_id:
                target_cam = cam
                break
        
        if not target_cam:
            print("DEBUG: Camera ID not found.")
            return None

        try:
            # Use native method
            await target_cam.snap_picture()
            
            # Refresh to update link
            await self.blink.refresh(force_cache=True)
            
            # Download just this image using native method
            path = f"{self.images_dir}/{target_id}.jpg"
            await target_cam.image_to_file(path)
            
            return f"/images/{target_id}.jpg"
        except Exception as e:
            print(f"DEBUG: Snapshot Exception: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()