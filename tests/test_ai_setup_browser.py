"""Real Chromium + production routes, synthetic CLI and credential store."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent_factory.web import create_app
from tests.test_credential_connections import MemoryStore
try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright=None


@unittest.skipIf(sync_playwright is None,'Install Playwright and Chromium')
class AIConnectBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=sync_playwright().start();cls.browser=cls.runtime.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.runtime.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.stack=ExitStack()
        self.stack.enter_context(patch.dict(os.environ,{'AGENT_FACTORY_API_TOKEN':'','AGENT_FACTORY_API_ACTOR':'Founder',
            'AGENT_FACTORY_API_ROLE':'operations_owner','AGENT_FACTORY_API_SCOPES':'read,write,control',
            'AGENT_FACTORY_API_TENANTS':'local','AGENT_FACTORY_TEMPORAL_ENABLED':'false','GEMINI_API_KEY':''}))
        self.stack.enter_context(patch('agent_factory.ai_setup.this_machine',return_value=SimpleNamespace(is_the_users_computer=True)))
        self.stack.enter_context(patch('agent_factory.ai_setup.launcher',return_value=['official-cli']))
        self.app=create_app(self.root,self.root/'state.db')
        self.setup=self.app.state.ai_setup;self.setup.store=MemoryStore()
        self.release=threading.Event();self.release.set()
        self.stack.enter_context(patch.object(self.setup,'process',side_effect=self.fake_process))
        import uvicorn
        self.server=uvicorn.Server(uvicorn.Config(self.app,host='127.0.0.1',port=0,log_level='error',access_log=False))
        self.thread=threading.Thread(target=self.server.run,daemon=True);self.thread.start()
        deadline=time.monotonic()+10
        while not self.server.started and time.monotonic()<deadline:time.sleep(.01)
        if not self.server.started:raise RuntimeError('Server failed to start')
        self.url=f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.context=self.browser.new_context();self.page=self.context.new_page();self.page.set_default_timeout(10000)
        self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)))

    def tearDown(self):
        self.release.set();self.context.close();self.server.should_exit=True;self.thread.join(10)
        self.setup.executor.shutdown(wait=True,cancel_futures=True)
        self.stack.close();self.temp.cleanup()
        self.assertFalse(self.thread.is_alive());self.assertEqual(self.errors,[])

    def fake_process(self,command,**kwargs):
        while not self.release.wait(.05):kwargs['check']()
        kwargs['check']()
        if '--output-last-message' in command:
            Path(command[command.index('--output-last-message')+1]).write_text('LOKVETIA_OK')
        if '--prompt' in command and not kwargs['env'].get('GEMINI_API_KEY'):
            return 1,'UNSUPPORTED_CLIENT'
        return 0,'{"response":"LOKVETIA_OK"}'

    def test_connect_reload_resume_done_and_disconnect(self):
        self.release.clear()
        self.page.goto(self.url+'/connect')
        expect(self.page.locator('[data-provider]')).to_have_count(3)
        self.assertEqual(self.setup.scan('Founder')['providers'][0]['job'],None)
        self.page.locator('[data-provider=codex]').click()
        expect(self.page.locator('#cancel')).to_be_visible()
        self.page.reload();expect(self.page.locator('#cancel')).to_be_visible()
        self.assertTrue(self.page.locator('[data-provider=gemini]').is_disabled())
        self.release.set();expect(self.page.locator('#done')).to_be_visible()
        self.page.reload();expect(self.page.locator('#ready-banner')).to_be_visible()
        self.page.get_by_role('button',name='Відключити',exact=True).click()
        expect(self.page.locator('#ready-banner')).to_be_hidden()

    def test_mobile_gemini_key_only_when_needed_cleared_after_submit(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.goto(self.url+'/connect')
        expect(self.page.locator('#key-form')).to_be_hidden()
        self.page.locator('[data-provider=gemini]').click()
        expect(self.page.locator('#key-form')).to_be_visible()
        self.page.reload();self.page.get_by_role('button',name='Ввести ключ',exact=True).click()
        key='synthetic-browser-key-12345'
        self.page.locator('#gemini-key').fill(key)
        self.assertEqual(self.page.locator('#gemini-key').get_attribute('type'),'password')
        self.page.get_by_role('button',name='Зберегти й підключити').click()
        expect(self.page.locator('#done')).to_be_visible()
        self.assertEqual(self.page.locator('#gemini-key').input_value(),'')
        self.assertNotIn(key,self.page.content())
        self.assertEqual(self.page.evaluate('JSON.stringify([Object.keys(localStorage),Object.keys(sessionStorage)])'),'[[],[]]')
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)

    def test_cancel_and_failed_post_allow_retry(self):
        self.release.clear();self.page.goto(self.url+'/connect')
        self.page.locator('[data-provider=codex]').click();self.page.locator('#cancel').click()
        expect(self.page.locator('#retry')).to_be_visible()
        self.page.route('**/api/ai-setup',lambda route:route.abort() if route.request.method=='POST' else route.continue_())
        self.page.locator('#retry').click()
        expect(self.page.locator('[data-provider=gemini]')).to_be_enabled()
        expect(self.page.locator('#notice')).not_to_be_empty()


if __name__=='__main__':unittest.main()
