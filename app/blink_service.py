import aiohttp
import logging
import os
import json
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
            # 1. Command Sync Modules
            for name, camera in self.blink.cameras.items():
                sync_module_name = camera.attributes['sync_module']
                if sync_module_name in self.blink.sync:
                    await self.blink.sync[sync_module_name].async_arm(arm)
            
            # 2. Refresh to update global status
            await self.blink.refresh(force_cache=True)
            return True
        except Exception as e:
            print(f"DEBUG: Arming Exception: {e}")
            return False

    async def refresh(self):
        if self.blink:
            await self.blink.refresh(force_cache=True)

    async def get_status(self):
        if not self.blink: return {}
        
        try:
            await self.blink.refresh(force_cache=True)
        except Exception as e:
            print(f"DEBUG: Refresh failed: {e}")

        is_armed = False
        cameras = []
        
        # --- 1. DETERMINE SYSTEM ARM STATE ---
        if hasattr(self.blink, 'homescreen') and 'networks' in self.blink.homescreen:
            for net in self.blink.homescreen['networks']:
                if net.get('armed') is True:
                    is_armed = True
                    break
        
        # --- 2. BUILD RAW LOOKUP MAP ---
        # Map Camera ID -> Raw JSON Data (contains the real 'status')
        raw_map = {}
        if hasattr(self.blink, 'homescreen'):
            # Blink puts devices in different lists based on type
            for category in ['owls', 'cameras', 'doorbells', 'chickadees']:
                for item in self.blink.homescreen.get(category, []):
                    # Convert ID to string for reliable matching
                    raw_map[str(item.get('id'))] = item

        # --- 3. BUILD CAMERA LIST ---
        if hasattr(self.blink, 'cameras'):
            for name, cam in self.blink.cameras.items():
                
                # Find raw data for this camera
                cam_id = str(cam.camera_id)
                raw_info = raw_map.get(cam_id, {})
                
                # Determine Online Status from Raw Data
                # Status is usually 'online' or 'offline'
                online = True
                if 'status' in raw_info:
                    online = (raw_info['status'] != 'offline')
                
                cameras.append({
                    "name": name,
                    "id": cam.camera_id,
                    "serial": cam.serial,
                    "thumbnail": cam.attributes.get("thumbnail", ""),
                    "temperature": cam.attributes.get("temperature", 0),
                    "online": online,
                    # Pass pretty-printed JSON to the UI
                    "raw_json": json.dumps(raw_info, indent=2, default=str)
                })

        # Debug dump for the global 🐞 button
        debug_data = {
            "networks_raw": self.blink.homescreen.get('networks', []) if hasattr(self.blink, 'homescreen') else "No Data",
            "derived_status": "Armed" if is_armed else "Disarmed"
        }

        return {
            "armed": is_armed,
            "status_str": "Armed" if is_armed else "Disarmed",
            "cameras": cameras,
            "raw_json": json.dumps(debug_data, indent=2, default=str)
        }

    async def snap_picture(self, camera_name):
        if not self.blink: return None
        camera = self.blink.cameras.get(camera_name)
        if not camera: return None

        print(f"DEBUG: Snapping picture for {camera_name}...")
        try:
            await camera.snap_picture()
            await self.blink.refresh(force_cache=True)
            
            filename = f"/app/images/{camera_name}.jpg"
            await camera.image_to_file(filename)
            return filename
        except Exception as e:
            print(f"DEBUG: Snapshot failed: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()