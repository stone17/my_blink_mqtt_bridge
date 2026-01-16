import aiohttp
import json
import logging
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

    async def login(self):
        """
        Returns: "SUCCESS", "2FA_REQUIRED", or "FAILED"
        """
        await self.start_session()
        
        # Load credentials if they exist
        try:
            auth_data = await json_load(self.creds_path)
            self.blink.auth = Auth(auth_data, session=self.session)
            print("DEBUG: Loaded existing credentials.")
        except Exception:
            print("DEBUG: No existing credentials found.")
            # If we had username/pass passed in, we would set them here, 
            # but blinkpy relies on the Auth object or prompt. 
            # We assume the config flow creates the initial Auth via file or we need to handle raw user/pass.
            # For this bridge, we rely on the saved file or generating it.
            pass

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
        """Submits the 2FA pin to Blink."""
        if not self.blink: return False
        try:
            # Blinkpy's method to send the PIN
            await self.blink.auth.send_auth_key(self.blink, code)
            # Try starting again to verify and save
            await self.blink.start()
            await self.blink.save(self.creds_path)
            return True
        except Exception as e:
            print(f"DEBUG: 2FA Validation failed: {e}")
            return False

    async def arm_system(self, arm=True):
        """Arms or Disarms all sync modules."""
        if not self.blink: return False
        try:
            # Logic from your blink_test.py
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
        """Returns simplified status for UI and MQTT."""
        if not self.blink: return {}
        
        # Determine global state (if any sync module is armed, we say armed)
        is_armed = False
        for name, sync in self.blink.sync.items():
            if sync.arm: is_armed = True
        
        cameras = []
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
        """Triggers a snapshot and saves it."""
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