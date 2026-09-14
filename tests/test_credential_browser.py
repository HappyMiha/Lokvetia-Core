"""Actual Chromium and production HTTP composition; synthetic store seam."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from agent_factory.web import create_app
from tests.test_connector_eligibility import AT, approval_fixture
from tests.test_credential_connections import MemoryStore
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright=None

@unittest.skipIf(sync_playwright is None,'Install Playwright and Chromium')
class CredentialBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=sync_playwright().start();cls.browser=cls.runtime.chromium.launch()
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.runtime.stop()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=MemoryStore()
        self.env=patch.dict(os.environ,{'AGENT_FACTORY_API_TOKEN':'','AGENT_FACTORY_API_ACTOR':'Founder','AGENT_FACTORY_API_ROLE':'operations_owner','AGENT_FACTORY_API_SCOPES':'read,write,control','AGENT_FACTORY_API_TENANTS':'local','AGENT_FACTORY_TEMPORAL_ENABLED':'false'})
        self.env.start()
        self.clock=patch('agent_factory.connector_eligibility.utc_now',return_value=AT);self.clock.start()
        import uvicorn
        self.app=create_app(self.root,self.root/'core.db',credential_store=self.store)
        self.app.state.connector_setup_approval=lambda **scope: approval_fixture(**scope)
        self.server=uvicorn.Server(uvicorn.Config(self.app,host='127.0.0.1',port=0,log_level='error',access_log=False))
        self.thread=threading.Thread(target=self.server.run,daemon=True);self.thread.start()
        deadline=time.monotonic()+10
        while not self.server.started and time.monotonic()<deadline:time.sleep(.01)
        if not self.server.started:raise RuntimeError('Server failed to start')
        self.url=f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.context=self.browser.new_context();self.page=self.context.new_page();self.page.set_default_timeout(7000);self.page.clock.install(time=AT)
        self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.secret='synthetic-browser-canary-123456'
    def tearDown(self):
        self.context.close();self.server.should_exit=True;self.thread.join(5);self.env.stop();self.clock.stop();self.temp.cleanup()
        self.assertFalse(self.thread.is_alive());self.assertEqual(self.errors,[])
    def enter(self):
        self.page.goto(self.url+'/settings/credentials')
        self.page.locator('#secret').fill(self.secret);self.page.locator('#confirmed').check()
    def test_masked_secret_cleared_no_browser_storage_and_disconnect_cancel_confirm(self):
        self.page.goto(self.url)
        # The navigation is no longer folded into a disclosure: every link a
        # person needs is on the page, and this one lives in the footer.
        self.page.get_by_role('link',name='Доступ до AI',exact=True).click()
        self.page.locator('#secret').fill(self.secret)
        self.assertEqual(self.page.locator('#secret').get_attribute('type'),'password')
        self.assertNotIn(self.secret,self.page.locator('body').inner_text())
        self.page.locator('#confirmed').check();self.page.locator('#save').click()
        self.page.wait_for_function("document.querySelector('#connections').textContent.includes('Збережено')")
        self.assertEqual(self.page.locator('#secret').input_value(),'')
        self.assertNotIn(self.secret,self.page.content())
        self.assertEqual(self.page.evaluate('JSON.stringify([Object.keys(localStorage),Object.keys(sessionStorage)])'),'[[],[]]')
        self.page.reload();self.page.get_by_role('button',name='Відключити',exact=True).click()
        self.page.get_by_role('button',name='Скасувати').click();self.assertEqual(len(self.store.values),1)
        self.page.get_by_role('button',name='Відключити',exact=True).click()
        self.page.locator('#disconnect-dialog').get_by_role('button',name='Відключити',exact=True).click()
        self.page.wait_for_function("document.querySelector('#connections').textContent.includes('Відключено')")
        self.assertFalse(self.store.values)
        self.page.wait_for_function("!document.querySelector('#save').disabled")
        self.assertTrue(self.page.locator('#save').is_enabled())
    def test_failure_and_lost_response_clear_secret_and_offer_recovery(self):
        self.enter()
        with patch.object(self.store,'put',side_effect=RuntimeError(self.secret)):
            self.page.locator('#save').click()
            self.page.wait_for_function("document.querySelector('#notice').textContent.includes('Не вдалося')")
        self.assertEqual(self.page.locator('#secret').input_value(),'')
        self.assertNotIn(self.secret,self.page.content())

        self.page.route('**/api/credential-connections',lambda route:route.abort() if route.request.method=='POST' else route.continue_())
        self.page.locator('#secret').fill(self.secret);self.page.locator('#confirmed').check();self.page.locator('#save').click()
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('Відповідь втрачено')")
        self.assertEqual(self.page.locator('#secret').input_value(),'')
        self.assertNotIn(self.secret,self.page.content())

    def test_product_guidance_selects_only_api_field_without_login_or_save(self):
        from agent_factory.provider_connection_catalog import connection_catalog
        with patch('agent_factory.credential_web.connection_catalog',return_value=connection_catalog(now=AT)):
            self.page.set_viewport_size({'width':390,'height':844})
            posts=[];self.page.on('request',lambda request:posts.append(request.url) if request.method=='POST' else None)
            self.page.goto(self.url+'/settings/credentials')
            self.page.locator('#connection-product').select_option('chatgpt')
            self.assertIn('не є ключем API',self.page.locator('#connection-guide').inner_text())
            self.assertEqual(self.page.locator('#connection-guide button').count(),0)
            self.page.locator('#connection-product').select_option('codex-cli')
            self.assertIn('codex login --device-auth',self.page.locator('#connection-guide').inner_text())
            self.assertEqual(self.page.locator('#connection-guide button').count(),0)
            self.page.locator('#secret').fill(self.secret)
            self.page.locator('#connection-product').select_option('anthropic-api')
            self.assertEqual(self.page.locator('#secret').input_value(),'')
            self.page.locator('#connection-guide button').click()
            self.assertEqual(self.page.locator('#provider').input_value(),'anthropic')
            self.assertFalse(self.page.locator('#confirmed').is_checked())
            self.assertFalse(self.store.values)
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
            self.assertNotIn(self.secret,self.page.content())
            self.assertEqual(posts,[])

    def test_refresh_denial_removes_old_catalogue_action_and_secret(self):
        from agent_factory.provider_connection_catalog import connection_catalog
        with patch('agent_factory.credential_web.connection_catalog',return_value=connection_catalog(now=AT)):
            self.enter();self.page.locator('#connection-product').select_option('openai-api')
            self.page.locator('#secret').fill(self.secret)
            self.page.route('**/api/credential-connections',lambda route:route.fulfill(status=403,body='{}'))
            self.page.locator('#refresh').click()
            self.page.wait_for_function("document.querySelector('#secret').disabled")
            self.assertEqual(self.page.locator('#connection-guide button').count(),0)
            self.assertEqual(self.page.locator('#secret').input_value(),'')
            self.assertTrue(self.page.locator('#connection-product').is_disabled())

    def test_catalogue_expiry_is_rechecked_at_selection(self):
        from agent_factory.provider_connection_catalog import connection_catalog
        with patch('agent_factory.credential_web.connection_catalog',return_value=connection_catalog(now=AT)):
            self.page.goto(self.url+'/settings/credentials')
            self.page.locator('#connection-product').select_option('openai-api')
            # No timer needs to fire: the click must itself reject obsolete guidance.
            self.page.evaluate("Date.now = () => Date.parse('2026-10-06T00:00:00Z')")
            self.page.locator('#connection-guide button').click()
            self.assertIn('потребують нового огляду',self.page.locator('#connection-guide-notice').inner_text())
            self.assertEqual(self.page.locator('#connection-guide button').count(),0)
            self.assertFalse(self.store.values)
