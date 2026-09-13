"""Real browser signup and download journey using a disposable local identity."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None
import uvicorn
from agent_factory.identity_service import create_identity_app


@unittest.skipIf(sync_playwright is None, 'Install Playwright and Chromium for desktop browser checks')
class DesktopBrowserTests(unittest.TestCase):
    def test_new_person_can_register_download_logout_and_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b'download fixture, not an executable'
            (root / 'Core.exe').write_bytes(payload)
            (root / 'desktop-release.json').write_text(json.dumps(dict(schema_version=1,
                version='test-preview', artifacts=[dict(name='Core.exe', platform='windows-x64',
                size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())])))
            with patch.dict(os.environ, {'AGENT_FACTORY_API_TOKEN':'synthetic-browser-secret',
                'LOKVETIA_PERSONAL_REGISTRATION':'1', 'LOKVETIA_DESKTOP_RELEASE_DIR':str(root)}):
                server = uvicorn.Server(uvicorn.Config(create_identity_app(root, {}),
                    host='127.0.0.1', port=0, log_level='error', access_log=False))
                worker = threading.Thread(target=server.run, daemon=True); worker.start()
                try:
                    deadline = time.monotonic() + 15
                    while not server.started and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(server.started)
                    base = f'http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}'
                    with sync_playwright() as runtime:
                        browser = runtime.chromium.launch()
                        context = browser.new_context(viewport={'width':390,'height':844})
                        page = context.new_page()
                        errors = []; page.on('pageerror', lambda e:errors.append(str(e)))
                        page.goto(base + '/register')
                        expect(page.locator('#form')).to_be_visible()
                        page.locator('#email').fill('browser@example.invalid')
                        page.locator('#password').fill('synthetic browser password')
                        page.locator('#confirm').fill('synthetic browser password')
                        page.locator('#submit').click()
                        page.wait_for_url('**/downloads')
                        expect(page.locator('#download')).to_be_visible()
                        with page.expect_download() as download:
                            page.locator('#download').click()
                        self.assertEqual(Path(download.value.path()).read_bytes(), payload)
                        page.goto(base + '/profile')
                        expect(page.locator('#email')).to_have_text('browser@example.invalid')
                        expect(page.locator('#organization-card')).to_be_hidden()
                        page.locator('#logout').click()
                        page.wait_for_url('**/auth/account')
                        page.locator('#email').fill('browser@example.invalid')
                        page.locator('#password').fill('synthetic browser password')
                        page.locator('#submit').click()
                        page.wait_for_url('**/downloads')
                        expect(page.locator('#download')).to_be_visible()
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.should_exit = True; worker.join(10)
                    self.assertFalse(worker.is_alive())
