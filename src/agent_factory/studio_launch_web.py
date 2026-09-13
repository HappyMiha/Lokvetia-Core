"""Create and start a local studio mission in one owner-authenticated request."""
from contextlib import closing
from uuid import UUID

from fastapi import HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .autonomous_mission import AutonomousMissionService
from .storage import SQLiteStorage
from .studio_start import create_local_game, checked_local_source
from .localisation import Message


class CreateStudioGame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    title: str = Field(min_length=1, max_length=160)
    idea: str = Field(min_length=1, max_length=6000)
    confirmed: StrictBool


def install_routes(app, database, workspace, runner):
    def owner(request):
        principal = request.state.local_principal
        if principal is None or not {"write", "control"} <= principal.scopes:
            raise HTTPException(403, "studio_access_denied")
        if not ({"local", "*"} & principal.tenants):
            raise HTTPException(403, "studio_access_denied")
        return principal.actor

    @app.get("/api/studio/local-readiness")
    def readiness(lang: str = "uk"):
        try:
            with closing(SQLiteStorage(database)) as storage:
                source = checked_local_source(storage, workspace)
            ready = True
        except (KeyError, ValueError, OSError):
            ready = False
        summary = (Message("Локальна модель перевірена. Можна починати планування.",
                           "The local model is qualified. Planning can start.") if ready else
                   Message("Спочатку перевірте локальний воркер і модель Ollama.",
                           "Qualify the local worker and Ollama model first."))
        return {"can_start": ready, "summary": summary.text(lang),
                "model": source.name if ready else None, "provider":"Ollama", "paid_limit":0}

    @app.get('/api/studio/games')
    def library(request: Request, lang: str='uk', offset: int=Query(0,ge=0), limit: int=Query(30,ge=1,le=100)):
        from .studio_progress import games
        actor = owner(request)
        with closing(SQLiteStorage(database)) as storage:
            return games(storage,runner,actor,lang,offset=offset,limit=limit)

    @app.get('/api/studio/games/{mission_id}')
    def detail(mission_id: int, request: Request, lang: str='uk'):
        from .studio_progress import game_progress
        actor = owner(request)
        try:
            with closing(SQLiteStorage(database)) as storage:
                return game_progress(storage,runner,mission_id,actor,lang)
        except KeyError:
            raise HTTPException(404,'game_not_found') from None

    @app.get('/api/studio/progress/{mission_key}')
    def progress(mission_key: str, request: Request, lang: str='uk'):
        from .studio_progress import game_progress
        actor=owner(request)
        with closing(SQLiteStorage(database)) as storage:
            row=storage.db.execute('SELECT id FROM autonomous_missions WHERE mission_key=? AND mission_owner=?',(mission_key,actor)).fetchone()
            if not row: raise HTTPException(404,'game_not_found')
            return game_progress(storage,runner,row['id'],actor,lang)

    @app.post('/api/studio/games/{mission_id}/stop')
    def stop(mission_id: int, request: Request):
        from .studio_supervisor import Supervisor
        actor = owner(request)
        if request.headers.get('X-Agent-Factory-Confirm') != 'true':
            raise HTTPException(400,'confirmation_required')
        with closing(SQLiteStorage(database)) as storage:
            try:
                mission = AutonomousMissionService(storage).get(mission_id)
                if mission.mission_owner != actor: raise KeyError()
            except KeyError:
                raise HTTPException(404,'game_not_found') from None
            supervisor=Supervisor(storage)
            if supervisor.mandate(mission.mission_key):
                supervisor.revoke(mission.mission_key,actor=actor)
        runner.cancel(mission_id)
        return {'status':runner.status(mission_id)}

    @app.post("/api/studio/create", status_code=202)
    def create(request: Request, command: CreateStudioGame):
        actor = owner(request)
        if command.confirmed is not True or request.headers.get("X-Agent-Factory-Confirm") != "true":
            raise HTTPException(400, "confirmation_required")
        try:
            return create_local_game(database, workspace, actor=actor,
                                     command_id=command.command_id, title=command.title,
                                     idea=command.idea, runner=runner)
        except (KeyError, ValueError, OSError) as error:
            raise HTTPException(409, "local_studio_not_ready") from error

    @app.get("/api/studio/jobs/{mission_id}")
    def status(mission_id: int, request: Request):
        principal = request.state.local_principal
        if principal is None:
            raise HTTPException(403, "studio_access_denied")
        with closing(SQLiteStorage(database)) as storage:
            mission = AutonomousMissionService(storage).get(mission_id)
            if mission.mission_owner != principal.actor:
                raise HTTPException(404, "game_not_found")
        return {"status": runner.status(mission_id)}
