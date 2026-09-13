from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import MagicMock,patch
from types import SimpleNamespace

from agent_factory.autonomous_planning_pipeline import AutonomousPlanningPipelineService,PlanningPipelineFailedError
from agent_factory.storage import SQLiteStorage,MIGRATIONS
from agent_factory.studio_recovery import recover,checkpoint
from agent_factory.studio_supervisor import Supervisor
from tests import test_autonomous_planning_pipeline as pipeline_tests
from tests.test_autonomous_planning_pipeline import GoldenPlanningInvoker
from tests import test_studio_progress as progress_tests


class PipelineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=pipeline_tests.AutonomousPlanningPipelineTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.storage=self.fixture.storage

    def test_recovery_reuses_two_validated_outputs_and_passes_errors_to_failed_stage(self):
        first=GoldenPlanningInvoker()
        original=first.invoke
        def fail(request):
            if request.assignment.role_id=='software_architect':first.always_invalid=True
            return original(request)
        first.invoke=fail
        service=AutonomousPlanningPipelineService(self.storage,first,self.fixture.capabilities)
        manifest,authority=self.fixture.planning_scope(1)
        with self.assertRaises(PlanningPipelineFailedError):
            service.execute(self.fixture.mission.id,manifest_id=manifest.id,planning_authorization_id=authority.id,actor='Founder',command_id='first')
        source=service.runs(self.fixture.mission.id)[0]
        self.assertEqual(len(source.artifacts),2)
        second=GoldenPlanningInvoker();service.invoker=second
        manifest,authority=self.fixture.planning_scope(2)
        options=dict(manifest_id=manifest.id,planning_authorization_id=authority.id,actor='Founder',command_id='repair',recovery_from_run_id=source.id)
        result=service.execute(self.fixture.mission.id,**options)
        self.assertEqual(result.status,'COMPLETED')
        self.assertEqual([r.assignment.role_id for r in second.requests],['software_architect','backlog_planner','backlog_reviewer'])
        self.assertTrue(second.requests[0].validation_feedback)
        self.assertEqual(service.get_run(source.id).status,'FAILED')
        self.assertEqual(result.artifacts[0].output_digest,source.artifacts[0].output_digest)
        attempts=service.attempts(result.id)
        self.assertFalse(attempts[0].provider_metadata['inference_performed'])
        self.assertEqual(attempts[0].provider_metadata['reused_from_artifact_id'],source.artifacts[0].id)
        self.assertEqual(service.execute(self.fixture.mission.id,**options).id,result.id)
        self.assertEqual(len(second.requests),3)
        # All normal proposal checks still run on freshly bound artifacts.
        from agent_factory.autonomous_proposal_verifier import AutonomousProposalVerificationService
        report=AutonomousProposalVerificationService(self.storage).verify_and_present(result.id,actor='Founder',command_id='verify',expected_mission_version=self.fixture.missions.get(self.fixture.mission.id).version)
        self.assertEqual(str(report.status),'READY')

    def test_recovery_rejects_different_model_without_calling_provider(self):
        service=AutonomousPlanningPipelineService(self.storage,GoldenPlanningInvoker(),self.fixture.capabilities)
        manifest,authority=self.fixture.planning_scope(1)
        source=service.execute(self.fixture.mission.id,manifest_id=manifest.id,planning_authorization_id=authority.id,actor='Founder',command_id='first')
        other,authority=self.fixture.planning_scope(2)
        from dataclasses import replace
        changed=replace(other,assignments=(replace(other.assignments[0],model='changed'),*other.assignments[1:]))
        original=service.planning.get_manifest
        with patch.object(service.planning,'get_manifest',side_effect=lambda ident:changed if ident==other.id else original(ident)):
            with self.assertRaisesRegex(ValueError,'changed'):
                service.execute(self.fixture.mission.id,manifest_id=other.id,planning_authorization_id=authority.id,actor='Founder',command_id='bad',recovery_from_run_id=source.id)


class StudioRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=progress_tests.ProgressTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db;self.runner=self.fixture.runner;self.game=self.fixture.game
        self.runner.status.return_value='finished'
        self.patch=patch('agent_factory.studio_recovery.checked_local_source',return_value=SimpleNamespace(name='qwen2.5-coder:7b'))
        self.patch.start();self.addCleanup(self.patch.stop)

    def request(self,command='retry',**extra):
        with closing(SQLiteStorage(self.db)) as storage:
            mission=__import__('agent_factory.autonomous_mission',fromlist=['AutonomousMissionService']).AutonomousMissionService(storage).get(self.game['mission_id'])
            token=checkpoint(storage,mission)
        options=dict(mission_id=mission.id,actor='Founder',command_id=command,expected_checkpoint=token);options.update(extra)
        return recover(self.db,self.fixture.root,self.runner,**options),options

    def test_stop_recover_replay_and_stale_tab_do_not_duplicate(self):
        with closing(SQLiteStorage(self.db)) as storage:Supervisor(storage).revoke(self.game['mission_key'],actor='Founder')
        self.runner.submit.reset_mock()
        result,options=self.request()
        self.assertFalse(result['replayed']);self.runner.submit.assert_called_once()
        result=recover(self.db,self.fixture.root,self.runner,**options)
        self.assertTrue(result['replayed']);self.runner.submit.assert_called_once()
        options['command_id']='other-tab'
        with self.assertRaisesRegex(ValueError,'recovery_changed'):recover(self.db,self.fixture.root,self.runner,**options)
        with closing(SQLiteStorage(self.db)) as storage:
            self.assertEqual(Supervisor(storage).mandate(self.game['mission_key']).ceiling,0)
            with self.assertRaises(sqlite3.IntegrityError):storage.db.execute('DELETE FROM studio_planning_recoveries')

    def test_active_foreign_and_model_changed_are_refused(self):
        self.runner.status.return_value='running'
        with self.assertRaisesRegex(ValueError,'recovery_busy'):self.request()
        self.runner.status.return_value='finished'
        with self.assertRaises(KeyError):self.request(actor='other')
        with patch('agent_factory.studio_recovery.checked_local_source',return_value=SimpleNamespace(name='qwen2.5-coder:14b')):
            with self.assertRaisesRegex(ValueError,'recovery_model_changed'):self.request()

    def test_another_process_holding_inference_lock_cannot_be_recovered_over(self):
        from agent_factory.local_games import local_games_lock
        with local_games_lock(str(self.db)+'.studio-inference'):
            with self.assertRaisesRegex(ValueError,'recovery_busy'):self.request()

    def test_http_recovery_requires_owner_confirmation_and_replay_is_read_only(self):
        import uuid
        from fastapi.testclient import TestClient
        from agent_factory.web import create_app
        from agent_factory.autonomous_mission import AutonomousMissionService
        with closing(SQLiteStorage(self.db)) as storage:
            token=checkpoint(storage,AutonomousMissionService(storage).get(self.game['mission_id']))
        body={'command_id':str(uuid.uuid4()),'checkpoint':token,'confirmed':True}
        url=f"/api/studio/games/{self.game['mission_id']}/recover"
        with patch.dict('os.environ',{'AGENT_FACTORY_API_TOKEN':'','AGENT_FACTORY_API_ACTOR':'Founder'}), \
             patch('agent_factory.studio_runner.StudioRunner',return_value=self.runner), \
             TestClient(create_app(self.fixture.root,self.db),base_url='http://127.0.0.1') as client:
            self.assertEqual(client.post(url,json=body).status_code,400)
            headers={'X-Agent-Factory-Confirm':'true'}
            with patch.dict('os.environ',{'AGENT_FACTORY_API_ACTOR':'other'}):
                self.assertEqual(client.post(url,json=body,headers=headers).status_code,404)
            self.runner.submit.reset_mock()
            self.assertEqual(client.post(url,json=body,headers=headers).status_code,202)
            self.assertTrue(client.post(url,json=body,headers=headers).json()['replayed'])
            self.runner.submit.assert_called_once()

    def test_unknown_step_requires_explicit_recovery_and_is_not_erased(self):
        from agent_factory.studio_supervisor import MissionState,Advanced
        with closing(SQLiteStorage(self.db)) as storage:
            supervisor=Supervisor(storage);mandate=supervisor.mandate(self.game['mission_key'])
            ident=supervisor._open(self.game['mission_key'],'plan',mandate=mandate,actor='Founder',command_id='old',at='2026-09-13T00:00:00+00:00')
            supervisor.reconcile(self.game['mission_key'])
        self.request()
        with closing(SQLiteStorage(self.db)) as storage:
            driver=MagicMock();driver.state.return_value=MissionState(self.game['mission_id'],'Founder','DRAFT','RUNNING',1,self.game['mission_key'])
            driver.plan.return_value=Advanced('WAITING_FOR_BACKLOG_APPROVAL')
            with patch('agent_factory.studio_first_run.FirstRun.readiness',return_value=SimpleNamespace(can_start=True)):
                result=Supervisor(storage).advance(self.game['mission_key'],driver)
            self.assertEqual(result.outcome,'advanced',result.summary.en);driver.plan.assert_called_once()
            self.assertIn(':recovery:',driver.plan.call_args.kwargs['command_id'])
            self.assertEqual(storage.db.execute('SELECT outcome FROM studio_supervisor_steps WHERE id=?',(ident,)).fetchone()[0],'unknown')

    def test_upgrade_from_previous_schema_preserves_games(self):
        old=self.fixture.root/'old.db'
        with patch('agent_factory.storage.MIGRATIONS',MIGRATIONS[:-1]):
            with closing(SQLiteStorage(old)) as storage:self.assertEqual(storage.db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0],89)
            from agent_factory.studio_start import create_local_game
            with patch('agent_factory.studio_start.checked_local_source',return_value=SimpleNamespace(name='qwen2.5-coder:7b')):
                game=create_local_game(old,self.fixture.root,actor='Founder',command_id='old',title='Preserved game',idea='Original idea',runner=MagicMock())
        with closing(SQLiteStorage(old)) as storage:
            self.assertEqual(storage.db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0],90)
            self.assertEqual(storage.db.execute('SELECT COUNT(*) FROM studio_planning_recoveries').fetchone()[0],0)
            self.assertEqual(storage.db.execute('SELECT name FROM autonomous_missions WHERE id=?',(game['mission_id'],)).fetchone()[0],'Preserved game')


if __name__=='__main__':unittest.main()
