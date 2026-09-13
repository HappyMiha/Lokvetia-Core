"""Same-origin invitation registration and email sign-in for the test workspaces."""
import json
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from .email_auth import email_address
from .http_auth import COOKIE, Principal


def install_routes(app, access, *, product='Lokvetia Core', personal_registration=False):
    page = Path(__file__).parent / 'static' / 'account.html'

    async def document(request):
        if request.headers.get('X-Agent-Factory-Session') != 'true':
            raise ValueError('Same-origin session intent is required')
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise ValueError('JSON is required')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise ValueError('Request is too large')
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError('Invalid request')
        return value

    @app.get('/auth/account/config', include_in_schema=False)
    async def configuration(request: Request):
        principal = request.state.local_principal
        return dict(product=product, registration='personal' if personal_registration else 'invitation', authenticated=principal is not None,
                    actor=principal.actor if principal else None,
                    can_invite=bool(principal and principal.role == 'operations_owner'))

    @app.get('/register', include_in_schema=False)
    @app.get('/auth/account', include_in_schema=False)
    async def account_page():
        return FileResponse(page)

    @app.post('/auth/account/register', include_in_schema=False)
    async def register(request: Request):
        try:
            data = await document(request)
            if set(data) != {'email', 'password', 'invitation'}:
                raise ValueError('Invalid registration request')
            if personal_registration and data['invitation'] == '':
                await run_in_threadpool(access.accounts.register_personal, data['email'], data['password'],
                                        peer=request.client.host if request.client else 'unknown')
            else:
                await run_in_threadpool(access.accounts.register, data['email'], data['password'], data['invitation'])
            return JSONResponse({'registered': True})
        except (ValueError, TypeError, KeyError) as error:
            return JSONResponse({'error': str(error)}, status_code=400)

    @app.post('/auth/account/login', include_in_schema=False)
    async def login(request: Request):
        try:
            data = await document(request)
            if set(data) != {'email', 'password', 'remember'} or type(data['remember']) is not bool:
                raise ValueError('Invalid login request')
            cookie, ttl = await run_in_threadpool(access.accounts.login, data['email'], data['password'],
                request.state.local_policy, remember=data['remember'], peer=request.client.host if request.client else 'unknown')
            await run_in_threadpool(access.logout, request.cookies.get(COOKIE))
            response = JSONResponse({'authenticated': True})
            response.set_cookie(COOKIE, cookie, max_age=ttl if data['remember'] else None,
                                httponly=True, secure=request.url.scheme == 'https', samesite='lax', path='/')
            return response
        except (ValueError, TypeError, KeyError) as error:
            return JSONResponse({'error': str(error)}, status_code=401)

    @app.post('/auth/account/logout', include_in_schema=False)
    async def logout(request: Request):
        try:
            await document(request)
        except ValueError as error:
            return JSONResponse({'error': str(error)}, status_code=403)
        await run_in_threadpool(access.logout, request.cookies.get(COOKIE))
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(COOKIE, path='/', httponly=True, samesite='lax', secure=request.url.scheme == 'https')
        return response

    @app.post('/auth/account/invitations', include_in_schema=False)
    async def invite(request: Request):
        principal = request.state.local_principal
        if not principal or principal.role != 'operations_owner' or 'control' not in principal.scopes:
            return JSONResponse({'error': 'Administrator access is required'}, status_code=403)
        try:
            data = await document(request)
            if set(data) != {'email'}:
                raise ValueError('An email address is required')
            email = email_address(data['email'])
            member = Principal(email, 'mission_owner', frozenset({'read', 'write'}) & principal.scopes, principal.tenants)
            token = await run_in_threadpool(access.accounts.invite, email, member)
            from urllib.parse import urlencode
            return JSONResponse({'path': '/register#' + urlencode({'invite': token, 'email': email})})
        except (ValueError, TypeError, KeyError) as error:
            return JSONResponse({'error': str(error)}, status_code=400)
