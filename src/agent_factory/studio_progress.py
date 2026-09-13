"""Owner-scoped projection of durable planning evidence, never an inference."""
import json
from .autonomous_mission import AutonomousMissionService
from .software_roles import AUTONOMOUS_PLANNING_ROLE_IDS
from .studio_supervisor import Supervisor

ROLE_NAMES = {
    'mission_analyst': ('Розбір ідеї', 'Idea analysis'),
    'product_requirements_analyst': ('Вимоги до гри', 'Game requirements'),
    'software_architect': ('Архітектура', 'Architecture'),
    'backlog_planner': ('План задач', 'Task plan'),
    'backlog_reviewer': ('Перевірка плану', 'Plan review'),
}
STATES = {
    'queued': ('У черзі', 'Queued'), 'running': ('Планування триває', 'Planning is running'),
    'stopping': ('Зупиняємо поточний запит', 'Stopping the current request'),
    'cancelled': ('Зупинено', 'Stopped'), 'failed': ('Планування не пройшло перевірку', 'Planning failed validation'),
    'ready': ('План готовий до перегляду', 'Plan ready to review'),
    'interrupted': ('Виконання не підтверджене після перезапуску', 'Execution is unconfirmed after a restart'),
    'idle': ('Збережено. Планування не запущене', 'Saved. Planning has not started'),
}


def game_progress(storage, runner, mission_id, actor, lang='uk', *, details=True):
    mission = AutonomousMissionService(storage).get(mission_id)
    if mission.mission_owner != actor:
        raise KeyError('game_not_found')
    english = lang == 'en'
    supervisor = Supervisor(storage)
    history = supervisor.history(mission.mission_key)
    mandates = supervisor.mandates(mission.mission_key)
    revoked = bool(mandates and mandates[0].revoked_at)
    process = runner.status(mission_id)
    run = storage.db.execute('SELECT * FROM autonomous_planning_pipeline_runs WHERE mission_id=? ORDER BY id DESC LIMIT 1', (mission_id,)).fetchone()
    failure = context = None
    assignments = {}
    invocations, artifacts = [], []
    if run:
        failure = storage.db.execute('SELECT * FROM autonomous_planning_pipeline_failures WHERE run_id=?', (run['id'],)).fetchone()
        manifest = storage.db.execute('SELECT assignments_json FROM autonomous_planning_manifests WHERE id=?',(run['manifest_id'],)).fetchone()
        if manifest: assignments={row['role_id']:row for row in json.loads(manifest['assignments_json'])}
        invocations = list(storage.db.execute('SELECT role_id,attempt_number,valid,created_at FROM autonomous_planning_pipeline_invocations WHERE run_id=? ORDER BY id', (run['id'],)))
        fields='id,role_id,artifact_kind,created_at'+(',content_json' if details else '')
        artifacts = list(storage.db.execute('SELECT '+fields+' FROM autonomous_planning_pipeline_artifacts WHERE run_id=? ORDER BY invocation_order', (run['id'],)))
        context = storage.db.execute('SELECT role_id,invocation_sequence,created_at FROM autonomous_planning_contexts WHERE manifest_id=? ORDER BY id DESC LIMIT 1', (run['manifest_id'],)).fetchone()
    active = process in {'running', 'queued'}
    if revoked:
        state = 'stopping' if active else 'cancelled'
    elif str(mission.phase) == 'WAITING_FOR_BACKLOG_APPROVAL':
        state = 'ready'
    elif failure:
        state = 'failed'
    elif active:
        state = process
    elif process == 'failed' or (history and history[0].outcome in {'waiting','refused','asked','unknown'}):
        state = 'failed'
    elif run or (history and history[0].outcome == 'in_flight') or mandates:
        state = 'interrupted'
    else:
        state = 'idle'
    done = {row['role_id'] for row in artifacts}
    steps = []
    for role in AUTONOMOUS_PLANNING_ROLE_IDS:
        attempts = [row for row in invocations if row['role_id'] == role]
        current = bool(context and context['role_id'] == role and role not in done)
        step_state = 'done' if role in done else 'failed' if failure and failure['role_id'] == role else 'running' if current and active else 'pending'
        steps.append({'role':role, 'name':ROLE_NAMES[role][english], 'state':step_state,
            'attempts':len(attempts), 'active_attempt':len(attempts)+1 if current and active else None,
            'model':assignments.get(role,{}).get('model',mission.configuration.role_models.get(role, mission.configuration.default_model)),
            'provider':assignments.get(role,{}).get('provider_id','ollama' if tuple(mission.configuration.local_provider_ids)==('ollama',) else 'Configured providers')})
    times = [mission.created_at, *(row['created_at'] for row in invocations)]
    if context: times.append(context['created_at'])
    if history: times.extend(step.finished_at or step.started_at for step in history)
    local_only = all(str(step['model']).startswith('local:') and step['provider']=='ollama' for step in steps) and tuple(mission.configuration.local_provider_ids) == ('ollama',)
    result = {'mission_id':mission.id, 'mission_key':mission.mission_key, 'title':mission.name,
        'created_at':mission.created_at, 'updated_at':max(times), 'state':state,
        'summary':STATES[state][english], 'model':', '.join(dict.fromkeys(step['model'] for step in steps)),
        'provider':'Ollama' if local_only else ', '.join(dict.fromkeys(step['provider'] for step in steps)), 'local_only':local_only,
        'paid_limit':0 if local_only else None, 'tokens':None, 'completed_steps':len(done),
        'total_steps':len(steps), 'steps':steps, 'can_stop':active and not revoked,
        'background':active, 'url':'/studio?mission='+mission.mission_key,
        'max_attempts':run['max_attempts_per_role'] if run else 2,
        'last_detail':history[0].summary.text(lang) if history else '',
        'failure_role':ROLE_NAMES.get(failure['role_id'],(failure['role_id'],)*2)[english] if failure else None}
    if details:
        result['idea'] = mission.initial_specification
        result['failure_details'] = [str(error)[:600] for error in json.loads(failure['validation_errors_json'])[:8]] if failure else []
        result['artifacts'] = [{'id':row['id'], 'title':ROLE_NAMES[row['role_id']][english],
                                'content':json.loads(row['content_json'])} for row in artifacts]
    return result


def games(storage, runner, actor, lang='uk', *, offset=0, limit=30):
    rows = storage.db.execute('SELECT id FROM autonomous_missions WHERE mission_owner=? ORDER BY id DESC LIMIT ? OFFSET ?', (actor,limit,offset)).fetchall()
    total = storage.db.execute('SELECT count(*) FROM autonomous_missions WHERE mission_owner=?',(actor,)).fetchone()[0]
    return {'items':[game_progress(storage,runner,row['id'],actor,lang,details=False) for row in rows], 'total':total,'offset':offset,'owner':actor}
