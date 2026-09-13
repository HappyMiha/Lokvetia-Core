"""Same-origin, local-owner-only automatic AI setup routes."""
import asyncio
import json
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from .ai_setup import AISetup
from .http_auth import LocalAccess, LocalHTTPBoundary


def install_routes(app, workspace, database, *, service=None):
    if not isinstance(app.state.local_access, LocalAccess) or not any(m.cls is LocalHTTPBoundary for m in app.user_middleware):
        raise ValueError('AI setup requires local HTTP authority')
    setup = service or AISetup(workspace, database)
    app.state.ai_setup = setup

    def owner(request, mutation=False):
        principal = request.state.local_principal
        required = {'read', 'write', 'control'} if mutation else {'read'}
        if (principal is None or principal.role != 'operations_owner'
                or not required <= principal.scopes or not {'local', '*'} & principal.tenants):
            raise HTTPException(403, 'local_owner_required')
        if mutation and request.headers.get('X-Agent-Factory-Confirm') != 'true':
            raise HTTPException(400, 'confirmation_required')
        return principal.actor

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    @app.get('/connect', include_in_schema=False)
    async def page(request: Request):
        if request.state.local_principal is None:
            return RedirectResponse('/login', status_code=303)
        owner(request)
        return FileResponse(Path(__file__).parent / 'static' / 'ai-connect.html')

    @app.get('/api/ai-setup')
    async def scan(request: Request):
        return response(await asyncio.to_thread(setup.scan, owner(request)))

    @app.post('/api/ai-setup')
    async def start(request: Request):
        actor = owner(request, True)
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 4096:
                    raise HTTPException(400, 'invalid_request')
                raw.extend(chunk)
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) not in ({'provider', 'command_id'}, {'provider', 'command_id', 'secret'}):
                raise ValueError()
            access = app.state.local_access
            digest = request.state.local_policy.digest
            authorization = request.headers.get('authorization')
            cookie = request.cookies.get('agent_factory_session')
            def authorized():
                try:
                    policy = access.policy()
                    who = access.authenticate(policy, authorization, cookie)
                    return bool(policy.digest == digest and who and who.actor == actor
                        and who.role == 'operations_owner' and {'write', 'control'} <= who.scopes)
                except Exception:
                    return False
            return response(await asyncio.to_thread(setup.start, data['provider'], actor,
                data['command_id'], authorize=authorized, secret=data.get('secret')))
        except PermissionError:
            raise HTTPException(403, 'local_owner_required') from None
        except (ValueError, TypeError):
            raise HTTPException(409, 'setup_busy_or_invalid_request') from None
        finally:
            raw[:] = b'\x00' * len(raw)
            if 'data' in locals() and isinstance(data,dict):
                data.clear()

    @app.get('/api/ai-setup/{ident}')
    async def status(ident: str, request: Request):
        try:
            return response(await asyncio.to_thread(setup.get, ident, owner(request)))
        except KeyError:
            raise HTTPException(404, 'setup_not_found') from None

    @app.delete('/api/ai-setup/connections/{provider}')
    async def disconnect(provider: str, request: Request):
        actor=owner(request,True)
        try:
            return response(await asyncio.to_thread(setup.disconnect,provider,actor))
        except ValueError:
            raise HTTPException(400,'invalid_provider') from None

    @app.delete('/api/ai-setup/{ident}')
    async def cancel(ident: str, request: Request):
        try:
            return response(await asyncio.to_thread(setup.cancel, ident, owner(request, True)))
        except KeyError:
            raise HTTPException(404, 'setup_not_found') from None
