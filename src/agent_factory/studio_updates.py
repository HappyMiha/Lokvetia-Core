"""Read-only release checks. Download links never execute or install a file."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from fastapi import HTTPException, Request as WebRequest

REPOSITORY = 'https://github.com/HappyMiha/Lokvetia-Core'
RELEASES = REPOSITORY + '/releases'
API = 'https://api.github.com/repos/HappyMiha/Lokvetia-Core/releases?per_page=10'
VERSION = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(alpha|beta|preview|rc)\.(0|[1-9][0-9]*))?'


def version_key(value):
    match = re.fullmatch(VERSION, str(value))
    if not match:
        raise ValueError('Invalid release version')
    major, minor, patch, label, number = match.groups()
    return (int(major), int(minor), int(patch), 0 if label else 1, label or '', int(number or 0))


def installed_build():
    if not getattr(sys, 'frozen', False):
        return {'kind':'source', 'version':None}
    try:
        data = json.loads((Path(sys._MEIPASS) / 'lokvetia-build.json').read_text(encoding='utf-8'))
        version_key(data['version'])
        if not re.fullmatch('[0-9a-f]{40}', data['source_commit']):
            raise ValueError('Invalid build identity')
        return {'kind':'desktop', 'version':data['version'], 'source_commit':data['source_commit']}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'kind':'desktop', 'version':None}


def official_transport_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {
            'api.github.com','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}
            or parsed.port not in {None,443} or parsed.username or parsed.password):
        raise ValueError('Unexpected release host')


class OfficialRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, request, fp, code, message, headers, newurl):
        official_transport_url(newurl)
        return super().redirect_request(request,fp,code,message,headers,newurl)


def fetch_json(url):
    official_transport_url(url)
    request = Request(url,headers={'User-Agent':'Lokvetia-Core-update-check', 'Accept':'application/json'})
    # No account cookies, bearer tokens, project data or inherited proxy credentials.
    opener = build_opener(ProxyHandler({}),OfficialRedirects())
    with opener.open(request,timeout=8) as response:
        data = response.read(262145)
    if len(data)>262144:
        raise ValueError('Release response is too large')
    return json.loads(data)


def latest_release(fetch=fetch_json):
    releases = fetch(API)
    if not isinstance(releases,list):
        raise ValueError('Invalid release listing')
    candidates=[]
    for release in releases:
        if not isinstance(release,dict) or release.get('draft'):
            continue
        tag=release.get('tag_name','')
        if not isinstance(tag,str) or not tag.startswith('v'):
            continue
        try:
            key=version_key(tag[1:])
        except ValueError:
            continue
        assets=release.get('assets',[])
        if not isinstance(assets,list):continue
        name=f'Lokvetia-Core-{tag[1:]}-win-x64.exe'
        required=[a for a in assets if isinstance(a,dict) and a.get('name') in {'desktop-release.json',name} and a.get('state')=='uploaded']
        if len(required)==2 and {a['name'] for a in required}=={'desktop-release.json',name}:
            candidates.append((key,release,required))
    if not candidates:
        raise ValueError('No published Windows installer')
    _,release,assets=max(candidates,key=lambda row:row[0])
    version=release['tag_name'][1:]
    base=REPOSITORY+'/releases/download/v'+version+'/'
    # Build URLs ourselves; metadata cannot redirect a download to another repo.
    for asset in assets:
        if asset.get('browser_download_url') != base+asset['name']:
            raise ValueError('Unexpected release asset URL')
    manifest=fetch(base+'desktop-release.json')
    if (not isinstance(manifest,dict) or manifest.get('schema_version') != 1
            or manifest.get('version') != version or not isinstance(manifest.get('tested'),list)
            or not manifest['tested']):
        raise ValueError('Release is not verified for distribution')
    name=f'Lokvetia-Core-{version}-win-x64.exe'
    files=[a for a in manifest.get('artifacts',[]) if isinstance(a,dict) and a.get('platform')=='windows-x64']
    if len(files)!=1:
        raise ValueError('Missing or ambiguous Windows installer')
    artifact=files[0]
    asset=next(a for a in assets if a['name']==name)
    if (artifact.get('name') != name or type(artifact.get('size_bytes')) is not int
            or not 0 < artifact['size_bytes'] <= 1024**3 or asset.get('size') != artifact['size_bytes']
            or not re.fullmatch('[0-9a-f]{64}',str(artifact.get('sha256','')))):
        raise ValueError('Invalid Windows installer metadata')
    return {'version':version,'download_url':base+name,'size_bytes':artifact['size_bytes'],
            'sha256':artifact['sha256'],'release_url':RELEASES+'/tag/v'+version}


class UpdateChecker:
    def __init__(self, *, fetch=None):
        self.fetch=fetch or fetch_json
        self.lock=threading.Lock()
        self.cached=None
        self.until=0

    def check(self):
        with self.lock:
            if self.cached is not None and time.monotonic()<self.until:
                return self.cached
            try:
                latest=latest_release(self.fetch)
                result={'latest':latest,'error':None}
            except (OSError,ValueError,KeyError,TypeError):
                result={'latest':None,'error':'unavailable'}
            result['checked_at']=datetime.now(timezone.utc).isoformat()
            self.cached=result
            self.until=time.monotonic()+(60 if result['latest'] else 3)
            return result


def install_routes(app):
    checker=UpdateChecker()

    def access(request):
        principal=request.state.local_principal
        if principal is None or 'read' not in principal.scopes:
            raise HTTPException(403,'studio_access_denied')

    @app.get('/api/studio/updates')
    def info(request: WebRequest):
        access(request)
        return {'installed':installed_build(),'release_url':RELEASES}

    @app.get('/api/studio/updates/check')
    def check(request: WebRequest):
        access(request)
        installed=installed_build()
        result=dict(checker.check(),installed=installed,release_url=RELEASES)
        latest=result['latest']
        if not latest:result['status']='unavailable'
        elif not installed['version']:result['status']='available'
        else:
            comparison=(version_key(latest['version'])>version_key(installed['version']))-(version_key(latest['version'])<version_key(installed['version']))
            result['status']={1:'update',0:'current',-1:'ahead'}[comparison]
        return result
