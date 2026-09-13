"""Create, leave, return and stop against production routes; no AI inference."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch
from agent_factory.web import create_app
from agent_factory import studio_launch_web  # Bind imports before patching the source-check seam.
try:
    from playwright.sync_api import sync_playwright,expect
except ImportError:sync_playwright=None

@unittest.skipIf(sync_playwright is None,'Install Playwright and Chromium')
class ProgressBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=sync_playwright().start();cls.browser=cls.runtime.chromium.launch()
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.runtime.stop()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.stack=ExitStack()
        self.stack.enter_context(patch.dict(os.environ,{'AGENT_FACTORY_API_TOKEN':'','AGENT_FACTORY_API_ACTOR':'Founder','AGENT_FACTORY_TEMPORAL_ENABLED':'false'}))
        for module in ['studio_start','studio_launch_web']:
            self.stack.enter_context(patch('agent_factory.'+module+'.checked_local_source',return_value=SimpleNamespace(name='qwen2.5-coder:7b')))
        self.runner=MagicMock();self.states={}
        self.runner.submit.side_effect=lambda ident:self.states.__setitem__(ident,'running')
        self.runner.status.side_effect=lambda ident:self.states.get(ident,'idle')
        self.runner.cancel.side_effect=lambda ident:self.states.__setitem__(ident,'cancelled')
        self.stack.enter_context(patch('agent_factory.studio_runner.StudioRunner',return_value=self.runner))
        import uvicorn
        self.server=uvicorn.Server(uvicorn.Config(create_app(self.root,self.root/'state.db'),host='127.0.0.1',port=0,log_level='error',access_log=False))
        self.thread=threading.Thread(target=self.server.run,daemon=True);self.thread.start()
        deadline=time.monotonic()+10
        while not self.server.started and time.monotonic()<deadline:time.sleep(.01)
        if not self.server.started:raise RuntimeError('server startup failed')
        self.url=f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.page=self.browser.new_page();self.page.set_default_timeout(10000)
        self.errors=[];self.page.on('pageerror',lambda error:self.errors.append(str(error)))
    def tearDown(self):
        self.page.close();self.server.should_exit=True;self.thread.join(10);self.stack.close();self.temp.cleanup()
        self.assertFalse(self.thread.is_alive());self.assertEqual(self.errors,[])
    def create(self):
        self.page.goto(self.url+'/studio')
        expect(self.page.locator('#create-readiness')).to_contain_text('qwen2.5-coder:7b')
        self.page.locator('#game-title').fill('Platformer')
        self.page.locator('#game-idea').fill('A platformer with a forest level.')
        self.page.locator('#create-game').click()
        expect(self.page.locator('#planning-title')).to_have_text('Platformer')
        expect(self.page.locator('#planning-status')).to_contain_text('триває')
    def test_create_leave_return_reload_and_stop_never_restarts_or_duplicates(self):
        self.create();url=self.page.url
        self.assertIn('mission=',url)
        self.page.goto(self.url+'/connect');self.page.goto(self.url+'/studio')
        expect(self.page.locator('#planning-title')).to_have_text('Platformer')
        expect(self.page.locator('#planning-model')).to_contain_text('qwen2.5-coder:7b')
        expect(self.page.locator('#planning-cost')).to_contain_text('$0')
        self.page.reload();expect(self.page.locator('#game-library button')).to_have_count(1)
        self.runner.submit.assert_called_once()
        self.page.locator('#planning-stop').click()
        expect(self.page.locator('#planning-status')).to_have_text('Зупинено')
        expect(self.page.locator('#planning-stop')).to_be_hidden()
        self.page.reload();expect(self.page.locator('#planning-status')).to_have_text('Зупинено')
        self.runner.submit.assert_called_once()
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
    def test_service_restart_keeps_game_and_does_not_claim_active_work(self):
        self.create();self.states.clear()
        self.page.reload()
        expect(self.page.locator('#planning-status')).to_contain_text('не підтверджене')
        expect(self.page.locator('#planning-artifacts')).to_contain_text('Ще немає')
        self.runner.submit.assert_called_once()

    def test_loss_of_owner_access_hides_previous_game_details(self):
        self.create()
        self.page.route('**/api/studio/games?*',lambda route:route.fulfill(status=401,json={'detail':'unauthorized'}))
        self.page.locator('#refresh').click()
        expect(self.page.locator('#planning-progress')).to_be_hidden()
        expect(self.page.locator('#game-library button')).to_have_count(0)
        self.runner.submit.assert_called_once()

    def test_return_restores_an_older_selected_game_not_just_the_latest(self):
        self.create();first=self.page.url
        self.page.locator('#game-title').fill('Second game')
        self.page.locator('#game-idea').fill('Another idea')
        self.page.locator('#create-game').click()
        expect(self.page.locator('#planning-title')).to_have_text('Second game')
        self.page.locator('#game-library button').filter(has_text='Platformer').click()
        expect(self.page.locator('#planning-title')).to_have_text('Platformer')
        self.page.goto(self.url+'/connect');self.page.goto(self.url+'/studio')
        expect(self.page.locator('#planning-title')).to_have_text('Platformer')
        self.assertEqual(self.page.url,first)
        self.assertEqual(self.runner.submit.call_count,2)

if __name__=='__main__':unittest.main()
