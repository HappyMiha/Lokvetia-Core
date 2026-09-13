from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from agent_factory.desktop_downloads import release_files
from agent_factory.email_auth import Accounts
from agent_factory.http_auth import Policy
from agent_factory.identity_service import create_identity_app


class PersonalOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            'AGENT_FACTORY_API_TOKEN': 'test-token-' * 8,
            'AGENT_FACTORY_API_TENANTS': '*', 'AGENT_FACTORY_API_ROLE': 'operations_owner',
            'LOKVETIA_PERSONAL_REGISTRATION': '1',
            'LOKVETIA_DESKTOP_RELEASE_DIR': str(self.root / 'release')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.root.joinpath('release').mkdir()
        payload = b'fixture desktop artifact, never executed'
        self.artifact = self.root / 'release' / 'Core.exe'
        self.artifact.write_bytes(payload)
        self.manifest = dict(schema_version=1, version='preview', artifacts=[dict(
            name='Core.exe', platform='windows-x64', size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest())])
        self.write_manifest()
        self.app = create_identity_app(self.root, {})
        self.browser = TestClient(self.app, base_url='http://localhost')
        self.browser.__enter__(); self.addCleanup(self.browser.__exit__, None, None, None)
        self.headers = {'X-Agent-Factory-Session': 'true'}
        self.body = dict(email='new@example.com', password='a long testing password', invitation='')

    def write_manifest(self):
        (self.root / 'release' / 'desktop-release.json').write_text(json.dumps(self.manifest))

    def signup(self):
        return self.browser.post('/auth/account/register', json=self.body, headers=self.headers)

    def login(self):
        return self.browser.post('/auth/account/login', json=dict(email=self.body['email'],
            password=self.body['password'], remember=True), headers=self.headers)

    def test_register_login_profile_download_and_logout(self):
        self.assertEqual(self.browser.get('/downloads/files/Core.exe').status_code, 401)
        self.assertEqual(self.signup().status_code, 200)
        self.assertEqual(self.login().status_code, 200)
        account = self.browser.get('/api/account').json()
        self.assertTrue(account['personal_account'])
        self.assertIsNone(account['organization'])
        self.assertFalse(account['can_invite'])
        self.assertEqual(account['members'], [])
        self.assertFalse(self.browser.get('/auth/session').json()['workspace_access'])
        self.assertEqual(self.browser.get('/downloads/files/Core.exe').content, self.artifact.read_bytes())
        self.browser.post('/auth/account/logout', json={}, headers=self.headers)
        self.assertEqual(self.browser.get('/downloads/files/Core.exe').status_code, 401)

    def test_signup_cannot_select_authority_or_invite_members(self):
        body = dict(self.body, role='operations_owner')
        self.assertEqual(self.browser.post('/auth/account/register', json=body, headers=self.headers).status_code, 400)
        self.signup(); self.login()
        for path in ['/api/projects', '/api/credentials', '/api/studio/create', '/progress/status']:
            self.assertEqual(self.browser.get(path).status_code, 403, path)
        self.assertEqual(self.browser.post('/auth/account/invitations', json={'email':'x@example.com'}, headers=self.headers).status_code, 403)

    def test_opt_in_and_invitation_reservation(self):
        Accounts(self.root / 'accounts.sqlite3').invite(self.body['email'], Policy.environment().principal)
        self.assertEqual(self.signup().status_code, 400)
        with patch.dict(os.environ, {'LOKVETIA_PERSONAL_REGISTRATION': '0'}):
            with TestClient(create_identity_app(self.root, {}), base_url='http://localhost') as browser:
                self.assertEqual(browser.get('/auth/account/config').json()['registration'], 'invitation')
                self.assertEqual(browser.post('/auth/account/register', json=self.body, headers=self.headers).status_code, 400)

    def test_duplicate_password_and_cross_origin(self):
        self.assertEqual(self.signup().status_code, 200)
        self.assertEqual(self.signup().status_code, 400)
        self.assertEqual(self.browser.post('/auth/account/register', json=self.body,
            headers=dict(self.headers, Origin='https://evil.example')).status_code, 403)
        self.assertEqual(self.browser.post('/auth/account/register', json=self.body).status_code, 400)
        self.body['password'] = 'wrong password'
        self.assertEqual(self.login().status_code, 401)

    def test_tampered_and_unlisted_downloads_fail_closed(self):
        self.signup(); self.login()
        self.assertEqual(self.browser.get('/downloads/files/private.txt').status_code, 404)
        self.artifact.write_bytes(b'x' * self.artifact.stat().st_size)
        self.assertEqual(self.browser.get('/downloads/files/Core.exe').status_code, 503)
        self.manifest['artifacts'][0]['name'] = '../accounts.sqlite3'
        self.write_manifest()
        self.assertEqual(self.browser.get('/downloads/manifest.json').status_code, 503)

    def test_account_scopes_are_private_and_session_survives_restart(self):
        self.signup(); self.login()
        accounts = Accounts(self.root / 'accounts.sqlite3')
        token, _ = accounts.login(self.body['email'], self.body['password'], Policy.environment())
        principal = accounts.authenticate(token, Policy.environment())
        self.assertEqual(principal.role, 'account_user')
        self.assertEqual(principal.scopes, {'read','write'})
        self.assertEqual(len(principal.tenants), 1)
        self.assertTrue(next(iter(principal.tenants)).startswith('account:user-'))


class DesktopSessionTests(unittest.TestCase):
    def test_launcher_secret_bootstraps_only_local_browser(self):
        from agent_factory.desktop import desktop_app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'AGENT_FACTORY_API_TOKEN': 'private-local-token' * 4, 'LOKVETIA_SSO_CLIENT': ''}):
            app = desktop_app(Path(directory), launch_secret='launch-secret', instance='test-instance')
            with TestClient(app, base_url='http://127.0.0.1') as client:
                self.assertFalse(client.get('/auth/session').json()['authenticated'])
                headers = {'X-Agent-Factory-Session': 'true', 'X-Lokvetia-Desktop':'wrong'}
                self.assertEqual(client.post('/desktop/session', headers=headers).status_code, 403)
                headers['X-Lokvetia-Desktop'] = 'launch-secret'
                self.assertEqual(client.post('/desktop/session', headers=dict(headers, Origin='https://evil.example')).status_code, 403)
                self.assertEqual(client.post('/desktop/session', headers=headers).status_code, 200)
                self.assertTrue(client.get('/auth/session').json()['authenticated'])
                self.assertNotIn('launch-secret', client.get('/desktop/status').text)

    def test_install_keeps_user_data(self):
        from agent_factory.desktop import install
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'download.exe'; source.write_bytes(b'new')
            target = root / 'program'; target.mkdir()
            (target / 'game.txt').write_text('keep')
            (target / 'Lokvetia-Core.exe').write_bytes(b'old')
            installed = install(source, target, shortcut=False)
            self.assertEqual(installed.read_bytes(), b'new')
            self.assertEqual((target / 'Lokvetia-Core.previous.exe').read_bytes(), b'old')
            self.assertEqual((target / 'game.txt').read_text(), 'keep')
