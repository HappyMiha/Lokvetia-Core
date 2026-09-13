from contextlib import closing
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch,MagicMock
from fastapi.testclient import TestClient
from agent_factory.storage import SQLiteStorage
from agent_factory.studio_start import create_local_game
from agent_factory.studio_progress import games,game_progress
from agent_factory.studio_supervisor import Supervisor
from agent_factory.localisation import Message


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=self.root/'state.db'
        self.runner=MagicMock();self.runner.status.return_value='queued'
        with patch('agent_factory.studio_start.checked_local_source',return_value=SimpleNamespace(name='qwen2.5-coder:7b')):
            self.game=create_local_game(self.db,self.root,actor='Founder',command_id='one',title='Platformer',idea='A little platformer',runner=self.runner)

    def test_library_is_durable_owner_scoped_and_reports_the_actual_model(self):
        with closing(SQLiteStorage(self.db)) as storage:
            result=games(storage,self.runner,'Founder')
            self.assertEqual(result['total'],1)
            row=result['items'][0]
            self.assertEqual(row['model'],'local:qwen2.5-coder:7b')
            self.assertTrue(row['local_only']);self.assertEqual(row['paid_limit'],0)
            self.assertEqual(row['state'],'queued');self.assertEqual(row['completed_steps'],0)
            self.assertIsNone(row['tokens'])
            self.assertEqual(games(storage,self.runner,'other')['items'],[])
            with self.assertRaises(KeyError):game_progress(storage,self.runner,self.game['mission_id'],'other')
        restarted=MagicMock();restarted.status.return_value='idle'
        with closing(SQLiteStorage(self.db)) as storage:
            result=game_progress(storage,restarted,self.game['mission_id'],'Founder')
            self.assertEqual(result['title'],'Platformer');self.assertEqual(result['idea'],'A little platformer')
            self.assertEqual(result['state'],'interrupted');self.assertFalse(result['background'])

    def test_finished_worker_with_refused_plan_is_failure_not_success(self):
        with closing(SQLiteStorage(self.db)) as storage:
            Supervisor(storage)._write(self.game['mission_key'],'plan','waiting',Message('Некоректні вимоги','Invalid requirements'))
            self.runner.status.return_value='finished'
            result=game_progress(storage,self.runner,self.game['mission_id'],'Founder')
            self.assertEqual(result['state'],'failed');self.assertFalse(result['background'])
            self.assertEqual(result['last_detail'],'Некоректні вимоги')

    def test_stop_requires_owner_confirmation_is_idempotent_and_revokes_authority(self):
        from agent_factory.web import create_app
        with patch.dict('os.environ',{'AGENT_FACTORY_API_TOKEN':'','AGENT_FACTORY_API_ACTOR':'Founder'}):
            app=create_app(self.root,self.db)
            # Routes capture this runner, so inspect its cancellation directly.
            with TestClient(app,base_url='http://127.0.0.1') as client:
                url=f"/api/studio/games/{self.game['mission_id']}/stop"
                self.assertEqual(client.post(url).status_code,400)
                with patch.dict('os.environ',{'AGENT_FACTORY_API_ACTOR':'other'}):
                    self.assertEqual(client.get(f"/api/studio/progress/{self.game['mission_key']}").status_code,404)
                    self.assertEqual(client.post(url,headers={'X-Agent-Factory-Confirm':'true'}).status_code,404)
                for _ in range(2):
                    self.assertEqual(client.post(url,headers={'X-Agent-Factory-Confirm':'true'}).status_code,200)
                with closing(SQLiteStorage(self.db)) as storage:
                    self.assertIsNone(Supervisor(storage).mandate(self.game['mission_key']))
                self.assertEqual(client.get('/api/studio/games?limit=0').status_code,422)

    def test_driver_cancellation_observes_durable_revocation(self):
        import threading
        from agent_factory.studio_supervisor import CoreMissionDriver
        with closing(SQLiteStorage(self.db)) as storage:
            driver=CoreMissionDriver(storage,self.game['mission_id'],workspace=self.root)
            driver.cancel_event=threading.Event()
            with patch('agent_factory.runtime.AgentRuntime') as runtime:
                runtime.return_value.providers={}
                invoker,_=driver._runtime()
            self.assertFalse(invoker.cancel_event.is_set())
            Supervisor(storage).revoke(self.game['mission_key'],actor='Founder')
            self.assertTrue(invoker.cancel_event.is_set())
            with self.assertRaises(PermissionError):invoker.invoke(None)
            runtime.return_value.run.assert_not_called()

    def test_invoker_passes_stop_signal_to_the_running_cli(self):
        import threading
        from tests.test_autonomous_planning_pipeline import AutonomousPlanningPipelineTests,GoldenPlanningInvoker
        from agent_factory.autonomous_planning_pipeline import AutonomousPlanningPipelineService,RuntimePlanningInvoker
        fixture=AutonomousPlanningPipelineTests();fixture.setUp()
        try:
            manifest,authorization=fixture.planning_scope(1)
            golden=GoldenPlanningInvoker()
            service=AutonomousPlanningPipelineService(fixture.storage,golden,fixture.capabilities)
            service.execute(fixture.mission.id,manifest_id=manifest.id,planning_authorization_id=authorization.id,actor='Founder',command_id='capture-request')
            runtime=MagicMock();event=threading.Event()
            invoker=RuntimePlanningInvoker(runtime,cancel_event=event)
            invoker.invoke(golden.requests[0])
            self.assertIs(runtime.run.call_args.kwargs['cancel_event'],event)
            event.set()
            with self.assertRaises(PermissionError):invoker.invoke(golden.requests[1])
            runtime.run.assert_called_once()
            self.runner.status.return_value='finished'
            result=game_progress(fixture.storage,self.runner,fixture.mission.id,'Founder')
            self.assertEqual(result['completed_steps'],5)
            self.assertEqual(len(result['artifacts']),5)
            self.assertEqual(result['provider'],'local')
            self.assertTrue(all(step['provider']=='local' for step in result['steps']))
            self.assertNotEqual(result['state'],'ready','A completed pipeline still needs proposal verification')
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
