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
        # 1. Try loading existing token
        if os.path.exists(self.creds_path):
            try:
                auth_data = await json_load(self.creds_path)
            except: pass

        # 2. If providing fresh credentials (first setup)
        if username and password:
            print(f"DEBUG: Using provided credentials for {username}")
            auth_data = {
                "username": username,
                "password": password,
            }

        if not auth_data:
            return "CONFIG_REQUIRED"

        # Initialize Auth with no_prompt=True for headless operation
        self.blink.auth = Auth(auth_data, session=self.session, no_prompt=True)

        try:
            await self.blink.start()
            # If successful, save immediately
            await self.blink.save(self.creds_path)
            return "SUCCESS"
        except BlinkTwoFARequiredError:
            print("DEBUG: 2FA Required (Headless Flow).")
            return "2FA_REQUIRED"
        except Exception as e:
            print(f"DEBUG: Login failed: {e}")
            return "FAILED"

    async def validate_2fa(self, code):
        """
        Completes the 2FA process using the key provided by the user.
        """
        if not self.blink or not self.blink.auth: return False
        
        print(f"DEBUG: Validating 2FA code '{code}'...")

        try:
            # OPTION 1: The official promptless method
            if hasattr(self.blink.auth, "send_auth_key"):
                print("DEBUG: Using blink.auth.send_auth_key()")
                await self.blink.auth.send_auth_key(self.blink, code)
                await self.blink.setup_post_verify()
                
            # OPTION 2: Fallback to prompt_2fa injection if method is missing
            else:
                print("DEBUG: send_auth_key missing. Falling back to prompt_2fa injection.")
                # Patch input() to simulate user typing the code
                with patch('builtins.input', side_effect=[code]):
                    await self.blink.prompt_2fa()

            # Save the new token
            await self.blink.save(self.creds_path)
            print("DEBUG: 2FA Success. Credentials saved.")
            return True

        except Exception as e:
            print(f"DEBUG: 2FA Validation Failed: {e}")
            # Extended debug to help identify issue if it persists
            try:
                print(f"DEBUG: Auth methods available: {[d for d in dir(self.blink.auth) if not d.startswith('_')]}")
            except: pass
            return False

    async def arm_system(self, arm=True):
        if not self.blink: return False
        try:
            for name, camera in self.blink.cameras.items():
                sync_name = camera.attributes['sync_module']
                await self.blink.sync[sync_name].async_arm(arm)
            await self.blink.refresh()
            return True
        except Exception as e:
            print(f"DEBUG: Arming error: {e}")
            return False

    async def refresh(self):
        if self.blink:
            await self.blink.refresh()

    async def get_status(self):
        if not self.blink: return {}
        is_armed = False
        
        if hasattr(self.blink, 'sync'):
            for name, sync in self.blink.sync.items():
                if sync.arm: is_armed = True
        
        cameras = []
        if hasattr(self.blink, 'cameras'):
            for name, cam in self.blink.cameras.items():
                cameras.append({
                    "name": name,
                    "id": cam.camera_id,
                    "serial": cam.serial,
                    "thumbnail": cam.attributes.get("thumbnail", ""),
                    "temperature": cam.attributes.get("temperature", 0)
                })

        return {
            "armed": is_armed,
            "status_str": "Armed" if is_armed else "Disarmed",
            "cameras": cameras
        }

    async def snap_picture(self, camera_name):
        if not self.blink: return None
        camera = self.blink.cameras.get(camera_name)
        if not camera: return None

        print(f"DEBUG: Snapping picture for {camera_name}...")
        await camera.snap_picture()
        await self.blink.refresh()
        
        filename = f"/app/images/{camera_name}.jpg"
        await camera.image_to_file(filename)
        return filename

    async def close(self):
        if self.session:
            await self.session.close()