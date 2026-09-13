"""Serve only explicitly published desktop artifacts from a separate directory."""
import hashlib
import json
import os
from pathlib import Path
import re

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse


def release_files(directory: Path):
    directory = directory.resolve()
    data = json.loads((directory / 'desktop-release.json').read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or not isinstance(data.get('artifacts'), list):
        raise ValueError('Invalid desktop release')
    files = {}
    for item in data['artifacts']:
        name = item['name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]+', name):
            raise ValueError('Invalid artifact name')
        path = (directory / name).resolve()
        if path.parent != directory or not path.is_file() or path.stat().st_size != item['size_bytes']:
            raise ValueError('Artifact is missing or changed')
        if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('Invalid artifact checksum')
        files[name] = path
    if not files:
        raise ValueError('No desktop artifacts')
    return data, files


def install_download_routes(app):
    def published():
        location = os.getenv('LOKVETIA_DESKTOP_RELEASE_DIR')
        if not location:
            raise ValueError('Desktop release is not configured')
        return release_files(Path(location))

    @app.get('/downloads', include_in_schema=False)
    async def downloads():
        return FileResponse(Path(__file__).parent / 'static' / 'downloads.html')

    @app.get('/downloads/manifest.json', include_in_schema=False)
    async def manifest():
        try:
            data, _ = published()
            return JSONResponse(data)
        except (OSError, ValueError, KeyError, TypeError):
            return JSONResponse({'error': 'Desktop download is not published yet'}, status_code=503)

    @app.get('/downloads/files/{name}', include_in_schema=False)
    async def artifact(name: str, request: Request):
        if request.state.local_principal is None:
            return JSONResponse({'error': 'Sign in to download'}, status_code=401)
        try:
            data, files = published()
            if name not in files:
                return JSONResponse({'error': 'Unknown download'}, status_code=404)
            item = next(a for a in data['artifacts'] if a['name'] == name)
            from starlette.concurrency import run_in_threadpool
            def checksum():
                with files[name].open('rb') as source:
                    return hashlib.file_digest(source, 'sha256').hexdigest()
            if await run_in_threadpool(checksum) != item['sha256']:
                raise ValueError('Artifact checksum mismatch')
            return FileResponse(files[name], filename=name, media_type='application/octet-stream')
        except (OSError, ValueError, KeyError, TypeError):
            return JSONResponse({'error': 'Download unavailable'}, status_code=503)
