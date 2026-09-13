"""Stable streaming gateway: route changes never close existing upstream calls."""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route
import uvicorn

state = Path(os.environ.get('DEPLOY_PUBLIC_STATE', '/state'))
HOP = {b'connection', b'keep-alive', b'proxy-authenticate', b'proxy-authorization', b'te', b'trailer', b'transfer-encoding', b'upgrade'}
# Signed-in operator pages served from published state, never from an application.
PAGES = {
    '/deployments': ('dashboard.html', 'status.json',
                     {'projects': {}, 'error': 'Deployment controller has not reported yet'}),
    '/progress': ('progress.html', 'progress.json',
                  {'projects': [], 'error': 'Progress report has not been generated yet'}),
}


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=None, write=60, pool=30), follow_redirects=False) as client:
        app.state.client = client
        yield


async def proxy(request: Request):
    hosts = request.headers.getlist('host')
    if len(hosts) != 1:
        return JSONResponse({'error': 'Invalid host'}, status_code=400)
    hostname = hosts[0].split(':')[0]
    private = hostname == 'identity.internal'
    if private:
        hostname = 'id.lokvetia.com'
    try:
        routes = json.loads((state / 'routes.json').read_text())
        route = routes[hostname]
        upstream = 'http://' + route['container'] + ':8080'
    except (OSError, ValueError, KeyError):
        return JSONResponse({'error': 'Route is not ready'}, status_code=503)
    if request.url.path.startswith('/backchannel/') and not private:
        return JSONResponse({'error': 'Private service endpoint'}, status_code=404)
    if private and not request.url.path.startswith('/backchannel/'):
        return JSONResponse({'error': 'Private service endpoint'}, status_code=404)
    # Preserve the existing domain adapter's origin checks. Never trust a client
    # supplied alternate identity or rewrite application authorization headers.
    headers = [(k, v) for k, v in request.headers.raw if k.lower() not in HOP]
    if private:
        headers = [(k, v) for k, v in headers if k.lower() != b'host'] + [(b'host', b'localhost')]
    client = request.app.state.client
    page = request.url.path[:-len('/status')] if request.url.path.endswith('/status') else request.url.path
    if page in PAGES:
        document, data, unavailable = PAGES[page]
        try:
            auth = await client.get(upstream + '/auth/session', headers=headers, timeout=8)
            if (auth.status_code != 200 or not auth.json().get('authenticated')
                    or not auth.json().get('workspace_access', False)):
                return RedirectResponse('/login', status_code=303) if request.url.path == page else JSONResponse({'error': 'Sign-in required'}, status_code=401)
        except (httpx.HTTPError, ValueError):
            return JSONResponse({'error': 'Authorization service unavailable'}, status_code=503)
        if request.url.path == page:
            return FileResponse(state / document, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        try:
            payload = json.loads((state / data).read_text())
        except (OSError, ValueError):
            payload = unavailable
        return JSONResponse(payload, headers={'Cache-Control': 'no-store'})
    url = upstream + request.scope.get('raw_path', request.url.path.encode()).decode('ascii')
    if request.url.query:
        url += '?' + request.url.query
    try:
        response = await client.send(client.build_request(request.method, url, headers=headers, content=request.stream()), stream=True)
    except httpx.HTTPError:
        # Retrying a POST could create duplicate games or AI requests.
        return JSONResponse({'error': 'Application is unavailable'}, status_code=502)
    result = StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                               background=BackgroundTask(response.aclose))
    result.raw_headers = [(k, v) for k, v in response.headers.raw if k.lower() not in HOP]
    return result


app = Starlette(routes=[Route('/{path:path}', proxy, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])], lifespan=lifespan)
if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8080, proxy_headers=False, access_log=False)
