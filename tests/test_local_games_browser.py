"""Real Chromium + loopback production routes; no model inference or playable evidence."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from agent_factory.application import AgentFactoryService
from agent_factory.local_games import LocalGames
from agent_factory.storage import SQLiteStorage
from agent_factory.web import create_app
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, 'Install Playwright and Chromium for browser checks')
class LocalGamesBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start(); cls.browser = cls.runtime.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.runtime.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); self.path = self.root/'state.db'
        self.env = patch.dict(os.environ, {'AGENT_FACTORY_API_TOKEN':'', 'AGENT_FACTORY_API_ACTOR':'Founder', 'AGENT_FACTORY_TEMPORAL_ENABLED':'false'})
        self.env.start(); self.start_server()
        self.context = self.browser.new_context(); self.page = self.context.new_page()
        self.errors = []; self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_default_timeout(7000)

    def start_server(self):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.root, self.path), host='127.0.0.1', port=0, log_level='error', access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True); self.thread.start()
        deadline = time.monotonic()+10
        while not self.server.started and time.monotonic()<deadline: time.sleep(.01)
        if not self.server.started: raise RuntimeError('Local server startup failed')
        self.url = f'http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}'

    def stop_server(self):
        self.server.should_exit = True; self.thread.join(5)
        if self.thread.is_alive(): raise RuntimeError('Local server did not stop')

    def tearDown(self):
        self.context.close(); self.stop_server(); self.env.stop(); self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def home(self):
        self.page.goto(self.url)
        self.page.wait_for_function("document.querySelector('#games-page').textContent.length > 0")

    def create(self):
        self.home(); self.page.locator('#create-game').click(); self.page.locator('#idea').wait_for(state='visible')

    def saved(self):
        self.page.wait_for_function("document.querySelector('#notice').textContent === 'Зміни збережено.'")

    def prepared(self):
        self.create(); self.page.locator('#idea').fill('A garden puzzle. Keep the original words.'); self.saved()
        self.page.locator('#next-step').click(); self.page.locator('#model').wait_for(state='visible')
        self.page.locator('#model').select_option('ollama/local:qwen2.5-coder:7b'); self.saved()
        self.page.locator('#next-step').click(); self.page.locator('#submit-draft').wait_for(state='visible')

    def submit(self):
        self.prepared(); self.page.locator('#submit-draft').click()
        self.page.locator('#confirm button[value=confirm]').click()
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('Виконання не почалося')")

    def test_empty_home_one_primary_no_probe_and_mobile(self):
        requests = []; self.page.on('request', lambda request: requests.append(request.url))
        self.home()
        self.page.wait_for_function("document.querySelector('#missions-page').textContent.length > 0")
        self.assertEqual(self.page.locator('#notice').inner_text(), '')
        self.assertEqual(self.page.locator('button.primary:visible').count(), 1)
        self.assertEqual(self.page.locator('button.primary:visible').inner_text(), 'Створити гру')
        self.assertEqual(self.page.locator('#technical:visible').count(), 0)
        self.assertFalse(any('/environment' in url for url in requests))
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        screenshots = os.environ.get('LOCAL_GAMES_SCREENSHOT_DIR')
        if screenshots:
            Path(screenshots).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(screenshots)/'home-mobile.png'), full_page=True)

    def test_interrupted_wizard_restores_text_step_error_and_previous_navigation(self):
        self.create(); self.page.locator('#next-step').click()
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('Напишіть')")
        hash_value = self.page.evaluate('location.hash')
        self.page.reload(); self.page.locator('#idea').wait_for(state='visible')
        self.assertIn('Напишіть', self.page.locator('#notice').inner_text())
        source = '  Незмінний задум ☃\nДругий рядок.  '
        self.page.locator('#idea').fill(source); self.saved()
        self.page.locator('#next-step').click(); self.page.locator('#model').wait_for(state='visible')
        self.context.close(); self.stop_server(); self.start_server()
        self.context = self.browser.new_context(); self.page = self.context.new_page()
        self.page.goto(self.url+'/'+hash_value); self.page.locator('#model').wait_for(state='visible')
        self.page.locator('#previous-step').click(); self.page.locator('#idea').wait_for(state='visible')
        self.assertEqual(self.page.locator('#idea').input_value(), source)

    def test_existing_project_real_status_no_automatic_execution_and_back_navigation(self):
        # Project IDs and mission IDs need not match.
        with closing(SQLiteStorage(self.path)) as storage:
            storage.create_project('Earlier legacy project', '')
        self.submit()
        self.page.locator('#steps button').nth(3).click()
        self.page.locator('#progress-state').wait_for(state='visible')
        self.assertIn('Чернетка', self.page.locator('#progress-state').inner_text())
        self.assertIn('затвердьте план', self.page.locator('#next-action').inner_text())
        self.page.locator('#steps button').nth(4).click()
        self.page.get_by_role('button', name='Грати — недоступно').wait_for(state='visible')
        self.assertTrue(self.page.get_by_role('button', name='Грати — недоступно').is_disabled())
        self.page.locator('#back-home').click(); self.page.reload()
        self.page.locator('#existing summary').click(); self.page.locator('#mission-list button').click()
        self.page.locator('#check-environment').wait_for(state='visible')
        self.assertEqual(self.page.locator('#game-title').inner_text(), 'Нова гра')
        with closing(SQLiteStorage(self.path)) as storage:
            self.assertEqual(storage.db.execute('SELECT COUNT(*) FROM workflow_runs').fetchone()[0], 0)
        screenshots = os.environ.get('LOCAL_GAMES_SCREENSHOT_DIR')
        if screenshots:
            Path(screenshots).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(screenshots)/'existing-desktop.png'), full_page=True)

    def test_conflict_retains_local_text_and_can_open_latest_without_overwrite(self):
        self.create(); self.page.locator('#idea').fill('Original'); self.saved()
        other = self.context.new_page(); other.goto(self.page.url); other.locator('#idea').wait_for(state='visible')
        other.locator('#idea').fill('Other tab saved'); other.wait_for_function("document.querySelector('#notice').textContent === 'Зміни збережено.'")
        self.page.locator('#idea').fill('Keep my local text')
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('Інша вкладка')")
        self.assertEqual(self.page.locator('#idea').input_value(), 'Keep my local text')
        self.page.get_by_text('Відновлення після конфлікту', exact=True).click()
        self.page.locator('#reload-draft').click()
        self.page.wait_for_function("document.querySelector('#idea').value === 'Other tab saved'")
        self.assertIn('Keep my local text', self.page.locator('#recovery-copy').inner_text())
        other.close()

    def test_response_loss_retries_same_save_without_duplicate_revision(self):
        self.create()
        def lose_response(route):
            route.fetch(); route.abort()
        self.page.route('**/api/games/starts/*/save', lose_response, times=1)
        self.page.locator('#idea').fill('Ambiguous save')
        self.page.wait_for_function("document.querySelector('#save-state').textContent.includes('не підтверджено')")
        self.page.locator('#save-draft').click(); self.saved()
        with closing(SQLiteStorage(self.path)) as storage:
            row = storage.db.execute('SELECT revision,idea FROM local_game_drafts').fetchone()
            self.assertEqual(tuple(row), (2, 'Ambiguous save'))

    def test_history_and_operator_work_beyond_200_search_and_dropdowns(self):
        with closing(SQLiteStorage(self.path)) as storage:
            games = LocalGames(storage, self.root); draft = games.create('Founder', str(uuid.uuid4()))
            for index in range(205):
                draft = games.save(draft['id'], 'Founder', str(uuid.uuid4()), draft['revision'], title='History', idea=f'Revision {index}', model_key='', view_step=1)
            service = AgentFactoryService(storage, workspace=self.root); project = service.create_project('Large').project_id
            for index in range(205):
                service.create_work_item(project_id=project, title=f'Task {index:03}', description='search-me' if index==204 else '', inputs={'priority':'high'})
        self.page.goto(self.url+'/#start/'+draft['id']); self.page.locator('#history summary').click()
        self.page.locator('#version-query').fill('Revision 0'); self.page.locator('#version-search button').click()
        self.page.wait_for_function("document.querySelector('#versions-page').textContent === '1–1 із 1'")
        self.page.locator('#version-list button').click(); self.assertIn('Revision 0', self.page.locator('#version-preview').inner_text())
        self.page.goto(self.url+'/operations#work')
        self.page.wait_for_function("document.querySelector('#work-page').textContent === '1–50 of 205'")
        for index in range(4):
            self.page.locator('#work-next').click()
            self.page.wait_for_function('(start) => document.querySelector("#work-page").textContent.startsWith(start)', arg=str(51+50*index)+'–')
        self.assertEqual(self.page.locator('#work-list .work-row').count(), 5)
        self.page.locator('#filter-query').fill('search-me'); self.page.locator('#work-filters button[type=submit]').click()
        self.page.wait_for_function("document.querySelector('#work-count').textContent === '1 work item'")
        self.assertIn('Task 204', self.page.locator('#work-list').inner_text())
        self.assertEqual(self.page.locator('#filter-priority').evaluate('(node) => node.tagName'), 'SELECT')
        self.assertIn('high', self.page.locator('#filter-priority').inner_text())

    def test_slow_provider_health_does_not_block_paging_or_duplicate_probes(self):
        entered = threading.Event(); release = threading.Event(); calls = []
        def slow_health(runtime):
            calls.append(threading.get_ident()); entered.set()
            if not release.wait(12): raise RuntimeError('Synthetic health barrier timed out')
            return [{'provider':'deterministic', 'healthy':True}]
        with closing(SQLiteStorage(self.path)) as storage:
            service = AgentFactoryService(storage, workspace=self.root)
            project = service.create_project('Responsive paging').project_id
            for index in range(205):
                service.create_work_item(project_id=project, title=f'Task {index:03}', description='')
        with patch('agent_factory.runtime.AgentRuntime.health', slow_health):
            try:
                self.page.goto(self.url+'/operations#work')
                self.assertTrue(entered.wait(2), 'The actual HTTP route must reach the slow probe')
                with self.page.expect_request('**/api/integrations'):
                    self.page.evaluate("""() => {
                        window.integrationRead = fetch('/api/integrations').then(response => {
                            window.integrationStatus = response.status;
                            return response.json();
                        });
                    }""")
                self.page.wait_for_function("document.querySelector('#work-page').textContent === '1–50 of 205'", timeout=2500)
                self.page.locator('#work-next').click()
                self.page.wait_for_function("document.querySelector('#work-page').textContent === '51–100 of 205'", timeout=2500)
                self.assertEqual(len(calls), 1, 'Dashboard/monitor/providers/integrations must share one in-flight probe')
            finally:
                release.set()
            self.page.wait_for_function("document.querySelector('#connection-dot').className === 'online'")
            self.assertEqual(self.page.locator('#work-page').inner_text(), '51–100 of 205')
            self.page.wait_for_function('window.integrationStatus === 200')
            self.assertTrue(self.page.evaluate('async () => Array.isArray(await window.integrationRead)'))
            self.assertEqual(len(calls), 1, 'The integrations response must reuse the shared health result')

    def test_readiness_expiry_and_failed_refresh_never_leave_positive_status(self):
        self.submit()
        report = {'status':'ready', 'mode':'live', 'checks':[{'ready':True}], 'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat()}
        self.page.route('**/api/autonomous-missions/*/environment/check', lambda route: route.fulfill(json=report))
        self.page.locator('#check-environment').click()
        self.page.wait_for_function("document.querySelector('#environment-state').textContent.includes('чинні')")
        self.page.wait_for_function("document.querySelector('#environment-state').textContent.includes('Термін')")
        report['expires_at'] = (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
        self.page.locator('#check-environment').click()
        self.page.wait_for_function("document.querySelector('#environment-state').textContent.includes('чинні')")
        self.page.unroute('**/api/autonomous-missions/*/environment/check')
        self.page.route('**/api/autonomous-missions/*/environment/check', lambda route: route.fulfill(status=503, json={'detail':'unavailable'}))
        self.page.locator('#check-environment').click()
        self.page.wait_for_function("document.querySelector('#environment-state').textContent.includes('недоступний')")

    def test_late_model_list_cannot_reopen_new_draft_and_cancel_its_autosave(self):
        pending=[]
        self.page.route('**/api/games/models',lambda route:pending.append(route))
        self.create()
        self.page.locator('#idea').fill('Preserve my idea while AI choices load.')
        self.assertEqual(len(pending),1)
        pending[0].fulfill(json={'items':[]})
        self.saved()
        self.page.unroute('**/api/games/models')
        self.page.reload()
        self.page.locator('#idea').wait_for(state='visible')
        self.assertEqual(self.page.locator('#idea').input_value(),'Preserve my idea while AI choices load.')
