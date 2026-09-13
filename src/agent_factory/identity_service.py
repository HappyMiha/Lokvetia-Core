"""One invitation-only account and organization service for the related sites.

Authorization codes are single-use, expire in 60 seconds, and bind the exact
client, redirect URI, browser login and PKCE challenge. No bearer tokens travel
through browser redirect URLs. Back-channel credentials remain server-side.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from .email_auth import Accounts, EmailAccess, digest
from .email_auth_web import install_routes
from .http_auth import COOKIE, LocalHTTPBoundary, Policy
from .sso import challenge


def create_identity_app(folder: Path, clients: dict):
    app = FastAPI(title='Lokvetia Account', docs_url=None, redoc_url=None, openapi_url=None)
    access = EmailAccess(folder / 'accounts.sqlite3')
    accounts = access.accounts
    organization = os.getenv('LOKVETIA_ORGANIZATION', 'lokvetia')
    organization_name = os.getenv('LOKVETIA_ORGANIZATION_NAME', 'Lokvetia')
    with closing(accounts.connect()) as db, db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS sso_codes (hash TEXT PRIMARY KEY, client TEXT NOT NULL,
                redirect TEXT NOT NULL, challenge TEXT NOT NULL, session_hash TEXT NOT NULL,
                expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sso_grants (hash TEXT PRIMARY KEY, client TEXT NOT NULL,
                session_hash TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles (email TEXT PRIMARY KEY, display_name TEXT NOT NULL);
        ''')
    app.state.local_access = access
    app.add_middleware(LocalHTTPBoundary, access=access)
    install_routes(app, access, product='Lokvetia Account',
                   personal_registration=os.getenv('LOKVETIA_PERSONAL_REGISTRATION') == '1')
    app.mount('/assets', StaticFiles(directory=Path(__file__).parent / 'static'), name='assets')
    from .desktop_downloads import install_download_routes
    install_download_routes(app)

    def session_row(cookie):
        if not cookie:
            return None
        with closing(accounts.connect()) as db:
            return db.execute('''SELECT s.* FROM email_sessions s JOIN accounts a ON a.email=s.email
                WHERE s.hash=? AND s.policy=? AND s.expires>? AND a.enabled=1''',
                              (digest(cookie), access.policy().digest, time.time())).fetchone()

    @app.get('/auth/session')
    async def session(request: Request):
        principal = request.state.local_principal
        return {'authenticated': principal is not None,
                'workspace_access': bool(principal and principal.role != 'account_user')}

    @app.get('/login')
    async def login_page():
        return RedirectResponse('/auth/account', status_code=303)

    @app.get('/')
    @app.get('/profile')
    @app.get('/organizations')
    async def portal(request: Request):
        if not request.state.local_principal:
            return RedirectResponse('/auth/account?next=' + request.url.path, status_code=303)
        return FileResponse(Path(__file__).parent / 'static' / 'identity.html')

    @app.get('/api/account')
    async def account(request: Request):
        row = await run_in_threadpool(session_row, request.cookies.get(COOKIE))
        if not row:
            return JSONResponse({'error': 'Email sign-in is required'}, status_code=401)
        with closing(accounts.connect()) as db:
            profile = db.execute('SELECT display_name FROM profiles WHERE email=?', (row['email'],)).fetchone()
            members = db.execute('SELECT email, principal FROM accounts WHERE enabled=1').fetchall()
        admin = request.state.local_principal.role == 'operations_owner'
        personal = request.state.local_principal.role == 'account_user'
        return dict(email=row['email'], display_name=profile[0] if profile else '',
                    organization=None if personal else dict(id=organization, name=organization_name), can_invite=admin,
                    personal_account=personal, email_verified=False,
                    members=[dict(email=r['email'], role=json.loads(r['principal'])['role']) for r in members] if admin else [],
                    applications=[dict(name='Завантажити Lokvetia Core', url='/downloads')] if personal else
                                 [dict(name=c['name'], url=c['origin']) for c in clients.values()])

    @app.post('/api/account')
    async def update_profile(request: Request):
        if request.headers.get('X-Agent-Factory-Session') != 'true':
            return JSONResponse({'error': 'Session intent required'}, status_code=403)
        raw = await request.body()
        if len(raw) > 1024:
            return JSONResponse({'error': 'Profile is too large'}, status_code=400)
        try:
            data = json.loads(raw)
            if set(data) != {'display_name'} or not isinstance(data['display_name'], str) or len(data['display_name']) > 100:
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse({'error': 'Invalid profile'}, status_code=400)
        row = await run_in_threadpool(session_row, request.cookies.get(COOKIE))
        if not row:
            return JSONResponse({'error': 'Email sign-in required'}, status_code=401)
        with closing(accounts.connect()) as db, db:
            db.execute('INSERT INTO profiles VALUES (?,?) ON CONFLICT(email) DO UPDATE SET display_name=excluded.display_name',
                       (row['email'], data['display_name'].strip()))
        return {'saved': True}

    @app.get('/authorize')
    async def authorize(request: Request):
        query = request.query_params
        client = clients.get(query.get('client_id', ''))
        if (not client or query.get('redirect_uri') != client['origin'] + '/auth/sso/callback'
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', query.get('code_challenge', ''))
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', query.get('state', ''))):
            return JSONResponse({'error': 'Invalid sign-in destination'}, status_code=400)
        row = await run_in_threadpool(session_row, request.cookies.get(COOKIE))
        if not row:
            return RedirectResponse('/auth/account?' + urlencode({'next': '/authorize?' + urlencode(dict(query))}), status_code=303)
        code = secrets.token_urlsafe(32)
        with closing(accounts.connect()) as db, db:
            db.execute('DELETE FROM sso_codes WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO sso_codes VALUES (?,?,?,?,?,?)',
                       (digest(code), query['client_id'], query['redirect_uri'], query['code_challenge'], row['hash'], time.time() + 60))
        return RedirectResponse(query['redirect_uri'] + '?' + urlencode(dict(code=code, state=query['state'])), status_code=303)

    @app.post('/backchannel/{operation}')
    async def backchannel(operation: str, request: Request):
        raw = await request.body()
        if len(raw) > 4096:
            return JSONResponse({'error': 'Invalid request'}, status_code=400)
        try:
            data = json.loads(raw)
            client_id = data['client_id']
            client = clients.get(client_id)
            supplied = request.headers.get('authorization', '')
            if not client or not secrets.compare_digest(supplied, 'Bearer ' + client['secret']):
                return JSONResponse({'error': 'Client authentication required'}, status_code=401)
            with closing(accounts.connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                if operation == 'token':
                    code = db.execute('SELECT * FROM sso_codes WHERE hash=?', (digest(data['code']),)).fetchone()
                    if (not code or code['expires'] <= time.time() or code['client'] != client_id
                            or code['redirect'] != data['redirect_uri']
                            or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', data['code_verifier'])
                            or not secrets.compare_digest(code['challenge'], challenge(data['code_verifier']))):
                        raise ValueError('Code is invalid or expired')
                    db.execute('DELETE FROM sso_codes WHERE hash=?', (code['hash'],))
                    session_hash = code['session_hash']
                else:
                    grant = db.execute('SELECT * FROM sso_grants WHERE hash=? AND client=? AND expires>?',
                                       (digest(data['token']), client_id, time.time())).fetchone()
                    if not grant:
                        return {'active': False}
                    session_hash = grant['session_hash']
                session = db.execute('''SELECT s.expires, a.principal FROM email_sessions s JOIN accounts a ON a.email=s.email
                    WHERE s.hash=? AND s.expires>? AND s.policy=? AND a.enabled=1''',
                                     (session_hash, time.time(), access.policy().digest)).fetchone()
                if not session:
                    return JSONResponse({'error': 'Login expired'}, status_code=401)
                if operation == 'token':
                    token = secrets.token_urlsafe(48)
                    db.execute('INSERT INTO sso_grants VALUES (?,?,?,?)', (digest(token), client_id, session_hash, session['expires']))
                    return {'token': token, 'expires': session['expires']}
                if operation == 'introspect':
                    return dict(active=True, principal=json.loads(session['principal']), organization=organization)
                if operation == 'logout':
                    db.execute('DELETE FROM email_sessions WHERE hash=?', (session_hash,))
                    db.execute('DELETE FROM sso_grants WHERE session_hash=?', (session_hash,))
                    return {'logged_out': True}
                raise ValueError('Unknown operation')
        except (ValueError, TypeError, KeyError):
            return JSONResponse({'error': 'Invalid authorization request'}, status_code=400)
    return app
