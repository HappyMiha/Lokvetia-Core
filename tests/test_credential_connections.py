import io
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent_factory.credential_connections import CredentialConnections
from agent_factory.credentials import CredentialBroker, REDACTED
from agent_factory.storage import SQLiteStorage

class MemoryStore:
    def __init__(self): self.values = {}; self.fail_delete = False
    def put(self, key, value): self.values[key] = value
    def get(self, key): return self.values[key]
    def delete(self, key):
        if self.fail_delete: raise RuntimeError('synthetic backend failure')
        self.values.pop(key,None)

class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = MemoryStore()
        self.service = CredentialConnections(self.root/'connections.db',store=self.store)
        self.storage = SQLiteStorage(self.root/'core.db'); self.addCleanup(self.storage.close)
        self.broker = CredentialBroker(self.storage)
        self.secret = 'synthetic-connection-secret-12345'
        self.record = self.service.connect(actor='Owner',tenant='local',provider='openai',secret=self.secret)
        self.ref = self.record['id']

    def execute(self, **overrides):
        kwargs = dict(actor='Owner',tenant='local',broker=self.broker,mission_id='m',tool_key='provider.request',operation='complete',preapproved_operations={'complete'},prompt='Write a plan',arguments={'part':1},executor=lambda env,args: {'ok':True})
        kwargs.update(overrides)
        return self.service.execute(self.ref,**kwargs)

    def test_no_scope_expansion_or_cross_actor_tenant_use(self):
        for changes in ({'actor':'Other'},{'tenant':'foreign'},{'preapproved_operations':set()}):
            with self.assertRaises(PermissionError): self.execute(**changes)
        self.assertEqual(self.service.list(actor='Other',tenant='local'),[])
        with self.assertRaises(PermissionError): self.service.disconnect(self.ref,actor='Other',tenant='local')
        self.assertEqual(self.broker._vault,{})

    def test_admission_scrubs_keys_nested_results_exceptions_and_persistent_sinks(self):
        logs=io.StringIO(); handler=logging.StreamHandler(logs); logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler,handler)
        seen={}
        def invoke(env,args):
            seen.update(env)
            return {self.secret: {'echo':[self.secret]}}
        self.assertEqual(self.execute(executor=invoke), {REDACTED:{'echo':[REDACTED]}})
        self.assertEqual(seen,{'OPENAI_API_KEY':self.secret})
        self.assertNotIn('OPENAI_API_KEY',__import__('os').environ)
        def fail(env,args): raise RuntimeError(self.secret)
        with self.assertRaisesRegex(RuntimeError,'Credential-backed operation failed') as error:
            self.execute(executor=fail)
        self.assertNotIn(self.secret,str(error.exception))
        for prompt,args in ((self.secret,{}),('safe',{'nested':[self.secret]})):
            with self.assertRaises(RuntimeError): self.execute(prompt=prompt,arguments=args)
        self.assertEqual(self.broker._vault,{})
        exported='\n'.join(self.storage.db.iterdump())
        self.assertNotIn(self.secret,exported+json.dumps(self.service.list(actor='Owner',tenant='local'))+logs.getvalue())
        for path in self.root.rglob('*'):
            if path.is_file(): self.assertNotIn(self.secret.encode(),path.read_bytes())

    def test_restart_and_failed_os_deletion_still_fence_new_use(self):
        self.service=CredentialConnections(self.root/'connections.db',store=self.store)
        self.assertEqual(self.execute(),{'ok':True})
        self.store.fail_delete=True
        result=self.service.disconnect(self.ref,actor='Owner',tenant='local')
        self.assertTrue(result['os_removal_pending'])
        self.service=CredentialConnections(self.root/'connections.db',store=self.store)
        with self.assertRaises(PermissionError): self.execute()
        self.store.fail_delete=False
        self.assertFalse(self.service.disconnect(self.ref,actor='Owner',tenant='local')['os_removal_pending'])
        self.assertNotIn(self.ref,self.store.values)

    def test_disconnect_serializes_with_inflight_use_across_service_instances(self):
        # Keep schema migration time outside the lock-synchronization deadline.
        prepared = SQLiteStorage(self.root/'thread-core.db')
        prepared.close()
        entered=threading.Event(); release=threading.Event(); disconnected=threading.Event(); errors=[]
        def execute():
            try:
                storage=SQLiteStorage(self.root/'thread-core.db')
                try:
                    def invoke(env,args): entered.set(); release.wait(3); return {'ok':True}
                    CredentialConnections(self.root/'connections.db',store=self.store).execute(self.ref,actor='Owner',tenant='local',broker=CredentialBroker(storage),mission_id='m',tool_key='t',operation='read',preapproved_operations={'read'},prompt='p',arguments={},executor=invoke)
                finally: storage.close()
            except Exception as exc: errors.append(type(exc).__name__)
        def revoke():
            try: CredentialConnections(self.root/'connections.db',store=self.store).disconnect(self.ref,actor='Owner',tenant='local');disconnected.set()
            except Exception as exc: errors.append(type(exc).__name__)
        worker=threading.Thread(target=execute);worker.start()
        try:
            self.assertTrue(entered.wait(3)); closer=threading.Thread(target=revoke);closer.start()
            self.assertFalse(disconnected.wait(.1));release.set();worker.join(5);closer.join(5)
            self.assertTrue(disconnected.is_set());self.assertEqual(errors,[])
            with self.assertRaises(PermissionError): self.execute()
        finally:release.set();worker.join(5)

    def test_failed_metadata_commit_removes_new_os_entry(self):
        with patch.object(self.service,'_view',side_effect=RuntimeError(self.secret)):
            with self.assertRaisesRegex(RuntimeError,'could not be saved'):
                self.service.connect(actor='Owner',tenant='local',provider='openai',secret=self.secret)
        self.assertEqual(set(self.store.values),{self.ref})

    def test_process_death_during_save_leaves_pending_reference_not_admitted(self):
        with patch.object(self.store,'put',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.connect(actor='Owner',tenant='local',provider='openai',secret=self.secret)
        pending=[x for x in self.service.list(actor='Owner',tenant='local') if x['status']=='pending']
        self.assertEqual(len(pending),1)
        self.ref=pending[0]['id']
        with self.assertRaises(PermissionError):self.execute()
        self.service.disconnect(self.ref,actor='Owner',tenant='local')
        self.assertEqual([x['status'] for x in self.service.list(actor='Owner',tenant='local') if x['id']==self.ref],['revoked'])
