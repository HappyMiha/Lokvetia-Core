"""Resumable local project navigation; no build, planning or spending authority."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
import uuid

from .autonomous_mission import AutonomousMissionConfiguration, AutonomousMissionService
from .config import config_path_for_workspace
from .local_role_qualification import ROLES
from .mission_intake import AutonomousMissionIntakeService
from .models import ProviderCapabilities


class GameConflict(ValueError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def bounded(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError('invalid_text')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError('invalid_text') from None
    if any(ord(c) < 32 and c not in '\r\n\t' for c in value):
        raise ValueError('invalid_text')
    return value


def stamp():
    return datetime.now(timezone.utc).isoformat()


def search(value):
    bounded(value, 200, empty=True)
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


@contextmanager
def local_games_lock(database, *, timeout=10):
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(database) + '.local-games-lock', timeout=timeout)) as lock:
        with lock:
            lock.execute('BEGIN IMMEDIATE')
            yield


class LocalGames:
    def __init__(self, storage, workspace):
        self.storage = storage
        self.db = storage.db
        self.workspace = Path(workspace).resolve()

    def model_choices(self):
        # Catalogue is configuration, never installation or qualification evidence.
        try:
            path = config_path_for_workspace('providers', self.workspace)
            if path.stat().st_size > 262144:
                raise ValueError('provider_catalog_unavailable')
            providers = json.loads(path.read_bytes())['providers']
            if not isinstance(providers, list):
                raise ValueError('provider_catalog_unavailable')
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            raise ValueError('provider_catalog_unavailable') from None
        choices = []
        try:
            for provider in providers:
                if not isinstance(provider, dict):
                    raise ValueError('provider_catalog_unavailable')
                capability = ProviderCapabilities.from_config(provider)
                if not provider.get('enabled', True) or not capability.autonomous_local_eligible:
                    continue
                models = provider.get('model_ids', [])
                namespace = bounded(provider.get('model_namespace', ''), 50)
                provider_id = bounded(provider['id'], 100)
                if not isinstance(models, list):
                    raise ValueError('provider_catalog_unavailable')
                for model in models:
                    full = namespace + ':' + bounded(model, 100)
                    if not any(capability.role_model_error(role, full) for role in ROLES):
                        choices.append({'key': provider_id + '/' + full, 'provider': provider_id,
                                        'model': full, 'qualified': False})
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError('provider_catalog_unavailable') from None
        return choices

    def row(self, ident, actor):
        row = self.db.execute('SELECT * FROM local_game_drafts WHERE id=? AND actor=?', (ident, actor)).fetchone()
        if not row:
            raise KeyError('game_not_found')
        return row

    def _snapshot(self, row):
        return {key: row[key] for key in ('id', 'title', 'idea', 'model_key', 'view_step', 'revision',
                                        'error_code', 'mission_id', 'created_at', 'updated_at')}

    def _record(self, row):
        self.db.execute('INSERT INTO local_game_draft_versions VALUES(?,?,?)',
                        (row['id'], row['revision'], encoded(self._snapshot(row))))

    def _replay(self, actor, command, request):
        try:
            if str(uuid.UUID(command)) != command:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid_command') from None
        digest = hashlib.sha256(encoded(request).encode()).hexdigest()
        old = self.db.execute('SELECT * FROM local_game_commands WHERE actor=? AND command_id=?',
                              (actor, command)).fetchone()
        if old and old['request_digest'] != digest:
            raise GameConflict('command_conflict')
        return digest, old

    def create(self, actor, command):
        bounded(actor, 200)
        with self.db:
            self.storage._begin_immediate()
            digest, old = self._replay(actor, command, ['create'])
            if old:
                ident = old['draft_id']
            else:
                ident = str(uuid.uuid4()); time = stamp()
                self.db.execute('INSERT INTO local_game_drafts VALUES(?,?,?,?,?,?,1,?,NULL,NULL,?,?)',
                                (ident, actor, 'Нова гра', '', '', 0, '', time, time))
                self._record(self.row(ident, actor))
                self.db.execute('INSERT INTO local_game_commands VALUES(?,?,?,?)', (actor, command, digest, ident))
        return self.detail(ident, actor)

    def save(self, ident, actor, command, expected, *, title, idea, model_key, view_step):
        bounded(title, 160); bounded(idea, 6000, empty=True); bounded(model_key, 200, empty=True)
        if type(expected) is not int or expected < 1 or type(view_step) is not int or not 0 <= view_step <= 4:
            raise ValueError('invalid_version_or_step')
        request = ['save', ident, expected, title, idea, model_key, view_step]
        with self.db:
            self.storage._begin_immediate()
            digest, old = self._replay(actor, command, request)
            row = self.row(ident, actor)
            if old:
                return self.detail(ident, actor)
            if row['revision'] != expected:
                raise GameConflict('newer_version')
            changed_source = (title, idea, model_key) != (row['title'], row['idea'], row['model_key'])
            if row['creation_json'] and changed_source:
                raise GameConflict('source_already_submitted')
            error = ''
            if view_step > 0 and not idea.strip():
                error = 'idea_required'; view_step = 0
            elif view_step > 1 and not model_key:
                error = 'model_required'; view_step = 1
            self.db.execute('UPDATE local_game_drafts SET title=?,idea=?,model_key=?,view_step=?,revision=revision+1,'
                            'error_code=?,updated_at=? WHERE id=?', (title, idea, model_key, view_step, error, stamp(), ident))
            self._record(self.row(ident, actor))
            self.db.execute('INSERT INTO local_game_commands VALUES(?,?,?,?)', (actor, command, digest, ident))
        return self.detail(ident, actor)

    def materialize(self, ident, actor, command, expected):
        # Intake performs several durable transactions. A separate SQLite writer
        # lock serializes local submissions across processes without nesting or
        # weakening Core transactions. OS crash recovery releases the lock.
        with local_games_lock(self.storage.path):
            return self._materialize(ident, actor, command, expected)

    def _materialize(self, ident, actor, command, expected):
        """Create a Core DRAFT from a frozen intake. Retries reuse its source command."""
        request = ['materialize', ident, expected]
        with self.db:
            self.storage._begin_immediate()
            digest, old = self._replay(actor, command, request)
            row = self.row(ident, actor)
            if old or row['mission_id'] is not None:
                return self.detail(ident, actor)
            if type(expected) is not int or row['revision'] != expected:
                raise GameConflict('newer_version')
            if not row['creation_json']:
                if not row['idea'].strip():
                    raise ValueError('idea_required')
                choice = next((v for v in self.model_choices() if v['key'] == row['model_key']), None)
                if choice is None:
                    raise ValueError('model_unavailable')
                config = AutonomousMissionConfiguration(repository_path=str(self.workspace),
                    default_model=choice['model'], role_models={role: choice['model'] for role in ROLES},
                    local_provider_ids=(choice['provider'],))
                frozen = {'title': row['title'], 'idea': row['idea'], 'configuration': config.to_dict()}
                self.db.execute('UPDATE local_game_drafts SET creation_json=?,error_code=? WHERE id=?',
                                (encoded(frozen), 'intake_pending', ident))
            else:
                frozen = json.loads(row['creation_json'])
        # Core owns its transactions and durable intake command; never create a
        # second source authority. The frozen draft cannot be edited during recovery.
        result = AutonomousMissionIntakeService(self.storage).create_from_text(
            name=frozen['title'], mission_owner=actor, specification=frozen['idea'], actor=actor,
            command_id='local-start-' + ident, provenance='local-start-original', source_name='idea.txt',
            configuration=AutonomousMissionConfiguration.from_dict(frozen['configuration']))
        with self.db:
            self.storage._begin_immediate()
            row = self.row(ident, actor)
            if row['mission_id'] is None:
                self.db.execute('UPDATE local_game_drafts SET mission_id=?,revision=revision+1,error_code=?,updated_at=? WHERE id=?',
                                (result.mission.id, '', stamp(), ident))
                self._record(self.row(ident, actor))
            digest, old = self._replay(actor, command, request)
            if not old:
                self.db.execute('INSERT INTO local_game_commands VALUES(?,?,?,?)', (actor, command, digest, ident))
        return self.detail(ident, actor)

    def detail(self, ident, actor):
        row = self.row(ident, actor)
        document = self._snapshot(row)
        document['source_locked'] = row['creation_json'] is not None
        document['project'] = self.project(row['mission_id'], actor) if row['mission_id'] else None
        document['latest_working'] = None
        document['play_blocker'] = 'verified_playable_version_unavailable'
        return document

    @staticmethod
    def _progress(counts):
        """Summarize stored work-item states. Accepted work is the honest share.

        A completed run is not acceptance, so the published share counts only
        approved items. Finished and blocked work are reported beside it rather
        than folded into one number.
        """
        total = sum(counts.values())
        accepted = counts.get('approved', 0)
        return {
            'total': total,
            'accepted': accepted,
            'finished': counts.get('completed', 0),
            'in_progress': counts.get('running', 0),
            'waiting': counts.get('pending', 0),
            'blocked': counts.get('failed', 0) + counts.get('rejected', 0),
            'accepted_share': round(100.0 * accepted / total, 1) if total else 0.0,
        }

    @staticmethod
    def _next_action(mission, progress):
        """The one action the creator has to take next, derived from stored state."""
        phase = mission.phase.value
        if phase in ('DRAFT', 'SPECIFICATION_ANALYSIS', 'BACKLOG_GENERATION'):
            return 'prepare_plan'
        if phase == 'WAITING_FOR_BACKLOG_APPROVAL' or mission.active_execution_epoch_id is None:
            return 'approve_plan'
        if progress['blocked']:
            return 'resolve_blocked_work'
        if phase == 'COMPLETED':
            return 'review_result'
        return 'inspect_readiness'

    def project(self, mission_id, actor):
        mission = AutonomousMissionService(self.storage).get(mission_id)
        if mission.mission_owner != actor:
            raise KeyError('game_not_found')
        counts = {r['status']: r['count'] for r in self.db.execute(
            'SELECT status,COUNT(*) count FROM work_items WHERE project_id=? GROUP BY status', (mission.project_id,))}
        progress = self._progress(counts)
        # Execution checkpoints and completed workflow runs do not certify playability.
        return {'id': mission.project_id, 'mission_id': mission.id, 'title': mission.name,
                'phase': mission.phase.value, 'disposition': mission.disposition.value,
                'version': mission.version, 'task_counts': counts, 'latest_working': None,
                'progress': progress,
                'working_version': {'available': False,
                                    'reason': 'verified_playable_version_unavailable'},
                'next_action': self._next_action(mission, progress),
                'updated_at': mission.updated_at}

    def list(self, actor, *, q='', offset=0, limit=20):
        clause = "actor=? AND (title LIKE ? ESCAPE '\\' OR idea LIKE ? ESCAPE '\\')"
        args = (actor, search(q), search(q))
        total = self.db.execute('SELECT COUNT(*) FROM local_game_drafts WHERE ' + clause, args).fetchone()[0]
        rows = self.db.execute('SELECT id,title,view_step,revision,error_code,mission_id,updated_at FROM local_game_drafts WHERE '
                               + clause + ' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return {'items': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def existing(self, actor, *, q='', offset=0, limit=20):
        clause = "mission_owner=? AND name LIKE ? ESCAPE '\\'"
        args = (actor, search(q))
        total = self.db.execute('SELECT COUNT(*) FROM autonomous_missions WHERE ' + clause, args).fetchone()[0]
        rows = self.db.execute('SELECT id FROM autonomous_missions WHERE ' + clause
                               + ' ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return {'items': [self.project(row['id'], actor) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def versions(self, ident, actor, *, q='', offset=0, limit=20):
        self.row(ident, actor)
        clause = "draft_id=? AND document_json LIKE ? ESCAPE '\\'"
        args = (ident, search(q))
        total = self.db.execute('SELECT COUNT(*) FROM local_game_draft_versions WHERE ' + clause, args).fetchone()[0]
        rows = self.db.execute('SELECT document_json FROM local_game_draft_versions WHERE ' + clause
                               + ' ORDER BY revision DESC LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return {'items': [json.loads(row[0]) for row in rows], 'total': total, 'offset': offset, 'limit': limit}
