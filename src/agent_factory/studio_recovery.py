"""Explicit, owner-bound local planning recovery; navigation never calls this."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

from .autonomous_mission import AutonomousMissionService
from .local_games import local_games_lock
from .storage import SQLiteStorage
from .studio_start import checked_local_source
from .studio_supervisor import Supervisor


def checkpoint(storage, mission):
    values = [mission.version,str(mission.phase),str(mission.disposition)]
    for table, column, key in [('autonomous_planning_pipeline_runs','mission_id',mission.id),
                               ('studio_supervisor_steps','mission',mission.mission_key),
                               ('studio_mandates','mission',mission.mission_key)]:
        values.append(storage.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {table} WHERE {column}=?',(key,)).fetchone()[0])
    mandate=Supervisor(storage).mandates(mission.mission_key)
    values.append(mandate[0].revoked_at if mandate else '')
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def recover(database, workspace, runner, *, mission_id, actor, command_id, expected_checkpoint):
    # Same order as execution. An old worker in another process must be gone
    # before its ambiguous step may be acknowledged or its outputs reused.
    key = None
    with closing(SQLiteStorage(database)) as storage:
        mission = AutonomousMissionService(storage).get(mission_id)
        if mission.mission_owner != actor:
            raise KeyError('game_not_found')
        replay = storage.db.execute('SELECT mission_id,checkpoint FROM studio_planning_recoveries WHERE actor=? AND command_id=?',(actor,str(command_id))).fetchone()
        if replay:
            if replay['mission_id'] != mission_id or replay['checkpoint'] != expected_checkpoint:
                raise ValueError('recovery_changed')
            return {'accepted':True,'replayed':True,'mission_key':mission.mission_key}
        key = hashlib.sha256(mission.mission_key.encode()).hexdigest()[:24]
    try:
        with local_games_lock(str(database)+'.studio-inference',timeout=.05), \
             local_games_lock(str(database)+'.studio-'+key,timeout=.05), \
             local_games_lock(database), closing(SQLiteStorage(database)) as storage:
            mission = AutonomousMissionService(storage).get(mission_id)
            replay = storage.db.execute('SELECT * FROM studio_planning_recoveries WHERE actor=? AND command_id=?',(actor,str(command_id))).fetchone()
            if replay:
                if replay['mission_id'] != mission_id or replay['checkpoint'] != expected_checkpoint:
                    raise ValueError('recovery_changed')
                return {'accepted':True,'replayed':True,'mission_key':mission.mission_key}
            if runner.status(mission_id) in {'queued','running'}:
                raise ValueError('recovery_busy')
            if checkpoint(storage,mission) != expected_checkpoint:
                raise ValueError('recovery_changed')
            if str(mission.phase) not in {'DRAFT','SPECIFICATION_ANALYSIS','BACKLOG_GENERATION'} or str(mission.disposition) != 'RUNNING':
                raise ValueError('recovery_phase')
            try:
                source = checked_local_source(storage,workspace)
            except (KeyError,ValueError,OSError) as error:
                raise ValueError('local_studio_not_ready') from error
            config = mission.configuration
            if tuple(config.local_provider_ids) != ('ollama',) or any(model != 'local:'+source.name for model in [config.default_model,*config.role_models.values()]):
                raise ValueError('recovery_model_changed')
            supervisor = Supervisor(storage)
            supervisor.reconcile(mission.mission_key)
            history = supervisor.history(mission.mission_key)
            latest = storage.db.execute('SELECT id,manifest_id FROM autonomous_planning_pipeline_runs WHERE mission_id=? ORDER BY id DESC LIMIT 1',(mission_id,)).fetchone()
            if latest:
                from .autonomous_planning import AutonomousPlanningService
                if AutonomousPlanningService(storage).get_manifest(latest['manifest_id']).stale:
                    raise ValueError('recovery_source_changed')
            now = datetime.now(timezone.utc).replace(microsecond=0)
            # Mandate and recovery receipt are committed together, including the
            # exact unknown step the owner acknowledged. Old evidence is retained.
            with storage.db:
                storage._begin_immediate()
                storage.db.execute("UPDATE studio_mandates SET revoked_at=?,revoked_by=? WHERE mission=? AND revoked_at=''",(now.isoformat(),actor,mission.mission_key))
                mandate = storage.db.execute("INSERT INTO studio_mandates(mission,granted_by,steps_json,ceiling,unit,reason,granted_at,expires_at) VALUES(?,?,'[\"plan\"]',0,'USD','Owner requested bounded local planning recovery',?,?)",(mission.mission_key,actor,now.isoformat(),(now+timedelta(hours=24)).isoformat())).lastrowid
                receipt = storage.db.execute('INSERT INTO studio_planning_recoveries(mission_id,actor,command_id,checkpoint,source_run_id,through_step_id,mandate_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (mission_id,actor,str(command_id),expected_checkpoint,latest['id'] if latest else None,history[0].identifier if history else 0,mandate,now.isoformat())).lastrowid
                storage._event('studio.planning_recovery_requested','autonomous_mission',mission_id,{'recovery_id':receipt,'actor':actor,'source_run_id':latest['id'] if latest else None,'paid_limit':0})
            runner.submit(mission_id)
            return {'accepted':True,'replayed':False,'mission_key':mission.mission_key}
    except sqlite3.OperationalError as error:
        if 'locked' in str(error).lower() or 'busy' in str(error).lower():
            raise ValueError('recovery_busy') from error
        raise
