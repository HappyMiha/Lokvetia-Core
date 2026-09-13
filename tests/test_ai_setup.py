from contextlib import ExitStack
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
import uuid

from agent_factory import ai_setup as module


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.keys={}
        self.store=MagicMock()
        self.store.put.side_effect=lambda ref,value:self.keys.__setitem__(ref,value)
        self.store.get.side_effect=lambda ref:self.keys[ref]
        self.store.delete.side_effect=lambda ref:self.keys.pop(ref,None)
        self.service=module.AISetup(self.root,self.root/'state.db',store=self.store);self.addCleanup(self.service.close)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(module,'this_machine',return_value=SimpleNamespace(is_the_users_computer=True)))
        self.stack.enter_context(patch.object(module,'launcher',return_value=['official-cli']))
        self.submit=self.stack.enter_context(patch.object(self.service.executor,'submit'))
        self.record=self.stack.enter_context(patch.object(self.service,'record',wraps=self.service.record))
        self.process=self.stack.enter_context(patch.object(self.service,'process',side_effect=self.fake_process))
        self.requests=[]

    def fake_process(self,command,**kwargs):
        self.requests.append(command)
        kwargs['check']()
        if '--output-last-message' in command:
            Path(command[command.index('--output-last-message')+1]).write_text('LOKVETIA_OK')
        return 0,'{"response":"LOKVETIA_OK"}'

    def start(self,provider='codex'):
        return self.service.start(provider,'owner',str(uuid.uuid4()))

    def execute(self):
        fn,*args=self.submit.call_args.args
        fn(*args)
        return self.service.get(args[0],'owner')

    def test_real_answer_required_before_ready_and_record(self):
        self.start();result=self.execute()
        self.assertEqual(result['status'],'ready')
        self.record.assert_called_once()
        command=next(row for row in self.requests if 'exec' in row)
        self.assertIn('read-only',command)
        self.assertIn('--ignore-user-config',command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',command)

    def test_failed_canary_does_not_create_connection(self):
        self.process.side_effect=lambda *args,**kwargs:(0,'wrong response')
        self.start();result=self.execute()
        self.assertEqual(result['status'],'failed')
        self.record.assert_not_called()

    def test_duplicate_command_cannot_repeat_or_switch_owner(self):
        job=self.start()
        replay=self.service.start('codex','owner',job['id'])
        self.assertEqual(job['id'],replay['id']);self.assertEqual(self.submit.call_count,1)
        with self.assertRaises(ValueError):self.service.start('gemini','owner',job['id'])
        with self.assertRaises(ValueError):self.service.start('codex','other',job['id'])
        with self.assertRaises(KeyError):self.service.get(job['id'],'other')
        with self.assertRaises(KeyError):self.service.cancel(job['id'],'other')

    def test_one_active_job_and_cancellation(self):
        job=self.start()
        with self.assertRaisesRegex(ValueError,'busy'):self.start('gemini')
        self.service.cancel(job['id'],'owner')
        self.assertEqual(self.execute()['status'],'cancelled')
        self.process.assert_not_called();self.record.assert_not_called()

    def test_revocation_and_remote_machine_deny_execution(self):
        with self.assertRaises(PermissionError):self.service.start('codex','owner',str(uuid.uuid4()),authorize=lambda:False)
        with patch.object(module,'this_machine',return_value=SimpleNamespace(is_the_users_computer=False)):
            with self.assertRaises(PermissionError):self.start()
            self.assertFalse(self.service.scan('owner')['local'])
        allowed=[True]
        job=self.service.start('codex','owner',str(uuid.uuid4()),authorize=lambda:allowed[0])
        allowed[0]=False
        self.assertEqual(self.execute()['status'],'cancelled')
        self.process.assert_not_called()

    def test_gemini_uses_official_acp_login_without_creating_agent_sessions(self):
        attempts=[]
        def process(command,**kwargs):
            attempts.append(command)
            self.assertIn('--admin-policy',command)
            self.assertIn('--allowed-mcp-server-names=__lokvetia_no_server__',command)
            if '--acp' not in command:
                return (1,'Login required') if len(attempts)==1 else (0,'{"response":"LOKVETIA_OK"}')
            from io import StringIO
            stdin=StringIO()
            proc=SimpleNamespace(stdin=stdin)
            kwargs['on_output']('',proc)
            output='{"id":1,"result":{}}\nhttps://accounts.google.com/o/oauth2/v2/auth?state=one\n'
            kwargs['on_output'](output,proc)
            kwargs['on_output'](output,proc)
            self.assertEqual([json.loads(line)['method'] for line in stdin.getvalue().splitlines()],['initialize','authenticate'])
            self.assertEqual(self.service.get(job['id'],'owner')['status'],'login')
            with patch.object(self.service.supervisor,'terminate_tree'):
                kwargs['on_output'](output+'{"id":2,"result":{}}\n',proc)
            return 0,''
        self.process.side_effect=process
        job=self.start('gemini');result=self.execute()
        self.assertEqual(result['status'],'ready');self.assertIsNone(result['login_url'])
        with self.service.db() as db:
            self.assertNotIn('state=one',str(list(db.execute('SELECT * FROM setup_jobs'))))

    def test_google_account_rejection_requests_key_without_retrying_login(self):
        self.process.side_effect=lambda *a,**k:(1,'IneligibleTierError UNSUPPORTED_CLIENT')
        self.start('gemini');result=self.execute()
        self.assertEqual(result['step'],'key');self.assertEqual(self.process.call_count,1)
        self.record.assert_not_called()

    def test_key_is_owner_bound_not_in_database_and_disconnect_revokes_first(self):
        secret='synthetic-gemini-key-1234'
        job=self.service.start('gemini','owner',str(uuid.uuid4()),secret=secret)
        self.assertEqual(self.execute()['status'],'ready')
        with patch('agent_factory.os_credentials.WindowsCredentialStore',return_value=self.store):
            self.assertEqual(module.connected_environment(self.root,'owner')['GEMINI_API_KEY'],secret)
            self.assertEqual(module.connected_environment(self.root,'other'),{})
        with self.service.db() as db:
            self.assertNotIn(secret,' '.join(db.iterdump()))
        self.service.start('gemini','owner',job['id'],secret='synthetic-replacement-key')
        self.assertEqual(self.store.put.call_count,1)
        self.store.delete.side_effect=OSError('store unavailable')
        self.service.disconnect('gemini','owner')
        self.assertEqual(module.connected_environment(self.root,'owner'),{})
        self.assertIsNone(self.service.scan('owner')['providers'][1]['connection'])

    def test_secret_rejected_for_other_providers_and_cancel_blocks_final_publication(self):
        with self.assertRaises(ValueError):
            self.service.start('codex','owner',str(uuid.uuid4()),secret='synthetic-secret')
        job=self.start()
        self.service.cancel(job['id'],'owner')
        with self.assertRaisesRegex(module.SetupError,'cancelled'):
            self.service.record('codex','owner','model',['cli'],ident=job['id'],authorize=lambda:True)
        self.assertIsNone(self.service.scan('owner')['providers'][0]['connection'])

    def test_existing_local_model_needs_no_key_or_download(self):
        from tests.test_environment_model_probe import synthetic_result,MODEL_DIGEST
        with patch.object(module,'local_api',side_effect=[{'models':[{'name':'qwen2.5-coder:7b'}]},
                {'model':'qwen2.5-coder:7b','done':True,'response':'LOKVETIA_OK'}]), \
                patch('agent_factory.local_role_qualification.qualify',return_value=synthetic_result()) as qualify, \
                patch('agent_factory.environment_model_probe.model_inventory',return_value=MODEL_DIGEST), \
                patch('agent_factory.hardware_inventory.collect_inventory',return_value={'gpus':[]}):
            self.start('ollama');result=self.execute()
            with patch('agent_factory.studio_start.model_inventory',return_value=MODEL_DIGEST), \
                 patch('agent_factory.studio_start.require_a_build_machine',return_value=SimpleNamespace(is_the_users_computer=True)):
                from agent_factory.studio_start import checked_local_source
                from agent_factory.storage import SQLiteStorage
                with __import__('contextlib').closing(SQLiteStorage(self.root/'state.db')) as storage:
                    self.assertEqual(checked_local_source(storage,self.root).name,'qwen2.5-coder:7b')
            qualify.assert_called_once()
        self.assertEqual(result['status'],'ready');self.assertEqual(result['model'],'qwen2.5-coder:7b')
        self.process.assert_not_called()

    def test_local_connection_never_claims_ready_when_role_qualification_fails(self):
        with patch.object(module,'local_api',side_effect=[{'models':[{'name':'qwen2.5-coder:7b'}]},
                {'model':'qwen2.5-coder:7b','done':True,'response':'LOKVETIA_OK'}]), \
                patch('agent_factory.local_role_qualification.qualify',side_effect=ValueError('private failure')):
            self.start('ollama');result=self.execute()
        self.assertEqual(result['step'],'qualification_failed');self.record.assert_not_called()
        self.assertNotIn('private failure',json.dumps(result))

    def test_quota_and_timeout_are_actionable_without_leaking_output(self):
        self.process.side_effect=module.SetupError('timeout')
        self.start();result=self.execute()
        self.assertEqual(result['step'],'timeout')
        with self.assertRaisesRegex(module.SetupError,'quota'):
            module.AISetup.checked(1,'','429 quota secret-not-for-ui')

    def test_missing_tool_attempts_only_pinned_official_install(self):
        job=self.start('gemini')
        node=self.root/'node.exe';node.touch()
        npm=self.root/'node_modules/npm/bin/npm-cli.js';npm.parent.mkdir(parents=True);npm.touch()
        invoke=MagicMock(return_value=(0,''))
        with patch.object(module,'program',side_effect=lambda name,*a:str(node) if name=='node' else None):
            self.service.install('gemini',job['id'],invoke)
        command=invoke.call_args.args[0]
        self.assertEqual(command[:3],[str(node),str(npm),'install'])
        self.assertIn('@google/gemini-cli@0.53.1',command)
        self.assertIn('--ignore-scripts',command)
        self.assertIn('--registry=https://registry.npmjs.org',command)

    def test_expired_job_is_audited_and_does_not_block_retry(self):
        job=self.start()
        with self.service.db() as db:db.execute('UPDATE setup_jobs SET deadline=0 WHERE id=?',(job['id'],))
        self.assertEqual(self.service.get(job['id'],'owner')['step'],'timeout')
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM setup_audit WHERE job_id=? AND event='timeout'",(job['id'],)).fetchone()[0],1)
        self.start('gemini')


class TransportTests(unittest.TestCase):
    def test_only_official_login_urls(self):
        for raw in ['https://evil.test/login','https://accounts.google.com.evil.test/login',
                    'https://user@accounts.google.com/login','https://accounts.google.com/login?access_token=SECRET']:
            self.assertIsNone(module.official_login_url('gemini',raw))
        self.assertEqual(module.official_login_url('codex','Open https://auth.openai.com/oauth/authorize?state=x'),
                         'https://auth.openai.com/oauth/authorize?state=x')

    def test_environment_does_not_leak_other_provider_secrets(self):
        with patch.dict(module.os.environ,{'OPENAI_API_KEY':'secret','GEMINI_API_KEY':'secret',
                'SLACK_TOKEN':'secret','DATABASE_PASSWORD':'secret','OLLAMA_HOST':'https://remote.test'}):
            env=module.environment()
        self.assertFalse(any(key in env for key in ['OPENAI_API_KEY','GEMINI_API_KEY','SLACK_TOKEN','DATABASE_PASSWORD']))
        self.assertEqual(env['OLLAMA_HOST'],'http://127.0.0.1:11434')

    def test_timeout_and_output_overflow_terminate_process(self):
        with TemporaryDirectory() as folder:
            setup=module.AISetup(Path(folder),Path(folder)/'state.db')
            try:
                for script,seconds,reason in [('import time;time.sleep(30)',.2,'timeout'),
                                             ('print("x"*70000)',5,'failed')]:
                    with self.assertRaisesRegex(module.SetupError,reason):
                        setup.process([sys.executable,'-c',script],cwd=folder,env=module.environment(),seconds=seconds,check=lambda:None)
            finally:setup.close()

    def test_noninteractive_stdin_is_closed_and_installer_progress_is_bounded(self):
        with TemporaryDirectory() as folder:
            setup=module.AISetup(Path(folder),Path(folder)/'state.db')
            try:
                code,output=setup.process([sys.executable,'-c','import sys;sys.stdin.read();print("x"*90000);print("DONE")'],cwd=folder,env=module.environment(),seconds=5,check=lambda:None,progress=True)
                self.assertEqual(code,0);self.assertIn('DONE',output);self.assertLessEqual(len(output),65536)
            finally:setup.close()


if __name__=='__main__':unittest.main()
