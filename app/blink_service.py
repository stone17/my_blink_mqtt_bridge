import aiohttp
import logging
import os
import json  # <--- Added
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
        """
        Logs in. Prioritizes existing credential file to avoid repeated 2FA.
        """
        await self.start_session()
        
        auth_data = None
        
        # 1. Try loading existing token from file FIRST
        if os.path.exists(self.creds_path):
            try:
                auth_data = await json_load(self.creds_path)
                print("DEBUG: Loaded existing credentials from file.")
            except Exception as e: 
                print(f"DEBUG: Failed to load existing credentials file: {e}")

        # 2. Only use provided username/password if we DO NOT have a valid token file
        if not auth_data and username and password:
            print(f"DEBUG: No token file found. Using provided credentials for {username}")
            auth_data = {
                "username": username,
                "password": password,
            }

        if not auth_data:
            print("DEBUG: No auth data available (no file and no config credentials).")
            return "CONFIG_REQUIRED"

        # Initialize Auth
        self.blink.auth = Auth(auth_data, session=self.session, no_prompt=True)

        try:
            await self.blink.start()
            await self.blink.save(self.creds_path)
            return "SUCCESS"
        except BlinkTwoFARequiredError:
            print("DEBUG: 2FA Required.")
            return "2FA_REQUIRED"
        except Exception as e:
            print(f"DEBUG: Login failed: {e}")
            return "FAILED"

    async def validate_2fa(self, code):
        if not self.blink or not self.blink.auth: return False
        
        print(f"DEBUG: Validating 2FA code '{code}'...")
        try:
            # OPTION 1: Official method
            if hasattr(self.blink.auth, "send_auth_key"):
                print("DEBUG: Using blink.auth.send_auth_key()")
                await self.blink.auth.send_auth_key(self.blink, code)
                await self.blink.setup_post_verify()
                
            # OPTION 2: Fallback to prompt_2fa injection
            else:
                print("DEBUG: send_auth_key missing. Falling back to prompt_2fa injection.")
                with patch('builtins.input', side_effect=[code]):
                    await self.blink.prompt_2fa()

            await self.blink.save(self.creds_path)
            print("DEBUG: 2FA Success. Credentials saved.")
            return True
        except Exception as e:
            print(f"DEBUG: 2FA Validation Failed: {e}")
            return False

    async def arm_system(self, arm=True):
        if not self.blink: return False
        
        print(f"DEBUG: COMMAND -> {'ARM' if arm else 'DISARM'} System")
        try:
            # Logic from your blink_test.py
            for name, camera in self.blink.cameras.items():
                sync_module_name = camera.attributes['sync_module']
                if sync_module_name in self.blink.sync:
                    await self.blink.sync[sync_module_name].async_arm(arm)
            
            await self.blink.refresh()

            # Verification debug
            for name, camera in self.blink.cameras.items():
                sync_module_name = camera.attributes['sync_module']
                if sync_module_name in self.blink.sync:
                    arm_status = self.blink.sync[sync_module_name]
                    print(f"DEBUG: POST-ACTION STATUS: {arm_status.name} arm status: {arm_status.arm}")
            
            return True
        except Exception as e:
            print(f"DEBUG: Arming Exception: {e}")
            return False

    async def refresh(self):
        if self.blink:
            await self.blink.refresh()

    async def get_status(self):
        if not self.blink: return {}
        
        try:
            await self.blink.refresh()
        except Exception as e:
            print(f"DEBUG: Refresh failed during status check: {e}")

        is_armed = False
        cameras = []
        
        # --- DEBUG DATA COLLECTION ---
        debug_data = {
            "sync_modules": {},
            "cameras": {}
        }
        # -----------------------------

        if hasattr(self.blink, 'cameras'):
            for name, cam in self.blink.cameras.items():
                
                # Check online status
                online = True
                if hasattr(cam, 'online'):
                    online = cam.online
                elif 'status' in cam.attributes:
                    online = (cam.attributes['status'] == 'online')

                cameras.append({
                    "name": name,
                    "id": cam.camera_id,
                    "serial": cam.serial,
                    "thumbnail": cam.attributes.get("thumbnail", ""),
                    "temperature": cam.attributes.get("temperature", 0),
                    "online": online
                })
                
                # Add to debug dump
                debug_data["cameras"][name] = cam.attributes

                # Check Sync Module Arm Status
                sync_name = cam.attributes.get('sync_module')
                if sync_name and hasattr(self.blink, 'sync') and sync_name in self.blink.sync:
                    sync_obj = self.blink.sync[sync_name]
                    
                    # Capture Sync Module Data for Debug
                    if sync_name not in debug_data["sync_modules"]:
                        debug_data["sync_modules"][sync_name] = {
                            "arm_property": sync_obj.arm,
                            "attributes": sync_obj.attributes
                        }

                    if sync_obj.arm:
                        is_armed = True

        return {
            "armed": is_armed,
            "status_str": "Armed" if is_armed else "Disarmed",
            "cameras": cameras,
            "raw_json": json.dumps(debug_data, indent=2, default=str) # <--- Passed to UI
        }

    async def snap_picture(self, camera_name):
        if not self.blink: return None
        camera = self.blink.cameras.get(camera_name)
        if not camera: return None

        print(f"DEBUG: Snapping picture for {camera_name}...")
        try:
            await camera.snap_picture()
            await self.blink.refresh()
            
            filename = f"/app/images/{camera_name}.jpg"
            await camera.image_to_file(filename)
            print(f"DEBUG: Image saved to {filename}")
            return filename
        except Exception as e:
            print(f"DEBUG: Snapshot failed: {e}")
            return None

    async def close(self):
        if self.session:
            await self.session.close()