import aiohttp
import logging
import os
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
        Attempts login. 
        - If 'username'/'password' provided, uses those (First Run).
        - If not, tries to load from 'creds_path'.
        """
        await self.start_session()
        
        auth_data = None
        
        # 1. Try loading existing token
        if os.path.exists(self.creds_path):
            try:
                auth_data = await json_load(self.creds_path)
                print("DEBUG: Loaded existing credentials from file.")
            except Exception:
                print("DEBUG: Credential file corrupt or unreadable.")

        # 2. If providing fresh credentials (first setup)
        if username and password:
            print(f"DEBUG: Using provided username/password for {username}")
            auth_data = {
                "username": username,
                "password": password,
                "login_url": "https://rest-prod.immedia-semi.com/login"
            }

        # 3. If we still have no data, we cannot start.
        if not auth_data:
            print("DEBUG: No credentials available. Waiting for user input.")
            return "CONFIG_REQUIRED"

        # 4. Initialize Auth with no_prompt=True to prevent CLI hang
        self.blink.auth = Auth(auth_data, session=self.session, no_prompt=True)

        try:
            await self.blink.start()
            print("DEBUG: Blink started successfully.")
            await self.blink.save(self.creds_path)
            return "SUCCESS"
        except BlinkTwoFARequiredError:
            print("DEBUG: 2FA Required.")
            return "2FA_REQUIRED"
        except Exception as e:
            print(f"DEBUG: Login failed: {e}")
            return "FAILED"

    async def validate_2fa(self, code):
        if not self.blink: return False
        try:
            await self.blink.auth.send_auth_key(self.blink, code)
            await self.blink.start()
            await self.blink.save(self.creds_path)
            return True
        except Exception as e:
            print(f"DEBUG: 2FA Validation failed: {e}")
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
        # Check all sync modules
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