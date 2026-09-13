from contextlib import ExitStack,closing
from pathlib import Path
import tempfile,threading,time,unittest
from unittest.mock import patch,MagicMock
from agent_factory.web import create_app
from agent_factory import studio_updates
from agent_factory.storage import SQLiteStorage
from tests.test_studio_updates import fixture
try:
    from playwright.sync_api import sync_playwright,expect
except ImportError:sync_playwright=None


@unittest.skipIf(sync_playwright is None,'Install Playwright and Chromium')
class UpdateBrowserTests(unittest.TestCase):
    def test_check_download_current_and_offline_without_background_checks(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ',{'AGENT_FACTORY_API_TOKEN':'','AGENT_FACTORY_API_ACTOR':'Founder','AGENT_FACTORY_TEMPORAL_ENABLED':'false'}))
            fetch=stack.enter_context(patch.object(studio_updates,'fetch_json',side_effect=list(fixture())))
            installed=stack.enter_context(patch.object(studio_updates,'installed_build',return_value={'kind':'desktop','version':'0.1.0-preview.2'}))
            with closing(SQLiteStorage(Path(folder)/'state.db')):pass
            import uvicorn
            server=uvicorn.Server(uvicorn.Config(create_app(Path(folder),Path(folder)/'state.db'),host='127.0.0.1',port=0,log_level='error',access_log=False,timeout_graceful_shutdown=5))
            thread=threading.Thread(target=server.run,daemon=True);thread.start()
            deadline=time.monotonic()+15
            while not server.started and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(server.started)
            base='http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1])
            try:
                with sync_playwright() as runtime:
                    browser=runtime.chromium.launch();page=browser.new_page(viewport={'width':390,'height':844})
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(base+'/studio')
                    expect(page.locator('#updates-current')).to_contain_text('preview.2')
                    fetch.assert_not_called()
                    page.locator('#updates-check').click()
                    expect(page.locator('#updates-status')).to_contain_text('Є оновлення')
                    expect(page.locator('#updates-download')).to_be_visible()
                    url=page.locator('#updates-download').get_attribute('href')
                    self.assertEqual(url,studio_updates.REPOSITORY+'/releases/download/v0.1.0-preview.3/Lokvetia-Core-0.1.0-preview.3-win-x64.exe')
                    # Browser download uses the selected official asset URL, not a local install action.
                    page.context.route(url,lambda route:route.fulfill(status=200,body=b'exe-download-fixture',headers={'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename=Core.exe'}))
                    with page.expect_download() as download:page.locator('#updates-download').click()
                    self.assertEqual(Path(download.value.path()).read_bytes(),b'exe-download-fixture')
                    self.assertEqual(fetch.call_count,2)
                    page.reload();expect(page.locator('#updates-current')).to_contain_text('preview.2')
                    self.assertEqual(fetch.call_count,2)
                    installed.return_value={'kind':'desktop','version':'0.1.0-preview.4'}
                    page.locator('#updates-check').click()
                    expect(page.locator('#updates-status')).to_contain_text('новіша')
                    expect(page.locator('#updates-download')).to_be_hidden()
                    page.route('**/api/studio/updates/check?*',lambda route:route.fulfill(status=503,json={'error':'offline'}))
                    page.locator('#updates-check').click()
                    expect(page.locator('#updates-status')).to_contain_text('Не вдалося')
                    expect(page.locator('#updates-releases')).to_be_visible()
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),390)
                    self.assertFalse(errors)
                    page.wait_for_load_state('networkidle')
                    browser.close()
            finally:
                server.should_exit=True;thread.join(15)
                self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()
