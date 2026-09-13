"""Per-user desktop installation and a loopback-only Core launcher.

The frozen executable includes Python. It does not include accounts, AI keys,
models, engine binaries, or the build machine's workspace.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from fastapi import Request


def default_workspace() -> Path:
    return Path(os.getenv('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'Lokvetia' / 'workspace'


def install(source: Path, destination: Path, *, shortcut: bool = True) -> Path:
    """Install this exact binary for this user; never touch their game data."""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'Lokvetia-Core.exe'
    if source.resolve() != target.resolve():
        if target.exists():
            # An upgrade is explicit; keep the preceding executable for rollback.
            shutil.copy2(target, destination / 'Lokvetia-Core.previous.exe')
        temporary = destination / 'Lokvetia-Core.new.exe'
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    if shortcut and os.name == 'nt':
        # Arguments stay data, including user paths containing quotes or spaces.
        env = dict(os.environ, LOKVETIA_SHORTCUT_TARGET=str(target),
                   LOKVETIA_SHORTCUT_FOLDER=str(destination))
        script = """$ErrorActionPreference='Stop'
$folder=Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut((Join-Path $folder 'Lokvetia Core.lnk'))
$link.TargetPath=$env:LOKVETIA_SHORTCUT_TARGET
$link.Arguments='--serve'
$link.WorkingDirectory=$env:LOKVETIA_SHORTCUT_FOLDER
$link.Description='Lokvetia Core local studio'
$link.Save()
"""
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                       env=env, check=True, timeout=30,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return target


def installation_window() -> int:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.title('Lokvetia Core — встановлення')
    root.geometry('560x330')
    root.configure(bg='#f5f2e9')
    target = Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'Lokvetia Core'
    tk.Label(root, text='Lokvetia Core', font=('Segoe UI', 25), bg='#f5f2e9', fg='#102c35').pack(pady=(25, 10))
    tk.Label(root, text='Локальна AI-студія для вашого ПК.\nPython включено. Моделі та Godot налаштовуються окремо.',
             font=('Segoe UI', 11), bg='#f5f2e9', wraplength=500).pack(pady=10)
    tk.Label(root, text=str(target), font=('Segoe UI', 9), bg='#f5f2e9', wraplength=500).pack(pady=10)

    def proceed():
        try:
            installed = install(Path(sys.executable), target)
            env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
            subprocess.Popen([str(installed), '--serve'], env=env,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            root.destroy()
        except (OSError, subprocess.SubprocessError) as error:
            messagebox.showerror('Не вдалося встановити', str(error))
    tk.Button(root, text='Встановити й відкрити', command=proceed, font=('Segoe UI', 12),
              bg='#102c35', fg='white', padx=20, pady=10).pack(pady=15)
    root.mainloop()
    return 0


def desktop_app(workspace: Path, *, launch_secret: str, instance: str):
    from starlette.responses import FileResponse, JSONResponse
    from .http_auth import COOKIE
    from .web import create_app
    app = create_app(workspace, workspace / 'state.db')
    access = app.state.local_access

    @app.get('/desktop/status', include_in_schema=False)
    async def status():
        return {'product': 'Lokvetia Core Desktop', 'instance': instance}

    @app.get('/desktop', include_in_schema=False)
    async def welcome():
        return FileResponse(Path(__file__).parent / 'static' / 'desktop.html')

    @app.post('/desktop/session', include_in_schema=False)
    async def session(request: Request):
        if request.headers.get('X-Agent-Factory-Session') != 'true':
            return JSONResponse({'error': 'Session intent required'}, status_code=403)
        supplied = request.headers.get('X-Lokvetia-Desktop', '')
        if not secrets.compare_digest(supplied, launch_secret):
            return JSONResponse({'error': 'Open Core from the Start menu'}, status_code=403)
        cookie = access.login(request.state.local_policy, request.state.local_policy.token)
        response = JSONResponse({'authenticated': bool(cookie)})
        if cookie:
            response.set_cookie(COOKIE, cookie, httponly=True, samesite='strict', path='/')
        return response

    @app.post('/desktop/quit', include_in_schema=False)
    async def quit_app(request: Request):
        if (request.headers.get('X-Agent-Factory-Session') != 'true'
                or request.state.local_principal is None
                or request.state.local_principal.actor != 'local-owner'):
            return JSONResponse({'error': 'Local owner session required'}, status_code=403)
        if hasattr(app.state, 'desktop_server'):
            app.state.desktop_server.should_exit = True
        return JSONResponse({'stopping': True})
    return app


def serve(workspace: Path, *, port: int = 0, browser: bool = True) -> int:
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # Desktop always has its own local authority. Never inherit cloud SSO or
    # remote worker configuration from the environment used to launch an installer.
    for key in list(os.environ):
        if key.startswith(('LOKVETIA_SSO_', 'LOKVETIA_IDENTITY_', 'AGENT_FACTORY_API_')):
            os.environ.pop(key)
    os.environ.update(AGENT_FACTORY_API_TOKEN=secrets.token_urlsafe(48),
                      AGENT_FACTORY_API_ACTOR='local-owner', LOKVETIA_MACHINE_KIND='this_pc',
                      LOKVETIA_MACHINE_NAME=socket.gethostname())
    from .local_games import local_games_lock
    # Protect the startup race across double-clicks; the server holds this lock.
    import sqlite3
    try:
        with local_games_lock(workspace / 'desktop-instance', timeout=0.25):
            import uvicorn
            instance, launch_secret = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
            app = desktop_app(workspace, launch_secret=launch_secret, instance=instance)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('127.0.0.1', port))
            sock.listen(128)
            actual_port = sock.getsockname()[1]
            record = workspace / 'desktop-instance.json'
            record.write_text(json.dumps(dict(port=actual_port, instance=instance, secret=launch_secret)), encoding='utf-8')
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=actual_port,
                                    log_config=None, access_log=False, proxy_headers=False))
            app.state.desktop_server = server
            def open_when_ready():
                deadline = time.monotonic() + 60
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.1)
                if server.started:
                    webbrowser.open(f'http://127.0.0.1:{actual_port}/desktop#{launch_secret}')
            if browser:
                threading.Thread(target=open_when_ready, daemon=True).start()
            try:
                server.run(sockets=[sock])
            finally:
                sock.close()
                record.unlink(missing_ok=True)
    except sqlite3.OperationalError as error:
        if 'locked' not in str(error).lower():
            raise
        from urllib.request import urlopen
        data = json.loads((workspace / 'desktop-instance.json').read_text(encoding='utf-8'))
        address = f"http://127.0.0.1:{int(data['port'])}"
        with urlopen(address + '/desktop/status', timeout=5) as response:
            if json.load(response).get('instance') != data['instance']:
                raise RuntimeError('Another application is using the saved Core port')
        if browser:
            webbrowser.open(address + '/desktop#' + data['secret'])
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Lokvetia Core desktop')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--no-shortcut', action='store_true')
    parser.add_argument('--workspace', type=Path, default=default_workspace())
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('Invalid port')
    if args.install:
        if not getattr(sys, 'frozen', False) or not args.install_dir:
            parser.error('--install requires a built executable and --install-dir')
        install(Path(sys.executable), args.install_dir, shortcut=not args.no_shortcut)
        return 0
    if getattr(sys, 'frozen', False) and not args.serve:
        return installation_window()
    return serve(args.workspace, port=args.port, browser=not args.no_browser)


if __name__ == '__main__':
    raise SystemExit(main())
