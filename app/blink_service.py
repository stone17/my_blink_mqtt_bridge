import aiohttp
import logging
import os
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
        Logs in using the 'promptless' flow described in documentation.
        """
        await self.start_session()
        
        auth_data = None
        if os.path.exists(self.creds_path):
            try:
                auth_data = await json_load(self.creds_path)
            except: pass

        if username and password:
            print(f"DEBUG: Using provided credentials for {username}")
            auth_data = {
                "username": username,
                "password": password,
            }

        if not auth_data:
            return "CONFIG_REQUIRED"

        self.blink.auth = Auth(auth_data, session=self.session, no_prompt=True)

        try:
            await self.blink.start()
            await self.blink.save(self.creds_path)
            return "SUCCESS"
        except BlinkTwoFARequiredError:
            print("DEBUG: 2FA Required (Headless Flow).")
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
        """
        Exact implementation from your blink_test.py
        """
        if not self.blink: return False
        
        print(f"DEBUG: COMMAND -> {'ARM' if arm else 'DISARM'} System")
        try:
            # 1. Iterate through cameras to find sync modules and arm them
            # (Matches logic in your blink_test.py)
            for name, camera in self.blink.cameras.items():
                sync_module_name = camera.attributes['sync_module']
                print(f"DEBUG: Sending command to Sync Module '{sync_module_name}' (via camera '{name}')...")
                
                # Check if sync module exists in the map
                if sync_module_name in self.blink.sync:
                    await self.blink.sync[sync_module_name].async_arm(arm)
                else:
                    print(f"DEBUG: WARNING - Sync module '{sync_module_name}' not found in blink.sync keys!")

            # 2. Refresh from server to confirm
            print("DEBUG: Refreshing status from server...")
            await self.blink.refresh()

            # 3. Print verification (Matches your blink_test.py verification loop)
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
            # This is crucial: updates local state from Blink servers
            await self.blink.refresh()

    async def get_status(self):
        """
        Replicates the logic from blink_test.py list_cameras() to determine status.
        """
        if not self.blink: return {}
        
        # Always refresh before reading status to ensure it's not stale
        # (Your script calls blink.refresh() inside list_cameras)
        await self.blink.refresh()

        is_armed = False
        cameras = []

        print("DEBUG: --- STATUS CHECK ---")
        
        # Iterate cameras to get sync module status (User's method)
        if hasattr(self.blink, 'cameras'):
            for name, cam in self.blink.cameras.items():
                
                # Camera Details
                cameras.append({
                    "name": name,
                    "id": cam.camera_id,
                    "serial": cam.serial,
                    "thumbnail": cam.attributes.get("thumbnail", ""),
                    "temperature": cam.attributes.get("temperature", 0)
                })

                # Sync Module Status
                sync_name = cam.attributes.get('sync_module')
                if sync_name and hasattr(self.blink, 'sync') and sync_name in self.blink.sync:
                    sync_obj = self.blink.sync[sync_name]
                    print(f"DEBUG: Camera '{name}' -> Sync '{sync_name}' -> Armed: {sync_obj.arm}")
                    
                    # If ANY sync module is armed, we consider the system armed
                    if sync_obj.arm:
                        is_armed = True
                else:
                    print(f"DEBUG: Camera '{name}' has unknown sync module '{sync_name}'")

        status_str = "Armed" if is_armed else "Disarmed"
        print(f"DEBUG: Calculated Global System Status: {status_str}")
        print("DEBUG: -----------------------")

        return {
            "armed": is_armed,
            "status_str": status_str,
            "cameras": cameras
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