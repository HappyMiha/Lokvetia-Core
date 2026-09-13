from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,MagicMock

from fastapi.testclient import TestClient
from agent_factory import studio_updates as updates
from agent_factory.web import create_app


def fixture(version='0.1.0-preview.3'):
    name=f'Lokvetia-Core-{version}-win-x64.exe'
    base=updates.REPOSITORY+'/releases/download/v'+version+'/'
    release={'draft':False,'tag_name':'v'+version,'assets':[
        {'name':name,'state':'uploaded','size':1234,'browser_download_url':base+name},
        {'name':'desktop-release.json','state':'uploaded','size':500,'browser_download_url':base+'desktop-release.json'}]}
    manifest={'schema_version':1,'version':version,'tested':['isolated install passed'],
        'artifacts':[{'name':name,'platform':'windows-x64','size_bytes':1234,'sha256':'a'*64}]}
    return [release],manifest


class ReleaseTests(unittest.TestCase):
    def test_versions_compare_numerically_and_stable_is_newer_than_preview(self):
        self.assertLess(updates.version_key('0.1.0-preview.2'),updates.version_key('0.1.0-preview.10'))
        self.assertLess(updates.version_key('0.1.0-preview.10'),updates.version_key('0.1.0'))
        self.assertLess(updates.version_key('0.1.0'),updates.version_key('0.2.0-alpha.1'))
        for value in ['latest','1.2','v1.2.3','01.2.3','1.2.3-preview.01','../../file']:
            with self.subTest(value=value),self.assertRaises(ValueError):updates.version_key(value)

    def test_only_uploaded_verified_windows_artifact_from_this_repository_is_selected(self):
        listing,manifest=fixture();draft,_=fixture('9.0.0');draft[0]['draft']=True
        fetch=MagicMock(side_effect=[draft+listing,manifest])
        result=updates.latest_release(fetch)
        self.assertEqual(result['version'],'0.1.0-preview.3')
        self.assertTrue(result['download_url'].endswith('/Lokvetia-Core-0.1.0-preview.3-win-x64.exe'))
        self.assertEqual(fetch.call_count,2)

    def test_rejects_changed_host_unverified_release_checksum_and_size(self):
        def changed_host(listing,manifest):listing[0]['assets'][0]['browser_download_url']='https://evil.example/game.exe'
        def changed_version(listing,manifest):manifest['version']='0.1.0-preview.9'
        def untested(listing,manifest):manifest['tested']=[]
        def changed_size(listing,manifest):manifest['artifacts'][0]['size_bytes']=12
        def malformed_hash(listing,manifest):manifest['artifacts'][0]['sha256']='z'*64
        def traversal(listing,manifest):manifest['artifacts'][0]['name']='../../file.exe'
        for mutate in [changed_host,changed_version,untested,changed_size,malformed_hash,traversal]:
            listing,manifest=fixture();mutate(listing,manifest)
            with self.subTest(case=mutate.__name__),self.assertRaises(ValueError):
                updates.latest_release(MagicMock(side_effect=[listing,manifest]))

    def test_redirects_cannot_leave_github_or_use_http_credentials_or_other_ports(self):
        for url in ['https://127.0.0.1/','https://github.com.evil.example/','http://github.com/',
                    'https://user:pass@github.com/','https://github.com:444/']:
            with self.subTest(url=url),self.assertRaises(ValueError):updates.official_transport_url(url)
        updates.official_transport_url('https://release-assets.githubusercontent.com/github-production-release-asset/abc')

    def test_source_and_missing_frozen_metadata_never_fabricate_a_version(self):
        with patch.object(sys,'frozen',False,create=True):self.assertEqual(updates.installed_build(),{'kind':'source','version':None})
        with tempfile.TemporaryDirectory() as folder,patch.object(sys,'frozen',True,create=True),patch.object(sys,'_MEIPASS',folder,create=True):
            self.assertIsNone(updates.installed_build()['version'])
            (Path(folder)/'lokvetia-build.json').write_text(json.dumps({'version':'0.1.0-preview.3','source_commit':'a'*40}))
            self.assertEqual(updates.installed_build()['version'],'0.1.0-preview.3')

    def test_success_cache_and_network_failure_do_not_return_a_stale_download(self):
        fetch=MagicMock(side_effect=list(fixture())+[OSError('private network details')])
        checker=updates.UpdateChecker(fetch=fetch)
        self.assertIsNotNone(checker.check()['latest']);checker.check();self.assertEqual(fetch.call_count,2)
        checker.until=0
        result=checker.check()
        self.assertIsNone(result['latest']);self.assertEqual(result['error'],'unavailable')
        self.assertNotIn('private',json.dumps(result))


class UpdateRouteTests(unittest.TestCase):
    def test_initial_info_is_local_and_check_requires_access(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'AGENT_FACTORY_API_TOKEN':'synthetic-token','AGENT_FACTORY_API_ACTOR':'Founder'}), \
             patch.object(updates,'fetch_json',side_effect=list(fixture())) as fetch, \
             TestClient(create_app(Path(folder),Path(folder)/'state.db'),base_url='http://localhost') as client:
            self.assertEqual(client.get('/api/studio/updates/check').status_code,401)
            fetch.assert_not_called()
            headers={'Authorization':'Bearer synthetic-token'}
            self.assertEqual(client.get('/api/studio/updates',headers=headers).status_code,200)
            fetch.assert_not_called()
            with patch.object(updates,'installed_build',return_value={'kind':'desktop','version':'0.1.0-preview.2'}):
                result=client.get('/api/studio/updates/check',headers=headers).json()
            self.assertEqual(result['status'],'update');self.assertEqual(fetch.call_count,2)
            with patch.object(updates,'installed_build',return_value={'kind':'desktop','version':'0.1.0-preview.3'}):
                self.assertEqual(client.get('/api/studio/updates/check',headers=headers).json()['status'],'current')
            with patch.object(updates,'installed_build',return_value={'kind':'desktop','version':'0.1.0-preview.4'}):
                self.assertEqual(client.get('/api/studio/updates/check',headers=headers).json()['status'],'ahead')
            self.assertEqual(fetch.call_count,2)


if __name__=='__main__':unittest.main()
