from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from agent_factory.ai_setup_web import install_routes
from agent_factory.http_auth import LocalHTTPBoundary,LocalAccess


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict('os.environ',{'AGENT_FACTORY_API_TOKEN':'owner-token','AGENT_FACTORY_API_ROLE':'operations_owner',
            'AGENT_FACTORY_API_ACTOR':'owner','AGENT_FACTORY_API_SCOPES':'read,write,control','AGENT_FACTORY_API_TENANTS':'local'})
        self.env.start();self.addCleanup(self.env.stop)
        app=FastAPI();app.state.local_access=LocalAccess();app.add_middleware(LocalHTTPBoundary,access=app.state.local_access)
        self.service=MagicMock();self.service.scan.return_value={'local':True,'providers':[]}
        self.service.start.return_value={'id':'job','status':'queued'}
        install_routes(app,Path(self.temp.name),Path(self.temp.name)/'state.db',service=self.service)
        self.client=TestClient(app,base_url='http://127.0.0.1');self.addCleanup(self.client.close)
        self.headers={'Authorization':'Bearer owner-token','X-Agent-Factory-Confirm':'true'}

    def test_get_never_starts_provider(self):
        self.assertEqual(self.client.get('/api/ai-setup',headers=self.headers).status_code,200)
        self.service.start.assert_not_called()

    def test_owner_click_authorizes_only_scoped_job(self):
        response=self.client.post('/api/ai-setup',headers=self.headers,json={'provider':'codex','command_id':'id'})
        self.assertEqual(response.status_code,200)
        callback=self.service.start.call_args.kwargs['authorize'];self.assertTrue(callback())
        with patch.dict('os.environ',{'AGENT_FACTORY_API_TOKEN':'rotated'}):self.assertFalse(callback())

    def test_auth_origin_role_and_confirmation(self):
        for headers in [{}, {'Authorization':'Bearer wrong'}, {**self.headers,'Origin':'https://evil.test'},
                        {'Authorization':'Bearer owner-token'}]:
            self.assertIn(self.client.post('/api/ai-setup',headers=headers,json={}).status_code,(400,401,403))
        with patch.dict('os.environ',{'AGENT_FACTORY_API_ROLE':'mission_owner'}):
            self.assertEqual(self.client.post('/api/ai-setup',headers=self.headers,json={}).status_code,403)
        self.service.start.assert_not_called()

    def test_no_secret_arbitrary_command_or_large_body_accepted(self):
        for body in [{'provider':'codex','secret':'private'}, {'provider':'codex','command':'del files'},
                     {'provider':'codex','command_id':'x','actor':'other'}]:
            self.assertEqual(self.client.post('/api/ai-setup',headers=self.headers,json=body).status_code,409)
        self.assertEqual(self.client.post('/api/ai-setup',headers=self.headers,content='x'*8192).status_code,400)
        self.service.start.assert_not_called()

    def test_optional_gemini_key_is_not_echoed_and_disconnect_is_owner_scoped(self):
        secret='synthetic-api-key-12345'
        response=self.client.post('/api/ai-setup',headers=self.headers,json={'provider':'gemini','command_id':'id','secret':secret})
        self.assertEqual(response.status_code,200);self.assertNotIn(secret,response.text)
        self.assertEqual(self.service.start.call_args.kwargs['secret'],secret)
        self.service.disconnect.return_value={'disconnected':True}
        self.assertEqual(self.client.delete('/api/ai-setup/connections/gemini',headers=self.headers).status_code,200)
        self.service.disconnect.assert_called_once_with('gemini','owner')


if __name__=='__main__':unittest.main()
