import aiohttp
import logging
import os
import json
import shutil
from unittest.mock import patch
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.helpers.util import json_load

_LOGGER = logging.getLogger(__name__)

class BlinkService:
    def __init__(self, creds_path):
        self.creds_path = creds_path
        self.session = None
        self.blink = None
        self.images_dir = "/app/images"
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
            # Arm/Disarm using the library's merged dictionary for convenience
            # (Sync Modules handle the commands even if names duplicate, usually)
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
            # After refreshing data, download the latest thumbnails to disk
            await self.download_thumbnails()

    async def download_thumbnails(self):
        """
        Downloads the current thumbnail from Blink's servers for every camera.
        Does NOT trigger a new snap (saves battery).
        """
        if not self.blink: return
        
        print("DEBUG: Caching latest thumbnails...")
        
        # We use the internal 'homescreen' lists to find ALL cameras
        all_devices = []
        if hasattr(self.blink, 'homescreen'):
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                all_devices.extend(self.blink.homescreen.get(category, []))

        for dev in all_devices:
            name = dev.get('name', 'Unknown')
            thumb_url = dev.get('thumbnail')
            
            if thumb_url:
                # Handle full URL vs relative URL
                if not thumb_url.startswith('http'):
                    base = "https://rest-prod.immedia-semi.com" # Fallback, usually overridden by region
                    if hasattr(self.blink, 'urls'):
                         base = self.blink.urls.base_url
                    thumb_url = f"{base}{thumb_url}"
                
                try:
                    # Save to /app/images/<NAME>.jpg
                    # Use cleaned name to avoid filesystem issues
                    clean_name = name.replace(" ", "_").replace("/", "-")
                    path = f"{self.images_dir}/{clean_name}.jpg"
                    
                    # Only download if we don't have it, or maybe every time?
                    # For now, let's download to ensure it's fresh.
                    async with self.session.get(thumb_url) as resp:
                        if resp.status == 200:
                            with open(path, 'wb') as f:
                                f.write(await resp.read())
                except Exception as e:
                    print(f"DEBUG: Failed to download thumb for {name}: {e}")

    async def get_status(self):
        if not self.blink: return {}
        
        # 1. Refresh Data & Images
        try:
            await self.refresh() 
        except Exception as e:
            print(f"DEBUG: Refresh failed: {e}")

        is_armed = False
        cameras = []
        
        # 2. Check Global Arm Status
        if hasattr(self.blink, 'homescreen') and 'networks' in self.blink.homescreen:
            for net in self.blink.homescreen['networks']:
                if net.get('armed') is True:
                    is_armed = True
                    break
        
        # 3. Build Camera List (Handling Duplicates)
        # We collect all raw devices first
        raw_devices = []
        if hasattr(self.blink, 'homescreen'):
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                for item in self.blink.homescreen.get(category, []):
                    item['category_type'] = category
                    raw_devices.append(item)

        # Track names to handle duplicates
        name_counts = {}
        for d in raw_devices:
            n = d.get('name')
            name_counts[n] = name_counts.get(n, 0) + 1

        name_indices = {}

        for dev in raw_devices:
            original_name = dev.get('name', 'Unknown')
            
            # Handle Duplicate Names
            display_name = original_name
            if name_counts[original_name] > 1:
                # Append ID or Type to make it unique
                # e.g. "Entrance (Outdoor)" vs "Entrance (Mini)"
                dev_type = dev.get('type', 'cam')
                display_name = f"{original_name} ({dev_type})"
            
            # Check Online Status
            online = True
            if 'status' in dev:
                online = (dev['status'] != 'offline')

            # Image path (matches what we saved in download_thumbnails)
            clean_name = original_name.replace(" ", "_").replace("/", "-")
            
            # Extract Temp (if available)
            # Raw dict might not have it directly if it's not processed by blinkpy
            # We try to look it up in the blinkpy objects if possible, else default 0
            temp = 0
            # Try to match with processed camera object for sensor data
            # (Note: this lookup might fail for the "hidden" duplicate camera)
            for _, c_obj in self.blink.cameras.items():
                if str(c_obj.camera_id) == str(dev.get('id')):
                    temp = c_obj.attributes.get('temperature', 0)
                    break

            cameras.append({
                "name": display_name,
                "file_name": clean_name, # used for <img> tag
                "id": dev.get('id'),
                "serial": dev.get('serial'),
                "temperature": temp,
                "online": online,
                "raw_json": json.dumps(dev, indent=2, default=str)
            })

        # Debug Data
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

    async def snap_picture(self, camera_name):
        if not self.blink: return None
        
        # Finding camera object by name is tricky with duplicates.
        # We try to find the best match.
        target_cam = None
        for name, cam in self.blink.cameras.items():
            # If the user passed "Entrance (owl)", we need to handle that mapping 
            # or just rely on simple names. 
            # For simplicity, we try exact match first.
            if name == camera_name:
                target_cam = cam
                break
        
        # Fallback: scan by ID if we passed an ID (not implemented in UI yet)
        if not target_cam:
            # If we renamed it in display, we might fail here. 
            # Basic fallback: match strict name
            target_cam = self.blink.cameras.get(camera_name)

        if not target_cam: 
            print(f"DEBUG: Could not find camera object for '{camera_name}'")
            return None

        print(f"DEBUG: Snapping picture for {camera_name}...")
        try:
            await target_cam.snap_picture()
            await self.blink.refresh(force_cache=True)
            await self.download_thumbnails() # Update local cache
            
            clean_name = camera_name.replace(" ", "_").replace("/", "-")
            return f"/app/images/{clean_name}.jpg"
        except Exception as e:
            print(f"DEBUG: Snapshot failed: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()