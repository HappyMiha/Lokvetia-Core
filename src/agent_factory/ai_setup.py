"""Local, user-initiated connection journey for official CLIs and Ollama.

Official CLIs own their login; optional API keys use the OS credential store. Jobs authorize only
installation/login and a synthetic connection check, never project execution.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener
import uuid

from .local_role_qualification import NoRedirect
from .machine_identity import describe_this_machine as this_machine
from .providers import ProcessSupervisor

PROVIDERS = ('codex', 'gemini', 'ollama')
PROMPT = 'Connection check only. Do not use tools or read files. Reply with exactly LOKVETIA_OK.'
ACTIVE = ('queued', 'running', 'login')
TITLES = {'codex': 'Codex CLI', 'gemini': 'Gemini CLI', 'ollama': 'Локальна модель'}
STEPS = {
    'queued': 'Готуємо підключення…', 'detect': 'Знаходимо AI на цьому ПК…',
    'install': 'Встановлюємо офіційний інструмент…',
    'login': 'Завершіть вхід у вікні провайдера. Далі все зробимо автоматично.',
    'check': 'Надсилаємо коротку перевірку підключення…',
    'start': 'Запускаємо локальний AI…', 'download': 'Завантажуємо локальну модель. Це може тривати кілька хвилин…',
    'ready': 'Підключено. AI відповів на перевірку.', 'cancelled': 'Підключення скасовано.',
    'timeout': 'Час очікування минув. Спробуйте ще раз.',
    'missing': 'Автовстановлення недоступне на цьому ПК. Встановіть офіційний інструмент і повторіть.',
    'failed': 'Не вдалося завершити підключення. Спробуйте ще раз.',
    'auth_failed': 'Вхід не завершено або доступ відхилено. Спробуйте увійти ще раз.',
    'quota': 'Провайдер повідомив про ліміт використання. Спробуйте інший AI або повторіть пізніше.',
    'memory': 'Для цієї локальної моделі потрібно щонайменше 8 ГБ оперативної пам’яті та 6 ГБ вільного місця.',
    'config': 'Конфігурацію змінено. Оновіть сторінку та повторіть підключення.',
    'key': 'Google не дозволив цей спосіб входу. Додайте Gemini API key — перевіримо його автоматично.',
}
LINKS = {'codex': 'https://learn.chatgpt.com/docs/cli',
         'gemini': 'https://geminicli.com/docs/get-started/installation/',
         'ollama': 'https://ollama.com/download/windows'}
LOGIN_HOSTS = {'codex': {'auth.openai.com', 'auth0.openai.com', 'chatgpt.com'},
               'gemini': {'accounts.google.com'}}


class SetupError(ValueError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def official_login_url(provider, output):
    """Only an official login URL from a running login process reaches the UI."""
    for candidate in re.findall(r'https://[^\s<>"\x1b]+', output):
        candidate = candidate.rstrip(').,')
        try:
            parsed = urlsplit(candidate)
            if (len(candidate) < 4096 and parsed.hostname in LOGIN_HOSTS.get(provider, ())
                    and parsed.port in {None, 443} and not parsed.username and not parsed.password
                    and not parsed.fragment and not re.search(r'(?:access_token|id_token|refresh_token)=', parsed.query)):
                return candidate
        except ValueError:
            pass
    return None


def environment():
    # Provider-owned cached login remains in its normal home; unrelated secrets
    # and alternate API endpoints are not passed into connection checks.
    kept = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
            'USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA',
            'PROGRAMFILES', 'PROGRAMFILES(X86)', 'CODEX_HOME', 'SSL_CERT_FILE',
            'CODEX_CA_CERTIFICATE', 'NODE_EXTRA_CA_CERTS', 'LANG', 'LC_ALL'}
    return {key: value for key, value in os.environ.items() if key.upper() in kept} | {
        'NO_COLOR': '1', 'TERM': 'dumb', 'OLLAMA_HOST': 'http://127.0.0.1:11434'}


def program(name, candidates=()):
    for raw in (*candidates, name):
        candidate = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(str(candidate))
        if found and Path(found).suffix.lower() not in {'.cmd', '.bat', '.ps1'}:
            return found
    return None


def launcher(provider, workspace):
    """Use fixed native/official Node entrypoints; never execute a shell shim."""
    node = program('node', ('%ProgramFiles%/nodejs/node.exe', '%LOCALAPPDATA%/Programs/nodejs/node.exe'))
    prefixes = [workspace / '.agent-factory' / 'ai-tools',
                Path(os.path.expandvars('%APPDATA%')) / 'npm']
    if provider in {'codex', 'gemini'} and node:
        package = '@openai/codex' if provider == 'codex' else '@google/gemini-cli'
        for prefix in prefixes:
            root = prefix / 'node_modules' / package
            metadata = root / 'package.json'
            if metadata.is_file() and metadata.stat().st_size < 65536:
                try:
                    data = json.loads(metadata.read_text(encoding='utf-8'))
                    entry = data['bin'][provider]
                    target = (root / entry).resolve()
                    if data['name'] == package and target.is_relative_to(root.resolve()) and target.is_file():
                        return [node, str(target)]
                except (ValueError, KeyError, TypeError):
                    pass
    candidates = {
        'codex': ('%USERPROFILE%/.codex/plugins/.plugin-appserver/codex.exe',
                  '%USERPROFILE%/.codex/.sandbox-bin/codex.exe'),
        'gemini': (),
        'ollama': ('%LOCALAPPDATA%/Programs/Ollama/ollama.exe',),
    }
    native = program(provider, candidates[provider])
    return [native] if native else None


def local_api(path, payload=None, timeout=3):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request('http://127.0.0.1:11434' + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(131073)
    if len(raw) > 131072:
        raise SetupError('failed')
    return json.loads(raw)


def connected_environment(workspace, actor):
    """Called only after the existing provider execution approval, for its owner."""
    from .os_credentials import WindowsCredentialStore
    database = Path(workspace).resolve() / '.agent-factory/ai-setup.db'
    if not database.is_file():
        return {}
    with closing(sqlite3.connect(database)) as db:
        row = db.execute('SELECT s.reference FROM setup_secrets s JOIN setup_connections c ON s.actor=c.actor AND s.provider=c.provider WHERE s.actor=? AND s.provider=?', (actor,'gemini')).fetchone()
    if not row:
        return {}
    store = WindowsCredentialStore(hashlib.sha256(str(database).encode()).hexdigest())
    return {'GEMINI_API_KEY':store.get(row[0]),
            'GEMINI_CLI_SYSTEM_SETTINGS_PATH':str(Path(__file__).parent/'defaults/gemini-key-settings.json')}


class AISetup:
    def __init__(self, workspace, database, *, store=None):
        self.workspace = Path(workspace).resolve()
        self.database = Path(database).resolve()
        self.path = self.workspace / '.agent-factory' / 'ai-setup.db'
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ai-setup')
        self.stopping = threading.Event()
        self.urls = {}  # Short-lived login URLs are never persisted or logged.
        self.supervisor = ProcessSupervisor()
        from .os_credentials import WindowsCredentialStore
        self.store = store or WindowsCredentialStore(hashlib.sha256(str(self.path).encode()).hexdigest())

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE IF NOT EXISTS setup_jobs(id TEXT PRIMARY KEY, actor TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL, step TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL, model TEXT NOT NULL DEFAULT "")')
            db.execute('CREATE TABLE IF NOT EXISTS setup_connections(actor TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, checked TEXT NOT NULL, PRIMARY KEY(actor,provider))')
            db.execute('CREATE TABLE IF NOT EXISTS setup_audit(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS setup_secrets(actor TEXT NOT NULL, provider TEXT NOT NULL, reference TEXT NOT NULL, PRIMARY KEY(actor,provider))')
            db.execute('BEGIN IMMEDIATE')
            with db:
                db.execute("INSERT INTO setup_audit(job_id,actor,event,at) SELECT id,actor,'timeout',? FROM setup_jobs WHERE status IN ('queued','running','login') AND deadline<?", (stamp(), time.time()))
                db.execute("UPDATE setup_jobs SET status='failed',step='timeout' WHERE status IN ('queued','running','login') AND deadline<?", (time.time(),))
                yield db

    def scan(self, actor):
        local = this_machine().is_the_users_computer
        if not local:
            return {'local': False, 'providers': [], 'download_url': '/downloads'}
        with self.db() as db:
            connections = {row['provider']: dict(row) for row in db.execute('SELECT * FROM setup_connections WHERE actor=?', (actor,))}
            jobs = [dict(row) for row in db.execute('SELECT * FROM setup_jobs WHERE actor=? ORDER BY created DESC', (actor,))]
        providers = []
        for provider in PROVIDERS:
            installed = bool(launcher(provider, self.workspace))
            current = next((row for row in jobs if row['provider'] == provider), None)
            providers.append({'id': provider, 'title': TITLES[provider], 'installed': installed,
                'connection': connections.get(provider) if installed else None,
                'job': self.public(current) if current else None, 'install_url': LINKS[provider]})
        return {'local': True, 'providers': providers}

    def public(self, row):
        return {key: row[key] for key in ('id', 'provider', 'status', 'step', 'model')} | {
            'message': STEPS.get(row['step'], STEPS['failed']),
            'login_url': self.urls.get(row['id']) if row['status'] == 'login' else None}

    def get(self, ident, actor):
        with self.db() as db:
            row = db.execute('SELECT * FROM setup_jobs WHERE id=? AND actor=?', (ident, actor)).fetchone()
            if not row:
                raise KeyError(ident)
            return self.public(row)

    def update(self, ident, *, status='running', step, model=''):
        with self.db() as db:
            changed = db.execute("UPDATE setup_jobs SET status=?,step=?,model=? WHERE id=? AND status IN ('queued','running','login')", (status, step, model, ident)).rowcount
            if changed:
                db.execute('INSERT INTO setup_audit(job_id,actor,event,at) SELECT id,actor,?,? FROM setup_jobs WHERE id=?', (step, stamp(), ident))
        if not changed:
            raise SetupError('cancelled')

    def start(self, provider, actor, command_id, *, authorize=lambda: True, secret=None):
        if provider not in PROVIDERS or not re.fullmatch('[a-f0-9-]{36}', command_id):
            raise ValueError('invalid_request')
        if not this_machine().is_the_users_computer or not authorize():
            raise PermissionError('local_owner_required')
        if secret is not None and (provider != 'gemini' or not isinstance(secret,str)
                or not 12 <= len(secret) <= 2048 or not secret.isprintable()):
            raise ValueError('invalid_secret')
        old = None
        with self.db() as db:
            existing = db.execute('SELECT * FROM setup_jobs WHERE id=?', (command_id,)).fetchone()
            if existing:
                if existing['actor'] != actor or existing['provider'] != provider:
                    raise ValueError('command_conflict')
                return self.public(existing)
            active = db.execute("SELECT * FROM setup_jobs WHERE status IN ('queued','running','login')").fetchone()
            if active:
                raise ValueError('setup_busy')
            if secret is not None:
                db.execute('DELETE FROM setup_connections WHERE actor=? AND provider=?',(actor,provider))
                reference = uuid.uuid4().hex
                self.store.put(reference, secret)
                old = db.execute('SELECT reference FROM setup_secrets WHERE actor=? AND provider=?',(actor,provider)).fetchone()
                db.execute('INSERT INTO setup_secrets VALUES(?,?,?) ON CONFLICT(actor,provider) DO UPDATE SET reference=excluded.reference',(actor,provider,reference))
            db.execute("INSERT INTO setup_jobs VALUES(?,?,?,'queued','queued',?,?, '')", (command_id, actor, provider, time.time(), time.time() + 900))
        if old:
            try:
                self.store.delete(old['reference'])
            except Exception:
                pass  # New reference is durable; never undo it on old-key cleanup failure.
        self.executor.submit(self.run, command_id, provider, actor, authorize)
        return self.get(command_id, actor)

    def cancel(self, ident, actor):
        self.get(ident, actor)
        with self.db() as db:
            db.execute("UPDATE setup_jobs SET status='cancelled',step='cancelled' WHERE id=? AND actor=? AND status IN ('queued','running','login')", (ident, actor))
            db.execute('INSERT INTO setup_audit(job_id,actor,event,at) VALUES(?,?,?,?)', (ident, actor, 'cancelled', stamp()))
        self.urls.pop(ident, None)
        return self.get(ident, actor)

    def alive(self, ident, actor, authorize):
        if self.stopping.is_set() or not authorize() or self.get(ident, actor)['status'] not in ACTIVE:
            raise SetupError('cancelled')

    def process(self, command, *, cwd, env, seconds, check, on_output=lambda output, proc: None, interactive=False, progress=False):
        """Bound both pipes; react to an official login prompt without a terminal."""
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        proc = subprocess.Popen(command, cwd=cwd, env=env, shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', **flags)
        if not interactive:
            proc.stdin.close()
        chunks, guard, overflow = [], threading.Lock(), threading.Event()
        def drain(stream):
            while char := stream.read(1):
                with guard:
                    if len(chunks) >= 65536:
                        if progress:
                            del chunks[:4096]
                            chunks.append(char)
                        else:
                            overflow.set()
                    else:
                        chunks.append(char)
        readers = [threading.Thread(target=drain, args=(stream,), daemon=True) for stream in (proc.stdout, proc.stderr)]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + seconds
        try:
            while proc.poll() is None:
                check()
                if overflow.is_set():
                    raise SetupError('failed')
                if time.monotonic() >= deadline:
                    raise SetupError('timeout')
                with guard:
                    output = ''.join(chunks)
                on_output(output, proc)
                time.sleep(.15)
            for reader in readers:
                reader.join(2)
            check()
            if overflow.is_set():
                raise SetupError('failed')
            return proc.returncode, ''.join(chunks)
        finally:
            if proc.poll() is None:
                self.supervisor.terminate_tree(proc)
            proc.wait(timeout=10)
            for reader in readers:
                reader.join(2)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

    def install(self, provider, ident, invoke):
        self.update(ident, step='install')
        winget = program('winget', ('%LOCALAPPDATA%/Microsoft/WindowsApps/winget.exe',))
        if provider == 'ollama' or not program('node', ('%ProgramFiles%/nodejs/node.exe',)):
            if not winget:
                raise SetupError('missing')
            package = 'Ollama.Ollama' if provider == 'ollama' else 'OpenJS.NodeJS.LTS'
            code, _ = invoke([winget, 'install', '--exact', '--id', package, '--silent',
                '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'], seconds=600, progress=True)
            if code:
                raise SetupError('missing')
        if provider != 'ollama':
            node = program('node', ('%ProgramFiles%/nodejs/node.exe',))
            npm = Path(node).parent / 'node_modules/npm/bin/npm-cli.js' if node else None
            if not npm or not npm.is_file():
                raise SetupError('missing')
            package = '@openai/codex@0.153.4' if provider == 'codex' else '@google/gemini-cli@0.53.1'
            code, _ = invoke([node, str(npm), 'install', '--prefix', str(self.workspace / '.agent-factory/ai-tools'),
                '--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org', package], seconds=600, progress=True)
            if code:
                raise SetupError('missing')

    def run(self, ident, provider, actor, authorize):
        self.urls.pop(ident, None)
        try:
            check = lambda: self.alive(ident, actor, authorize)
            check()
            folder = self.workspace / '.agent-factory' / 'ai-checks' / ident
            folder.mkdir(parents=True)
            env = environment()
            invoke = lambda command, seconds=180, on_output=lambda output, proc: None, interactive=False, progress=False: self.process(
                command, cwd=folder, env=env, seconds=seconds, check=check, on_output=on_output, interactive=interactive, progress=progress)
            self.update(ident, step='detect')
            command = launcher(provider, self.workspace)
            if not command:
                self.install(provider, ident, invoke)
                command = launcher(provider, self.workspace)
            if not command:
                raise SetupError('missing')
            model = ''
            if provider == 'ollama':
                model = self.local(ident, command, invoke, check)
            elif provider == 'codex':
                code, _ = invoke(command + ['login', 'status'], seconds=15)
                if code:
                    self.update(ident, status='login', step='login')
                    def login_output(output, proc):
                        url = official_login_url(provider, output)
                        if url:
                            self.urls[ident] = url
                    code, _ = invoke(command + ['login'], seconds=330, on_output=login_output)
                    if code:
                        raise SetupError('auth_failed')
                self.urls.pop(ident, None)
                self.update(ident, step='check')
                target = folder / 'answer.txt'
                model = 'gpt-5.6-sol'
                code, output = invoke(command + ['exec', '--ignore-user-config', '--ephemeral',
                    '--skip-git-repo-check', '--sandbox', 'read-only', '--color', 'never',
                    '-c', 'model_reasoning_effort="low"', '--model', model,
                    '--output-last-message', str(target), PROMPT])
                answer = target.read_text(encoding='utf-8') if target.is_file() and target.stat().st_size < 4096 else ''
                self.checked(code, answer, output)
            else:
                # Keep provider-owned auth, but disable tools/hooks/extensions for
                # the synthetic check. User config files are never overwritten.
                settings = folder / 'gemini-settings.json'
                settings.write_text(json.dumps({
                    'tools': {'core': []}, 'hooksConfig': {'enabled': False}, 'mcpServers': {},
                    'telemetry': {'enabled': False}}), encoding='utf-8')
                policy = folder / 'deny-tools.toml'
                policy.write_text('[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n', encoding='utf-8')
                env['GEMINI_CLI_SYSTEM_SETTINGS_PATH'] = str(settings)
                with self.db() as db:
                    stored = db.execute('SELECT reference FROM setup_secrets WHERE actor=? AND provider=?', (actor,provider)).fetchone()
                if stored:
                    env['GEMINI_API_KEY'] = self.store.get(stored['reference'])
                elif os.environ.get('GEMINI_API_KEY'):
                    env['GEMINI_API_KEY'] = os.environ['GEMINI_API_KEY']
                else:
                    env['GOOGLE_GENAI_USE_GCA'] = 'true'
                if env.get('GEMINI_API_KEY'):
                    config = json.loads(settings.read_text())
                    config['security'] = {'auth': {'selectedType': 'gemini-api-key', 'enforcedType': 'gemini-api-key'}}
                    settings.write_text(json.dumps(config), encoding='utf-8')
                self.update(ident, step='check')
                arguments = ['--prompt', PROMPT, '--output-format', 'json', '--skip-trust',
                    '--approval-mode', 'plan', '--extensions', 'none',
                    '--allowed-mcp-server-names=__lokvetia_no_server__', '--admin-policy', str(policy)]
                code, output = invoke(command + arguments, seconds=180)
                if code and 'GEMINI_API_KEY' not in env:
                    if 'UNSUPPORTED_CLIENT' in output or 'IneligibleTierError' in output:
                        raise SetupError('key')
                    self.login_gemini(ident, command, policy, invoke)
                    self.update(ident, step='check')
                    code, output = invoke(command + arguments, seconds=180)
                # CLI may print login progress before its final JSON envelope.
                answer = ''
                for match in re.finditer(r'\{', output):
                    try:
                        result, _ = json.JSONDecoder().raw_decode(output[match.start():])
                        if isinstance(result, dict) and isinstance(result.get('response'), str):
                            answer = result['response']
                    except ValueError:
                        continue
                self.checked(code, answer, output)
                model = 'gemini-cli-default'
            check()
            self.record(provider, actor, model, command, ident=ident, authorize=authorize)
        except Exception as error:
            reason = str(error) if isinstance(error, SetupError) and str(error) in STEPS else 'failed'
            try:
                self.update(ident, status='cancelled' if reason == 'cancelled' else 'failed', step=reason)
            except SetupError:
                pass
        finally:
            self.urls.pop(ident, None)

    def login_gemini(self, ident, command, policy, invoke):
        """Official ACP login only: no sessions, prompts or tool calls."""
        self.update(ident, status='login', step='login')
        sent = set()
        complete = False
        def output_changed(output, proc):
            nonlocal complete
            def send(identification, method, params):
                if identification not in sent:
                    proc.stdin.write(json.dumps({'jsonrpc':'2.0','id':identification,'method':method,'params':params})+'\n')
                    proc.stdin.flush(); sent.add(identification)
            send(1,'initialize',{'protocolVersion':1,'clientCapabilities':{}})
            url = official_login_url('gemini', output)
            if url:
                self.urls[ident] = url
            for line in output.splitlines():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message,dict):
                    continue
                if message.get('id') == 1 and 'result' in message:
                    send(2,'authenticate',{'methodId':'oauth-personal'})
                if message.get('id') == 2:
                    complete = 'result' in message
                    if not complete:
                        raise SetupError('key' if 'UNSUPPORTED_CLIENT' in json.dumps(message) else 'auth_failed')
                    self.supervisor.terminate_tree(proc)
        invoke(command + ['--acp','--skip-trust','--extensions','none',
            '--allowed-mcp-server-names=__lokvetia_no_server__','--admin-policy',str(policy)],
            seconds=330,on_output=output_changed,interactive=True)
        self.urls.pop(ident,None)
        if not complete:
            raise SetupError('auth_failed')

    @staticmethod
    def checked(code, answer, output):
        if code == 0 and answer.strip() == 'LOKVETIA_OK':
            return
        if re.search(r'quota|rate.limit|usage.limit|exhausted|429', output, re.I):
            raise SetupError('quota')
        if re.search(r'auth|log.?in|sign.?in|401|403', output, re.I):
            raise SetupError('auth_failed')
        raise SetupError('failed')

    def local(self, ident, command, invoke, check):
        try:
            inventory = local_api('/api/tags')
        except Exception:
            self.update(ident, step='start')
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
            # Start only our missing local daemon; never stop an existing daemon.
            subprocess.Popen(command + ['serve'], env=environment(), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
            inventory = None
            for _ in range(20):
                check()
                time.sleep(.5)
                try:
                    inventory = local_api('/api/tags')
                    break
                except Exception:
                    pass
            if inventory is None:
                raise SetupError('failed')
        models = inventory.get('models', [])
        model = next((row['name'] for name in ('qwen2.5-coder:7b', 'qwen2.5-coder:14b') for row in models if row.get('name') == name), None)
        if model is None:
            from .hardware_inventory import collect_inventory
            hardware = collect_inventory(self.workspace)
            memory = hardware['memory'].get('total_bytes') or 0
            disk = hardware['disk'].get('free_bytes') or 0
            if memory < 8 * 1024**3 or disk < 6 * 1024**3:
                raise SetupError('memory')
            model = 'qwen2.5-coder:7b'
            self.update(ident, step='download', model=model)
            code, _ = invoke(command + ['pull', model], seconds=720, progress=True)
            if code:
                raise SetupError('failed')
        self.update(ident, step='check', model=model)
        check()
        result = local_api('/api/generate', {'model': model, 'prompt': PROMPT, 'stream': False,
            'keep_alive': 0, 'options': {'temperature': 0, 'num_predict': 32, 'num_ctx': 2048}}, timeout=120)
        if result.get('model') != model or result.get('done') is not True:
            raise SetupError('failed')
        self.checked(0, result.get('response', ''), '')
        return model

    def record(self, provider, actor, model, command, *, ident, authorize):
        """Connection evidence is separate from role/job approval and spending."""
        from .local_games import local_games_lock
        from .localisation import Message
        from .storage import SQLiteStorage
        from .studio_first_run import FirstRun
        # Serialize cancellation against publication of the verified connection.
        with self.db() as db:
            row = db.execute('SELECT status FROM setup_jobs WHERE id=? AND actor=?',(ident,actor)).fetchone()
            if not row or row['status'] not in ACTIVE or not authorize():
                raise SetupError('cancelled')
            with local_games_lock(self.database):
                with closing(SQLiteStorage(self.database)) as storage:
                    FirstRun(storage).connect(provider, kind='local_model' if provider == 'ollama' else 'own_subscription',
                        name=model or TITLES[provider], state='verified', connected_by=actor,
                        machine_key='local-' + socket.gethostname().lower(),
                        detail=Message('AI відповів на коротку перевірку підключення. Готовність до конкретної роботи перевіряється окремо.',
                                       'AI answered a short connection check. Qualification for a particular job is separate.'))
            db.execute('INSERT INTO setup_connections VALUES(?,?,?,?) ON CONFLICT(actor,provider) DO UPDATE SET model=excluded.model,checked=excluded.checked', (actor, provider, model, stamp()))
            db.execute("UPDATE setup_jobs SET status='ready',step='ready',model=? WHERE id=?",(model,ident))
            db.execute('INSERT INTO setup_audit(job_id,actor,event,at) VALUES(?,?,?,?)',(ident,actor,'ready',stamp()))

    def disconnect(self, provider, actor):
        if provider not in PROVIDERS:
            raise ValueError('invalid_provider')
        with self.db() as db:
            db.execute("UPDATE setup_jobs SET status='cancelled',step='cancelled' WHERE provider=? AND actor=? AND status IN ('queued','running','login')",(provider,actor))
            removed=db.execute('DELETE FROM setup_connections WHERE provider=? AND actor=?',(provider,actor)).rowcount
            remaining=db.execute('SELECT 1 FROM setup_connections WHERE provider=?',(provider,)).fetchone()
            row=db.execute('SELECT reference FROM setup_secrets WHERE provider=? AND actor=?',(provider,actor)).fetchone()
            if row:
                db.execute('DELETE FROM setup_secrets WHERE provider=? AND actor=?',(provider,actor))
            db.execute('INSERT INTO setup_audit(job_id,actor,event,at) VALUES(?,?,?,?)',(provider,actor,'disconnected',stamp()))
        # Revoke access first, even when OS key cleanup fails.
        if row:
            try:
                self.store.delete(row['reference'])
            except Exception:
                pass
        from .storage import SQLiteStorage
        from .studio_first_run import FirstRun
        from .local_games import local_games_lock
        if removed and not remaining:
            with local_games_lock(self.database), closing(SQLiteStorage(self.database)) as storage:
                try:
                    FirstRun(storage).verify(provider, works=False)
                except KeyError:
                    pass
        return {'disconnected':True}

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
