import asyncio
import os
import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from app.blink_service import BlinkService
from blinkpy.auth import BlinkTwoFARequiredError

async def test_blink_service_login_and_2fa():
    with tempfile.TemporaryDirectory() as tmpdir:
        creds_path = os.path.join(tmpdir, "creds.json")
        svc = BlinkService(creds_path)
        
        # Test 1: Config required when no credentials
        res = await svc.login()
        assert res == "CONFIG_REQUIRED", f"Expected CONFIG_REQUIRED, got {res}"
        print("Test 1 passed: CONFIG_REQUIRED returned when credentials missing")
        await svc.close()

        # Test 2: Login with username/password when start() raises 2FA_REQUIRED
        with patch("app.blink_service.Blink") as mock_blink_cls:
            mock_blink = MagicMock()
            mock_blink_cls.return_value = mock_blink
            
            async def mock_start_2fa():
                raise BlinkTwoFARequiredError("2FA Required")
                
            mock_blink.start = mock_start_2fa
            
            res = await svc.login("user@example.com", "secretpass")
            assert res == "2FA_REQUIRED", f"Expected 2FA_REQUIRED, got {res}"
            assert svc.blink.auth.data.get("username") == "user@example.com"
            assert svc.blink.auth.data.get("password") == "secretpass"
            print("Test 2 passed: 2FA_REQUIRED returned and credentials correctly merged")

            # Test 3: validate_2fa with send_2fa_code success
            with patch.object(svc.blink, "send_2fa_code", new_callable=AsyncMock) as mock_send_2fa, \
                 patch.object(svc.blink, "save", new_callable=AsyncMock) as mock_save:
                mock_send_2fa.return_value = True
                
                val_res = await svc.validate_2fa("123456")
                assert val_res is True, "Expected validate_2fa to return True"
                mock_send_2fa.assert_called_once_with("123456")
                mock_save.assert_called_once_with(creds_path)
                print("Test 3 passed: validate_2fa successfully calls send_2fa_code and save")

        await svc.close()

        # Test 4: Existing creds file missing username/password gets config credentials merged
        with open(creds_path, "w") as f:
            json.dump({"token": "old_token"}, f)
            
        with patch("app.blink_service.Blink") as mock_blink_cls:
            mock_blink = MagicMock()
            mock_blink.urls = None  # test safety when urls is None
            mock_blink_cls.return_value = mock_blink
            
            async def mock_start_success():
                return True
                
            mock_blink.start = mock_start_success
            mock_blink.save = AsyncMock()
            
            res = await svc.login("user@example.com", "secretpass")
            assert res == "SUCCESS", f"Expected SUCCESS, got {res}"
            assert svc.blink.auth.data.get("username") == "user@example.com"
            assert svc.blink.auth.data.get("password") == "secretpass"
            print("Test 4 passed: Existing creds merged with config credentials without AttributeError on None urls")

            # Test 5: get_status and download_thumbnails when homescreen/urls are None
            mock_blink.homescreen = None
            mock_blink.urls = None
            status = await svc.get_status()
            assert status.get("armed") is False
            print("Test 5 passed: get_status safe when homescreen is None")

        await svc.close()

if __name__ == "__main__":
    asyncio.run(test_blink_service_login_and_2fa())
