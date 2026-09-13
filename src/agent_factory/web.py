"""Loopback-only FastAPI host for the Local Control Center."""

import sqlite3
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .config import config_path_for_workspace
from .local_games import LocalGames, GameConflict, local_games_lock
from .environment_readiness import EnvironmentReadiness, EnvironmentNotReady
from .http_auth import COOKIE, LocalAccess, LocalHTTPBoundary
from .sso import SsoAccess, access_for_workspace, install_routes as install_sso_routes
from .credential_web import install_routes as install_credential_routes
from .hardware_web import install_routes as install_hardware_routes
from .game_planning_web import install_routes as install_game_planning_routes
from .configuration_advice_web import install_routes as install_configuration_advice_routes
from .installation_web import install_routes as install_installation_routes

from .application import (
    AgentFactoryService,
    AgentView,
    ApprovalView,
    ArtifactView,
    AuditEventView,
    BacklogFileImportResult,
    EventView,
    FounderDecisionPacket,
    FounderDecisionReceipt,
    OperationalStateView,
    ProjectView,
    ProviderView,
    ReviewView,
    RuntimeSettingView,
    RunView,
    SettingsView,
    WorkItemView,
)
from .storage import MIGRATIONS, SQLiteStorage
from .control_plane import HumanControlPlaneService
from .backlog import proposal_from_document
from .backlog_analyzer import analyze_specification
from .orchestration.temporal.client import (
    connect_temporal,
    signal_workflow,
    start_job_workflow,
    workflow_snapshot,
)
from .orchestration.temporal.settings import TemporalSettings

class GameCreateCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str = Field(min_length=36, max_length=36)


class GameSaveCommand(GameCreateCommand):
    expected_revision: StrictInt = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    idea: str = Field(max_length=6000)
    model_key: str = Field(max_length=200)
    view_step: StrictInt = Field(ge=0, le=4)


class GameSubmitCommand(GameCreateCommand):
    expected_revision: StrictInt = Field(ge=1)
    confirmed: StrictBool = False


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    offset: int
    limit: int
    total: int


class HealthResponse(BaseModel):
    status: str
    database: str
    integrity: dict[str, Any]


class IntegrationStatus(BaseModel):
    name: str
    status: str
    detail: str


class DashboardCounts(BaseModel):
    ready: int
    active: int
    blocked: int
    failed: int
    awaiting_review: int
    awaiting_approval: int


class DashboardResponse(BaseModel):
    counts: DashboardCounts
    runs: list[RunView]
    providers: list[ProviderView]
    pending_approvals: list[ApprovalView]
    recent_failures: list[EventView]
    operations: OperationalStateView


class MonitorResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checked_at: str
    database: dict[str, Any]
    migrations: dict[str, int]
    providers: dict[str, int]
    agents: dict[str, int]
    runtime: dict[str, int]
    safety: dict[str, Any]
    blockers: list[str]


class ConfirmedCommand(BaseModel):
    confirmed: bool


class ClaimCommand(ConfirmedCommand):
    agent_id: str


class RunCommand(ConfirmedCommand):
    workflow_id: str = "delivery"
    mode: Literal["simulation"] = "simulation"


class ReviewCommand(ConfirmedCommand):
    task_id: int
    decision: Literal["approved", "rejected"]
    note: str = ""


class AgentEnabledCommand(ConfirmedCommand):
    enabled: bool


class AgentProviderCommand(ConfirmedCommand):
    provider: str
    model: str = ""


class AgentCommandResult(BaseModel):
    agent: AgentView
    impact_summary: str


class RuntimeSettingCommand(ConfirmedCommand):
    value: int


class GitHubPreviewCommand(ConfirmedCommand):
    repo: str
    backlog_path: str
    existing_issues: list[dict[str, Any]] = Field(default_factory=list)


class FounderDecisionCommand(ConfirmedCommand):
    decision: Literal["approved", "rejected"]
    note: str = ""
    actor: Literal["Founder"] = "Founder"


class ReviewedBacklogItem(BaseModel):
    stable_id: str
    title: str
    description: str
    acceptance_criteria: list[str]


class BacklogImportCommand(ConfirmedCommand):
    project_name: str
    project_description: str = ""
    backlog_path: str
    reviewed_items: list[ReviewedBacklogItem] | None = None


class ArchiveWorkItemCommand(ConfirmedCommand):
    reason: str = ""


class ExecutionLeaseCommand(ConfirmedCommand):
    assignment_id: int
    fencing_token: int


class ExecutionTargetCommand(ConfirmedCommand):
    reason: str = "Stopped from Local Control Center"


class UploadedBacklogResponse(BaseModel):
    source_path: str
    source_name: str
    recommended_agent: str
    agent_role: str
    analysis_status: Literal["completed", "needs_review"]
    source_type: str
    analysis_method: Literal["deterministic_import"] = "deterministic_import"
    # A deterministic import is never a confirmed plan. plan_ready stays false
    # while the source leaves a requirement unstated, and the questions below
    # are what the reviewer has to answer before importing.
    plan_ready: bool = False
    clarifications: list[str] = []
    original_path: str
    original_sha256: str
    original_text: str
    counts: dict[str, int]
    items: list[dict[str, Any]]


class RunDetail(BaseModel):
    run: RunView
    artifacts: list[ArtifactView]
    reviews: list[ReviewView]
    approval: ApprovalView | None
    stopped_reason: str

class SettingChangeCommand(ConfirmedCommand):
    value: str = Field(default="", max_length=1000)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=300)
    acknowledged_consequence: bool = False


class FeedbackAttachmentField(BaseModel):
    kind: str = Field(max_length=40)
    name: str = Field(max_length=200)
    size_bytes: int = Field(default=0, ge=0)
    digest: str = Field(default="", max_length=64)
    leaves_machine: bool = False


class FeedbackCommand(ConfirmedCommand):
    played_version: str = Field(min_length=1, max_length=64)
    wish: str = Field(min_length=1, max_length=2000)
    steps: list[str] = Field(default_factory=list, max_length=20)
    attachments: list[FeedbackAttachmentField] = Field(default_factory=list, max_length=10)
    engine: str = Field(default="", max_length=40)
    played_at: str = Field(default="", max_length=40)


class TranslatedField(BaseModel):
    uk: str = Field(min_length=1, max_length=400)
    en: str = Field(min_length=1, max_length=400)


class ChangeField(TranslatedField):
    touches: list[str] = Field(default_factory=list, max_length=20)


class ImpactField(TranslatedField):
    requirement: str = Field(min_length=1, max_length=80)
    impact: str = Field(min_length=1, max_length=40)


class CostField(BaseModel):
    amount: float = Field(default=0.0, ge=0)
    unit: str = Field(default="USD", max_length=10)
    basis: TranslatedField | None = None


class ChangePlanCommand(ConfirmedCommand):
    changes: list[ChangeField] = Field(min_length=1, max_length=20)
    impacts: list[ImpactField] = Field(default_factory=list, max_length=40)
    added_scope: list[TranslatedField] = Field(default_factory=list, max_length=20)
    cost: CostField = Field(default_factory=CostField)
    current_version: str = Field(default="", max_length=64)


class AcceptPlanCommand(ConfirmedCommand):
    actor: str = Field(min_length=1, max_length=120)
    accept_cost: bool = False
    accept_scope: bool = False


class DeclareSliceCommand(ConfirmedCommand):
    outcome: str = Field(pattern="^(playable|nothing_to_test)$")
    project_key: str = Field(default="", max_length=120)
    version_digest: str = Field(default="", max_length=64)
    reason_uk: str = Field(default="", max_length=400)
    reason_en: str = Field(default="", max_length=400)
    declared_by: str = Field(default="", max_length=120)


class PauseCommand(ConfirmedCommand):
    actor: str = Field(min_length=1, max_length=120)
    finishing: list[str] = Field(default_factory=list, max_length=20)


class CommentCommand(ConfirmedCommand):
    text: str = Field(min_length=1, max_length=2000)
    scope: str = Field(default="game", pattern="^(game|stage|task)$")
    subject: str = Field(default="", max_length=200)
    author: str = Field(default="", max_length=120)


class SetLimitCommand(ConfirmedCommand):
    amount: float = Field(ge=0)
    actor: str = Field(min_length=1, max_length=120)
    unit: str = Field(default="USD", max_length=10)
    reason: str = Field(default="", max_length=300)


class PaidToolCommand(ConfirmedCommand):
    tool: str = Field(min_length=1, max_length=120)
    reason_uk: str = Field(min_length=1, max_length=400)
    reason_en: str = Field(min_length=1, max_length=400)
    blocks: list[str] = Field(default_factory=list, max_length=40)
    alternative_uk: str = Field(default="", max_length=200)
    alternative_en: str = Field(default="", max_length=200)
    task_key: str = Field(default="", max_length=120)


class PaidToolAnswerCommand(ConfirmedCommand):
    choice: str = Field(min_length=1, max_length=40)
    actor: str = Field(min_length=1, max_length=120)


class RegisterMachineCommand(ConfirmedCommand):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(cloud_worker|this_pc|web_container)$")
    capabilities: list[str] = Field(default_factory=list, max_length=40)
    video_memory_gb: float = Field(default=0.0, ge=0)
    registered_by: str = Field(default="", max_length=120)


class ConnectSourceCommand(ConfirmedCommand):
    kind: str = Field(
        pattern="^(own_subscription|platform_subscription|local_model)$")
    name: str = Field(min_length=1, max_length=200)
    state: str = Field(default="unverified", pattern="^(verified|unverified|unavailable)$")
    machine_key: str = Field(default="", max_length=120)
    needed_gb: float = Field(default=0.0, ge=0)
    connected_by: str = Field(default="", max_length=120)


class MandateCommand(ConfirmedCommand):
    """What a named person allows the studio to do here without asking again."""

    actor: str = Field(min_length=1, max_length=120)
    steps: list[str] = Field(default_factory=list, max_length=8)
    ceiling: float | None = Field(default=None, ge=0)
    unit: str = Field(default="USD", max_length=8)
    hours: float = Field(default=24.0, gt=0, le=24 * 30)
    reason: str = Field(default="", max_length=500)


class RevokeMandateCommand(ConfirmedCommand):
    actor: str = Field(min_length=1, max_length=120)


class RosterCommand(ConfirmedCommand):
    actor: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="", max_length=60)
    model: str = Field(default="", max_length=120)
    concurrency: str = Field(default="sequential", pattern="^(sequential|parallel)$")
    reason: str = Field(default="", max_length=300)


class AnswerQuestionCommand(ConfirmedCommand):
    answer: str = Field(min_length=1, max_length=40)
    actor: str = Field(min_length=1, max_length=120)


class SettingResetCommand(ConfirmedCommand):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=300)
    acknowledged_consequence: bool = False


class ControlActionCommand(ConfirmedCommand):
    tenant_id: str
    actor: str
    role: str
    action: str
    target_type: str
    target_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


Offset = Annotated[int, Query(ge=0, le=1_000_000)]
Limit = Annotated[int, Query(ge=1, le=200)]
Confirmation = Annotated[str | None, Header(alias="X-Agent-Factory-Confirm")]


def validate_loopback_host(host: str) -> str:
    normalized = host.strip().lower()
    allowed = {"127.0.0.1", "localhost", "::1"}
    if normalized not in allowed:
        raise ValueError("Local Control Center must bind to a loopback host")
    return normalized


def _page(items: list[T], offset: int, limit: int) -> Page[T]:
    return Page(items=items[offset : offset + limit], offset=offset, limit=limit, total=len(items))


def _require_confirmation(command: ConfirmedCommand, header: str | None) -> None:
    if command.confirmed is not True or header != "true":
        raise ValueError("Explicit confirmation is required")


def create_app(workspace: Path, database: Path, *, environment_probes=None, credential_store=None) -> FastAPI:
    workspace = workspace.expanduser().resolve()
    database = database.expanduser().resolve()
    from .studio_runner import StudioRunner
    from .studio_launch_web import install_routes as install_studio_launch_routes
    studio_runner = StudioRunner(database, workspace)
    temporal_settings = TemporalSettings.from_env()
    probe_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-health")
    probe_guard = threading.RLock()
    probe_cache = {"future": None, "key": None, "finished": 0.0}

    def provider_configuration_key():
        path = config_path_for_workspace("providers", workspace)
        return (str(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def collect_provider_health():
        # SQLite connections are created, used and closed in this same worker.
        with closing(SQLiteStorage(database)) as storage:
            return AgentFactoryService(storage, workspace=workspace).providers()

    async def provider_snapshot():
        key = provider_configuration_key()
        with probe_guard:
            future = probe_cache["future"]
            fresh = (probe_cache["key"] == key and
                     time.monotonic() - probe_cache["finished"] < 5)
            if future is None or (future.done() and not fresh):
                future = probe_executor.submit(collect_provider_health)
                probe_cache.update(future=future, key=key, finished=0.0)
                def completed(done):
                    with probe_guard:
                        if probe_cache["future"] is done:
                            probe_cache["finished"] = time.monotonic()
                future.add_done_callback(completed)
            selected_key = probe_cache["key"]
        try:
            # Cancellation/timeout of one HTTP reader must not cancel another's
            # shared probe or enqueue a second slow CLI batch.
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), 10)
        except TimeoutError:
            raise HTTPException(503, "provider_health_pending") from None
        except Exception:
            raise HTTPException(503, "provider_health_unavailable") from None
        if selected_key != provider_configuration_key() or selected_key != key:
            raise HTTPException(503, "provider_configuration_changed")
        return result

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if temporal_settings.enabled:
            app.state.temporal_client = await connect_temporal(
                temporal_settings, initialize_namespace=True
            )
        try:
            yield
        finally:
            probe_executor.shutdown(wait=False, cancel_futures=True)
            studio_runner.close()

    app = FastAPI(
        title="Lokvetia Core — Local Control Center",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    static_directory = Path(__file__).resolve().parent / "static"
    access = access_for_workspace(workspace)
    app.state.local_access = access

    def access_error(status: int, code: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code}},
                            headers={"Cache-Control": "no-store"})

    app.add_middleware(LocalHTTPBoundary, access=access)
    install_sso_routes(app, access)
    from .desktop_downloads import install_download_routes
    install_download_routes(app)
    install_credential_routes(app, workspace, store=credential_store)
    install_hardware_routes(app, workspace)
    install_game_planning_routes(app, database)
    install_configuration_advice_routes(app)
    install_installation_routes(app, database, workspace)
    install_studio_launch_routes(app, database, workspace, studio_runner)

    @app.get("/auth/session", include_in_schema=False)
    async def session_status(request: Request):
        principal = request.state.local_principal
        return {"authentication_required": bool(request.state.local_policy.token),
                "authenticated": principal is not None,
                "workspace_access": bool(principal and principal.role != 'account_user'),
                "actor": principal.actor if principal else None}

    @app.post("/auth/session", include_in_schema=False)
    async def session_login(request: Request):
        if request.headers.get("X-Agent-Factory-Session") != "true":
            return access_error(403, "session_intent_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return access_error(400, "invalid_session_request")
        try:
            document = json.loads(body)
            candidate = document["token"]
            if set(document) != {"token"} or not isinstance(candidate, str):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return access_error(400, "invalid_session_request")
        cookie = access.login(request.state.local_policy, candidate)
        if cookie is None:
            return access_error(401, "authentication_required")
        access.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"authenticated": True})
        response.set_cookie(COOKIE, cookie, max_age=request.state.local_policy.ttl,
                            httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    @app.delete("/auth/session", include_in_schema=False)
    async def session_logout(request: Request):
        if request.headers.get("X-Agent-Factory-Session") != "true":
            return access_error(403, "session_intent_required")
        access.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.get("/login", include_in_schema=False)
    async def login_shell():
        if isinstance(access, SsoAccess):
            from starlette.responses import RedirectResponse
            return RedirectResponse('/auth/sso/start', status_code=303)
        return FileResponse(static_directory / "login.html")

    app.mount("/assets", StaticFiles(directory=static_directory), name="assets")
    async def service_dependency() -> AsyncIterator[AgentFactoryService]:
        storage = SQLiteStorage(database)
        try:
            yield AgentFactoryService(storage, workspace=workspace)
        finally:
            storage.close()

    Service = Annotated[AgentFactoryService, Depends(service_dependency)]

    @app.exception_handler(KeyError)
    async def not_found(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": str(exc).strip("'")}},
        )

    @app.exception_handler(ValueError)
    async def invalid_request(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": str(exc)}},
        )

    @app.exception_handler(PermissionError)
    async def forbidden_operation(_request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "operation_conflict", "message": str(exc)}},
        )

    @app.exception_handler(RuntimeError)
    async def operation_blocked(_request: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "operation_blocked", "message": str(exc)}},
        )

    @app.exception_handler(sqlite3.Error)
    async def storage_unavailable(_request: Request, exc: sqlite3.Error) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "storage_unavailable",
                    "message": type(exc).__name__,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def malformed_request(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request parameters are invalid",
                    "details": exc.errors(),
                }
            },
        )

    @app.get("/api/health", response_model=HealthResponse)
    async def health(service: Service) -> HealthResponse:
        integrity = service.storage.integrity_check()
        return HealthResponse(
            status="ready" if integrity["ok"] else "degraded",
            database=str(database),
            integrity=integrity,
        )

    @app.get("/api/dashboard", response_model=DashboardResponse)
    async def dashboard(service: Service) -> DashboardResponse:
        items = service.work_items()
        runs = service.runs()
        approvals = service.approvals()
        artifacts = service.artifacts()
        by_id = {item.id: item for item in items}
        blocked = sum(
            item.status == "pending"
            and any(
                dependency not in by_id
                or by_id[dependency].status not in {"completed", "approved"}
                for dependency in item.dependencies
            )
            for item in items
        )

        ready = sum(
            item.status == "pending"
            and all(
                dependency in by_id
                and by_id[dependency].status in {"completed", "approved"}
                for dependency in item.dependencies
            )
            for item in items
        )
        failures = [
            event
            for event in service.events(limit=100)
            if event.event_type.endswith(".failed")
            or event.payload.get("ok") is False
            or "error" in event.payload
        ][:10]
        return DashboardResponse(
            counts=DashboardCounts(
                ready=ready,
                active=sum(run.status == "running" for run in runs),
                blocked=blocked,
                failed=sum(run.status == "failed" for run in runs),
                awaiting_review=sum(artifact.status == "pending" for artifact in artifacts),
                awaiting_approval=sum(item.status == "pending" for item in approvals),
            ),
            runs=runs[-10:][::-1],
            providers=await provider_snapshot(),
            pending_approvals=[item for item in approvals if item.status == "pending"],
            recent_failures=failures,
            operations=service.operational_state(),
        )

    @app.get("/api/executions", response_model=dict[str, list[dict[str, Any]]])
    async def executions(service: Service) -> dict[str, list[dict[str, Any]]]:
        return service.active_executions()

    def games_call(request, operation):
        try:
            # Concurrent first-page reads must not race database migrations.
            with local_games_lock(database):
                storage = SQLiteStorage(database)
            with closing(storage):
                return operation(LocalGames(storage, workspace), request.state.local_principal.actor)
        except KeyError:
            raise HTTPException(404, 'game_not_found') from None
        except GameConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            allowed = {'invalid_text', 'invalid_command', 'invalid_version_or_step', 'idea_required',
                       'model_required', 'model_unavailable', 'provider_catalog_unavailable'}
            code = str(error) if str(error) in allowed else 'invalid_game_request'
            raise HTTPException(400, code) from None

    @app.get('/api/games/models')
    def game_models(request: Request):
        return games_call(request, lambda games, actor: {'items': games.model_choices()})

    @app.get('/api/games/starts')
    def game_starts(request: Request, q: str = Query('', max_length=200), offset: Offset = 0, limit: Limit = 20):
        return games_call(request, lambda games, actor: games.list(actor, q=q, offset=offset, limit=limit))

    @app.post('/api/games/starts')
    def game_create(request: Request, command: GameCreateCommand):
        return games_call(request, lambda games, actor: games.create(actor, command.command_id))

    @app.get('/api/games/missions')
    def game_missions(request: Request, q: str = Query('', max_length=200), offset: Offset = 0, limit: Limit = 20):
        return games_call(request, lambda games, actor: games.existing(actor, q=q, offset=offset, limit=limit))

    @app.get('/api/games/missions/{mission_id}')
    def game_mission(mission_id: int, request: Request):
        return games_call(request, lambda games, actor: games.project(mission_id, actor))

    @app.get('/api/games/starts/{ident}')
    def game_start(ident: str, request: Request):
        return games_call(request, lambda games, actor: games.detail(ident, actor))

    @app.post('/api/games/starts/{ident}/save')
    def game_save(ident: str, request: Request, command: GameSaveCommand):
        return games_call(request, lambda games, actor: games.save(ident, actor, command.command_id,
            command.expected_revision, title=command.title, idea=command.idea,
            model_key=command.model_key, view_step=command.view_step))

    @app.post('/api/games/starts/{ident}/submit')
    def game_submit(ident: str, request: Request, command: GameSubmitCommand,
                    confirmed: str | None = Header(default=None, alias='X-Agent-Factory-Confirm')):
        if command.confirmed is not True or confirmed != 'true':
            raise HTTPException(400, 'confirmation_required')
        return games_call(request, lambda games, actor: games.materialize(ident, actor, command.command_id, command.expected_revision))

    @app.get('/api/games/starts/{ident}/versions')
    def game_versions(ident: str, request: Request, q: str = Query('', max_length=200), offset: Offset = 0, limit: Limit = 20):
        return games_call(request, lambda games, actor: games.versions(ident, actor, q=q, offset=offset, limit=limit))

    @app.get('/api/work-item-filters')
    async def work_item_filters(service: Service, project_id: int | None = None):
        rows = service.work_items(project_id)
        return {key: sorted({getattr(row, key) for row in rows if getattr(row, key)})
                for key in ('status', 'kind', 'priority', 'assignee')}

    @app.get("/api/environment/missions", response_model=dict)
    def environment_missions(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        with closing(SQLiteStorage(database)) as storage:
            rows = storage.db.execute("SELECT id,name FROM autonomous_missions "
                "WHERE active_execution_epoch_id IS NOT NULL ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = storage.db.execute("SELECT COUNT(*) FROM autonomous_missions WHERE active_execution_epoch_id IS NOT NULL").fetchone()[0]
            return {"items": [dict(row) for row in rows], "offset": offset, "limit": limit, "total": total}

    def environment_report(mission_id: int, storage: SQLiteStorage, *, refresh: bool):
        row = storage.db.execute(
            "SELECT approval.id FROM autonomous_backlog_approvals approval "
            "JOIN autonomous_missions mission ON mission.id=approval.mission_id "
            "WHERE mission.id=? AND approval.execution_epoch_id=mission.active_execution_epoch_id "
            "ORDER BY approval.id DESC LIMIT 1", (mission_id,)
        ).fetchone()
        if not row:
            return {"status": "blocked", "mode": "unknown", "checks": [],
                    "next_action": "Approve an explicit environment profile with the selected plan first."}
        readiness = EnvironmentReadiness(storage, probes=environment_probes)
        try:
            return readiness.assess(row['id']) if refresh else readiness.current(row['id'])
        except EnvironmentNotReady as error:
            return {"status": "blocked", "mode": "unknown", "checks": [], "next_action": str(error)}

    @app.get("/api/autonomous-missions/{mission_id}/environment", response_model=dict)
    def read_environment(mission_id: int):
        with closing(SQLiteStorage(database)) as storage:
            return environment_report(mission_id, storage, refresh=False)

    @app.post("/api/autonomous-missions/{mission_id}/environment/check", response_model=dict)
    def check_environment(mission_id: int):
        with closing(SQLiteStorage(database)) as storage:
            return environment_report(mission_id, storage, refresh=True)

    @app.get("/api/monitor", response_model=MonitorResponse)
    async def monitor(service: Service) -> MonitorResponse:
        integrity = service.storage.integrity_check()
        migration_row = service.storage.db.execute(
            "SELECT COALESCE(MAX(version), 0) AS current_version FROM schema_migrations"
        ).fetchone()
        current_version = int(migration_row["current_version"])
        latest_version = max(version for version, _ in MIGRATIONS)
        providers = await provider_snapshot()
        agents = service.agents()
        operational = service.operational_state()
        safety = service.storage.policy_state()
        blockers: list[str] = []
        if not integrity["ok"]:
            blockers.append("database_integrity_failed")
        if current_version < latest_version:
            blockers.append("database_migrations_pending")
        if any(item.status not in {"ready", "disabled"} for item in providers):
            blockers.append("provider_health_degraded")
        if not any(item.enabled for item in agents):
            blockers.append("no_enabled_agents")
        if safety["emergency_stop"]:
            blockers.append("emergency_stop_active")
        return MonitorResponse(
            status="ready" if not blockers else "degraded",
            checked_at=datetime.now(timezone.utc).isoformat(),
            database={"ok": bool(integrity["ok"]), "path": str(service.storage.path.resolve())},
            migrations={"current": current_version, "latest": latest_version},
            providers={
                "total": len(providers),
                "ready": sum(item.status == "ready" for item in providers),
                "enabled": sum(item.enabled for item in providers),
                "execution_enabled": sum(item.execution_enabled for item in providers),
            },
            agents={"total": len(agents), "enabled": sum(item.enabled for item in agents)},
            runtime={
                "active_sessions": operational.active_sessions,
                "queued_tasks": operational.queued_tasks,
                "active_leases": operational.active_leases,
                "active_worktrees": operational.active_worktrees,
                "failures": operational.failures,
            },
            safety={
                "emergency_stop": bool(safety["emergency_stop"]),
                "reason": safety["reason"],
                "version": safety["version"],
            },
            blockers=blockers,
        )

    @app.get("/api/projects", response_model=Page[ProjectView])
    async def projects(service: Service, offset: Offset = 0, limit: Limit = 50) -> Page[ProjectView]:
        return _page(service.projects(), offset, limit)

    @app.get("/api/work-items", response_model=Page[WorkItemView])
    async def work_items(
        service: Service,
        offset: Offset = 0,
        limit: Limit = 50,
        project_id: int | None = None,
        kind: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        dependency: int | None = None,
        assignee: str | None = None,
        q: str = Query("", max_length=200),
    ) -> Page[WorkItemView]:
        rows = service.work_items(project_id)
        if q:
            rows = [item for item in rows if q.casefold() in (item.title + " " + item.description).casefold()]
        if kind is not None:
            rows = [item for item in rows if item.kind == kind]
        if status is not None:
            rows = [item for item in rows if item.status == status]
        if priority is not None:
            rows = [item for item in rows if item.priority == priority]
        if dependency is not None:
            rows = [item for item in rows if dependency in item.dependencies]
        if assignee is not None:
            rows = [item for item in rows if item.assignee == assignee]
        return _page(rows, offset, limit)

    @app.get("/api/work-items/{task_id}", response_model=WorkItemView)
    async def work_item(task_id: int, service: Service) -> WorkItemView:
        return service.work_item(task_id)

    @app.post("/api/backlog/import", response_model=BacklogFileImportResult)
    async def import_backlog(
        command: BacklogImportCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> BacklogFileImportResult:
        _require_confirmation(command, confirmation)
        backlog_path = command.backlog_path
        if command.reviewed_items is not None:
            if not command.project_name.strip():
                raise ValueError("Backlog import requires a project name")
            upload_dir = (workspace / ".agent-factory" / "uploads").resolve()
            manifest = (workspace / backlog_path).resolve()
            if Path(backlog_path).is_absolute() or manifest.parent != upload_dir or not manifest.is_file():
                raise ValueError("Edited preview must reference an uploaded proposal")
            document = json.loads(manifest.read_text(encoding="utf-8"))
            source = document.get("source", {})
            if source.get("analysis_method") != "deterministic_import":
                raise ValueError("Edited preview must reference a deterministic upload")
            edits = {item.stable_id: item for item in command.reviewed_items}
            expected = {item["stable_id"] for item in document["items"]}
            if len(edits) != len(command.reviewed_items) or set(edits) != expected:
                raise ValueError("Edited preview must retain each proposed item exactly once")
            for item in document["items"]:
                item.update(edits[item["stable_id"]].model_dump())
            source["review_status"] = "user_confirmed"
            proposal = proposal_from_document(
                document, source_path=document["source_path"],
                source_sha256=document["source_sha256"], source_name=document["source_name"],
            )
            reviewed = json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2)
            reviewed_path = upload_dir / (hashlib.sha256(reviewed.encode("utf-8")).hexdigest() + ".reviewed.json")
            # A separate content-addressed file preserves the initial proposal.
            reviewed_path.write_text(reviewed, encoding="utf-8")
            backlog_path = reviewed_path.relative_to(workspace).as_posix()
        return service.import_backlog_file(
            command.project_name, backlog_path, command.project_description,
        )

    @app.post("/api/backlog/analyze-upload", response_model=UploadedBacklogResponse)
    async def analyze_upload(
        upload: UploadFile | None = File(None),
        specification: UploadFile | None = File(None),
    ) -> UploadedBacklogResponse:
        upload = upload or specification
        if upload is None:
            raise ValueError("Upload a specification file")
        raw = await upload.read()
        if not raw or len(raw) > 10 * 1024 * 1024:
            raise ValueError("Uploaded specification must be between 1 byte and 10 MB")
        source_name = Path(upload.filename or "uploaded-specification.txt").name
        upload_dir = (workspace / ".agent-factory" / "uploads").resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        original = upload_dir / f"{digest}.original"
        # Preserve bytes even if extraction or proposal validation fails.
        original.write_bytes(raw)
        proposal = analyze_specification(raw, source_name)
        document = proposal.to_dict()
        original_path = original.relative_to(workspace).as_posix()
        document["source"].update({
            "original_path": original_path, "original_sha256": digest,
            "analysis_method": "deterministic_import", "review_status": "needs_review",
        })
        for item in document["items"]:
            item["source_references"] = list(dict.fromkeys([
                *item["source_references"], f"{original_path}#sha256={digest}",
            ]))
        manifest_text = json.dumps(document, ensure_ascii=False, indent=2)
        manifest_name = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest() + ".json"
        manifest = upload_dir / manifest_name
        manifest.write_text(manifest_text, encoding="utf-8")
        counts = {
            kind: sum(item.level == kind for item in proposal.items)
            for kind in ("epic", "feature", "story", "task")
        }
        return UploadedBacklogResponse(
            source_path=manifest.relative_to(workspace).as_posix(),
            source_name=source_name,
            recommended_agent="backlog-steward",
            agent_role="Delivery Planner",
            analysis_status="needs_review",
            source_type=Path(source_name).suffix.lower().lstrip(".") or "binary",
            counts=counts,
            original_path=original_path,
            original_sha256=digest,
            original_text=document["source"]["original_text"],
            plan_ready=bool(document["source"].get("plan_ready", False)),
            clarifications=[str(question) for question in document["source"].get("clarifications", [])],
            items=document["items"],
        )

    @app.get("/api/runs", response_model=Page[RunView])
    async def runs(
        service: Service,
        offset: Offset = 0,
        limit: Limit = 50,
        task_id: int | None = None,
    ) -> Page[RunView]:
        return _page(service.runs(task_id), offset, limit)

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def run(run_id: int, service: Service) -> RunView:
        return service.run(run_id)

    @app.get("/api/runs/{run_id}/detail", response_model=RunDetail)
    async def run_detail(run_id: int, service: Service) -> RunDetail:
        run = service.run(run_id)
        artifacts = service.artifacts(run_id)
        reviews = service.reviews(run_id, limit=10_000)
        approval = next(
            (
                item
                for item in service.approvals()
                if item.kind == "workflow" and item.target_id == run_id
            ),
            None,
        )
        reason = {
            "awaiting_approval": "Founder decision required",
            "failed": "Workflow failed; inspect stage evidence and audit events",
            "approved": "Founder approved the accumulated evidence",
            "rejected": "Founder rejected the accumulated evidence",
            "running": "Workflow is still executing",
        }.get(run.status, f"Workflow stopped in state {run.status}")
        return RunDetail(
            run=run,
            artifacts=artifacts,
            reviews=reviews,
            approval=approval,
            stopped_reason=reason,
        )

    @app.post("/api/work-items/{task_id}/claim", response_model=dict[str, Any])
    async def claim_work_item(
        task_id: int, command: ClaimCommand, service: Service, confirmation: Confirmation = None
    ) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        result = service.claim_work_item(task_id, command.agent_id)
        return asdict(result)

    @app.post("/api/executions/runs/{run_id}/cancel", response_model=dict[str, Any])
    async def cancel_execution_run(run_id: int, command: ExecutionTargetCommand, service: Service, confirmation: Confirmation = None) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        workflow_id = service.temporal_workflow_id(run_id)
        if temporal_settings.enabled and workflow_id:
            await signal_workflow(app.state.temporal_client, workflow_id, "cancel")
            return {
                "run_id": run_id,
                "workflow_id": workflow_id,
                "status": "cancelling",
            }
        return service.cancel_execution_run(run_id, command.reason)

    @app.post("/api/executions/runs/{run_id}/pause", response_model=dict[str, Any])
    async def pause_execution_run(run_id: int, command: ExecutionTargetCommand, service: Service, confirmation: Confirmation = None) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        workflow_id = service.temporal_workflow_id(run_id)
        if not temporal_settings.enabled or not workflow_id:
            raise RuntimeError("The run is not orchestrated by Temporal")
        await signal_workflow(app.state.temporal_client, workflow_id, "pause")
        return {"run_id": run_id, "workflow_id": workflow_id, "status": "paused"}

    @app.post("/api/executions/runs/{run_id}/resume", response_model=dict[str, Any])
    async def resume_execution_run(run_id: int, command: ExecutionTargetCommand, service: Service, confirmation: Confirmation = None) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        workflow_id = service.temporal_workflow_id(run_id)
        if not temporal_settings.enabled or not workflow_id:
            raise RuntimeError("The run is not orchestrated by Temporal")
        await signal_workflow(app.state.temporal_client, workflow_id, "resume")
        return {"run_id": run_id, "workflow_id": workflow_id, "status": "running"}

    @app.get("/api/runs/{run_id}/temporal", response_model=dict[str, Any])
    async def temporal_run_status(run_id: int, service: Service) -> dict[str, Any]:
        workflow_id = service.temporal_workflow_id(run_id)
        if not temporal_settings.enabled or not workflow_id:
            raise KeyError(f"Run {run_id} has no Temporal workflow")
        snapshot = await workflow_snapshot(app.state.temporal_client, workflow_id)
        snapshot["ui_url"] = temporal_settings.workflow_url(workflow_id)
        return snapshot

    @app.post("/api/executions/sessions/{session_id}/stop", response_model=dict[str, Any])
    async def stop_execution_session(session_id: int, command: ExecutionTargetCommand, service: Service, confirmation: Confirmation = None) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        return service.stop_execution_session(session_id, command.reason)

    @app.post("/api/executions/leases/release", response_model=dict[str, Any])
    async def release_execution_lease(command: ExecutionLeaseCommand, service: Service, confirmation: Confirmation = None) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        return service.release_execution_lease(command.assignment_id, command.fencing_token)

    @app.post("/api/work-items/{task_id}/archive", response_model=WorkItemView)
    async def archive_work_item(
        task_id: int,
        command: ArchiveWorkItemCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> WorkItemView:
        _require_confirmation(command, confirmation)
        return service.archive_work_item(task_id, command.reason)

    @app.post("/api/work-items/archive-all", response_model=dict[str, Any])
    async def archive_all_work_items(
        command: ArchiveWorkItemCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        return service.archive_all_work_items(command.reason)

    @app.post("/api/work-items/{task_id}/runs", response_model=RunView)
    async def start_workflow(
        task_id: int, command: RunCommand, service: Service, confirmation: Confirmation = None
    ) -> RunView:
        _require_confirmation(command, confirmation)
        if temporal_settings.enabled:
            run, job = service.prepare_temporal_workflow(
                task_id, command.workflow_id, command.mode
            )
            try:
                started = await start_job_workflow(
                    app.state.temporal_client, job, temporal_settings
                )
            except Exception as exc:
                service.storage.finish_run(
                    run.id,
                    "failed",
                    event_payload={
                        "error": "Temporal workflow start failed",
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            service.storage.event(
                "workflow.temporal.started",
                "run",
                run.id,
                {
                    "job_id": job.job_id,
                    "workflow_id": started.workflow_id,
                    "workflow_run_id": started.run_id,
                    "namespace": temporal_settings.namespace,
                    "task_queue": temporal_settings.task_queue,
                    "duplicate": started.duplicate,
                },
            )
            return service.run(run.id)
        return service.run_workflow(task_id, command.workflow_id, command.mode)

    @app.post("/api/artifacts/{artifact_id}/review", response_model=ArtifactView)
    async def review_artifact(
        artifact_id: int,
        command: ReviewCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> ArtifactView:
        _require_confirmation(command, confirmation)
        return service.review_artifact(
            command.task_id, artifact_id, command.decision, command.note
        )

    @app.get("/api/artifacts", response_model=Page[ArtifactView])
    async def artifacts(
        service: Service,
        offset: Offset = 0,
        limit: Limit = 50,
        run_id: int | None = None,
        task_id: int | None = None,
    ) -> Page[ArtifactView]:
        return _page(service.artifacts(run_id, task_id=task_id), offset, limit)

    @app.get("/api/agents", response_model=Page[AgentView])
    async def agents(service: Service, offset: Offset = 0, limit: Limit = 50) -> Page[AgentView]:
        return _page(service.agents(), offset, limit)

    @app.post(
        "/api/agents/{agent_id}/enabled", response_model=AgentCommandResult
    )
    async def set_agent_enabled(
        agent_id: str,
        command: AgentEnabledCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> AgentCommandResult:
        _require_confirmation(command, confirmation)
        agent = service.set_agent_enabled(agent_id, command.enabled)
        action = "receive future assignments" if agent.enabled else "be excluded from assignments"
        return AgentCommandResult(
            agent=agent,
            impact_summary=f"{agent.id} will {action}; existing evidence remains immutable",
        )

    @app.post(
        "/api/agents/{agent_id}/provider", response_model=AgentCommandResult
    )
    async def replace_agent_provider(
        agent_id: str,
        command: AgentProviderCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> AgentCommandResult:
        _require_confirmation(command, confirmation)
        agent = service.replace_agent_provider(
            agent_id, command.provider, command.model
        )
        return AgentCommandResult(
            agent=agent,
            impact_summary=(
                f"Future {agent.role} assignments use {agent.provider} / {agent.model}; "
                "existing artifacts remain attributed to their original producer"
            ),
        )

    @app.get("/api/providers", response_model=Page[ProviderView])
    async def providers(
        service: Service, offset: Offset = 0, limit: Limit = 50
    ) -> Page[ProviderView]:
        return _page(await provider_snapshot(), offset, limit)

    @app.get("/api/reviews", response_model=Page[ReviewView])
    async def reviews(
        service: Service,
        offset: Offset = 0,
        limit: Limit = 50,
        run_id: int | None = None,
    ) -> Page[ReviewView]:
        rows = service.reviews(run_id, limit=10_000)
        return _page(rows, offset, limit)

    @app.get("/api/approvals", response_model=Page[ApprovalView])
    async def approvals(
        service: Service, offset: Offset = 0, limit: Limit = 50
    ) -> Page[ApprovalView]:
        return _page(service.approvals(), offset, limit)

    @app.get("/api/founder-decisions", response_model=list[FounderDecisionPacket])
    async def founder_decisions(
        service: Service, include_decided: bool = False
    ) -> list[FounderDecisionPacket]:
        return service.founder_decisions(pending_only=not include_decided)

    @app.post(
        "/api/founder-decisions/{gate_id}", response_model=FounderDecisionReceipt
    )
    async def founder_decide(
        gate_id: int,
        command: FounderDecisionCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
    ) -> FounderDecisionReceipt:
        _require_confirmation(command, confirmation)
        if command.actor != request.state.local_principal.actor:
            raise HTTPException(403, "Authenticated actor does not match the decision actor")
        return service.founder_decide(
            gate_id, command.decision, command.note, command.actor
        )

    @app.get("/api/events", response_model=Page[AuditEventView])
    async def events(
        service: Service,
        offset: Offset = 0,
        limit: Limit = 50,
        from_time: str | None = None,
        to_time: str | None = None,
        project_id: int | None = None,
        task_id: int | None = None,
        run_id: int | None = None,
        agent_id: str | None = None,
        provider: str | None = None,
        action: str | None = None,
        outcome: Literal["success", "failure", "pending", "info"] | None = None,
    ) -> Page[AuditEventView]:
        rows = service.audit_events(
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            provider=provider,
            action=action,
            outcome=outcome,
        )
        return _page(rows, offset, limit)

    @app.get("/api/settings", response_model=SettingsView)
    async def settings(service: Service) -> SettingsView:
        return service.settings()

    @app.post(
        "/api/settings/{key}", response_model=RuntimeSettingView
    )
    async def update_runtime_setting(
        key: str,
        command: RuntimeSettingCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> RuntimeSettingView:
        _require_confirmation(command, confirmation)
        return service.update_runtime_setting(key, command.value)

    def settings_centre(service: Service):
        from .settings_store import SettingsCentre

        return SettingsCentre(service.storage)

    def chosen_language(request: Request, explicit: str | None = None) -> str:
        from .localisation import LANGUAGE_COOKIE, negotiate

        return negotiate(
            explicit=explicit,
            stored=request.cookies.get(LANGUAGE_COOKIE),
            accept_language=request.headers.get("accept-language"),
        )

    def settings_error(exc: Exception, language: str = "uk") -> JSONResponse:
        from .localisation import LocalisedError
        from .settings_store import ActorRequired, ConfirmationRequired, NotReconfigurable

        codes = {
            ConfirmationRequired: (409, "confirmation_required"),
            NotReconfigurable: (409, "not_reconfigurable"),
            ActorRequired: (400, "actor_required"),
            KeyError: (404, "unknown_setting"),
        }
        status, code = codes.get(type(exc), (400, "invalid_setting"))
        message = (
            exc.text(language) if isinstance(exc, LocalisedError)
            else str(exc).strip("'")
        )
        return JSONResponse(
            status_code=status, content={"error": {"code": code, "message": message}}
        )

    @app.get("/api/i18n", response_model=dict[str, Any])
    async def interface_messages(request: Request, lang: str | None = None) -> Any:
        from .localisation import LANGUAGES, bundle

        language = chosen_language(request, lang)
        return {
            "language": language,
            "available": list(LANGUAGES),
            "messages": bundle(language),
        }

    @app.get("/api/settings/sections", response_model=dict[str, Any])
    async def settings_sections(
        request: Request, service: Service, lang: str | None = None
    ) -> Any:
        return settings_centre(service).overview(chosen_language(request, lang))

    @app.get("/api/settings/sections/{section_id}", response_model=dict[str, Any])
    async def settings_section(
        section_id: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        language = chosen_language(request, lang)
        try:
            return settings_centre(service).section_view(section_id, language)
        except KeyError as exc:
            return settings_error(exc, language)

    @app.post("/api/settings/sections/{section_id}/verify", response_model=dict[str, Any])
    async def settings_verify(
        section_id: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        centre = settings_centre(service)
        language = chosen_language(request, lang)
        try:
            findings = centre.verify_section(section_id)
        except KeyError as exc:
            return settings_error(exc, language)
        return {
            "section": section_id,
            "language": language,
            "findings": [finding.record(language) for finding in findings],
        }

    @app.get("/api/settings/changes", response_model=dict[str, Any])
    async def settings_changes(
        service: Service, key: str | None = None, limit: Limit = 50
    ) -> Any:
        try:
            history = settings_centre(service).changes(key=key, limit=limit)
        except (KeyError, ValueError) as exc:
            return settings_error(exc)
        return {"changes": [item.record for item in history]}

    @app.post("/api/settings/values/{key}", response_model=dict[str, Any])
    async def settings_set(
        key: str,
        command: SettingChangeCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            return settings_centre(service).set(
                key, command.value, actor=command.actor, reason=command.reason,
                acknowledged_consequence=command.acknowledged_consequence,
                language=language,
            )
        except (KeyError, ValueError, PermissionError) as exc:
            return settings_error(exc, language)

    @app.post("/api/settings/values/{key}/reset", response_model=dict[str, Any])
    async def settings_reset(
        key: str,
        command: SettingResetCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            return settings_centre(service).reset(
                key, actor=command.actor, reason=command.reason,
                acknowledged_consequence=command.acknowledged_consequence,
                language=language,
            )
        except (KeyError, ValueError, PermissionError) as exc:
            return settings_error(exc, language)

    def autonomy_journal(service: Service):
        from .studio_autonomy import AutonomyJournal

        return AutonomyJournal(service.storage)

    @app.get("/api/studio/gates", response_model=dict[str, Any])
    async def studio_gates(request: Request, lang: str | None = None) -> Any:
        """What is decided automatically, and the only three things still asked."""
        from .studio_autonomy import catalogue

        return catalogue(chosen_language(request, lang))

    def stage_boundaries(service: Service):
        from .studio_slices import StageBoundaries

        return StageBoundaries(service.storage)

    def studio_cycles(service: Service):
        from .studio_cycles import StudioCycles

        return StudioCycles(service.storage)

    def studio_error(exc: Exception, language: str, code: str) -> JSONResponse:
        from .localisation import LocalisedError

        message = (
            exc.text(language) if isinstance(exc, LocalisedError)
            else str(exc).strip("'")
        )
        return JSONResponse(
            status_code=409, content={"error": {"code": code, "message": message}}
        )

    def studio_costs(service: Service):
        from .studio_cost import StudioCosts

        return StudioCosts(service.storage)

    def paid_tools(service: Service):
        from .studio_paid_tools import PaidTools

        return PaidTools(service.storage)

    def studio_machines(service: Service):
        from .studio_workers import StudioMachines

        return StudioMachines(service.storage)

    def studio_first_run(service: Service):
        from .studio_first_run import FirstRun

        return FirstRun(service.storage)

    @app.get("/api/studio/machines", response_model=dict[str, Any])
    async def studio_machine_list(
        request: Request, service: Service, lang: str | None = None
    ) -> Any:
        """Every machine, and what the web container is never allowed to answer."""
        return studio_machines(service).overview(
            language=chosen_language(request, lang),
        )

    @app.post("/api/studio/machines/{machine_key}", response_model=dict[str, Any])
    async def studio_register_machine(
        machine_key: str,
        command: RegisterMachineCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .studio_workers import WorkerRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            machine = studio_machines(service).register(
                machine_key, name=command.name, kind=command.kind,
                capabilities=command.capabilities,
                video_memory_gb=command.video_memory_gb,
                registered_by=command.registered_by,
            )
        except WorkerRefused as exc:
            return studio_error(exc, language, "machine_refused")
        return machine.record(language)

    def studio_supervisor(service: Service):
        from .studio_supervisor import Supervisor

        return Supervisor(service.storage)

    @app.get("/api/studio/supervisor/{mission_key}", response_model=dict[str, Any])
    async def studio_supervisor_state(
        mission_key: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        """Whether the studio is running this game on its own, and what it did."""
        return studio_supervisor(service).report(
            mission_key, language=chosen_language(request, lang),
        )

    @app.post("/api/studio/supervisor/{mission_key}", response_model=dict[str, Any])
    async def studio_grant_mandate(
        mission_key: str,
        command: MandateCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        """Let the studio work on this game without being asked each time."""
        from .studio_supervisor import SupervisorRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            mandate = studio_supervisor(service).grant(
                mission_key, steps=command.steps or ("plan",),
                granted_by=command.actor, ceiling=command.ceiling,
                unit=command.unit, hours=command.hours, reason=command.reason,
            )
        except SupervisorRefused as exc:
            return studio_error(exc, language, "mandate_refused")
        return mandate.record(language)

    @app.post("/api/studio/supervisor/{mission_key}/revoke",
              response_model=dict[str, Any])
    async def studio_revoke_mandate(
        mission_key: str,
        command: RevokeMandateCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        """Stop the studio starting anything else here, keeping what it did."""
        from .studio_supervisor import SupervisorRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            mandate = studio_supervisor(service).revoke(
                mission_key, actor=command.actor)
        except SupervisorRefused as exc:
            return studio_error(exc, language, "mandate_refused")
        return mandate.record(language)

    @app.get("/api/studio/first-run", response_model=dict[str, Any])
    async def studio_first_run_state(
        request: Request, service: Service, lang: str | None = None
    ) -> Any:
        """Whether work can start at all, and what is missing when it cannot."""
        return studio_first_run(service).report(
            language=chosen_language(request, lang),
        )

    @app.post("/api/studio/first-run/{source_key}", response_model=dict[str, Any])
    async def studio_connect_source(
        source_key: str,
        command: ConnectSourceCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        """Connect a way of getting work done. A local model is checked first."""
        from .studio_first_run import FirstRunRefused
        from .studio_workers import WorkerRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        wizard = studio_first_run(service)
        try:
            if command.kind == "local_model":
                source = wizard.offer_local_model(
                    source_key, name=command.name,
                    machines=studio_machines(service),
                    machine_key=command.machine_key, needed_gb=command.needed_gb,
                )
            else:
                source = wizard.connect(
                    source_key, kind=command.kind, name=command.name,
                    state=command.state, connected_by=command.connected_by,
                )
        except (FirstRunRefused, WorkerRefused) as exc:
            return studio_error(exc, language, "source_refused")
        return source.record(language)

    def studio_roster(service: Service):
        from .studio_roster import StudioRoster

        return StudioRoster(service.storage)

    @app.get("/api/studio/roster/{mission_key}", response_model=dict[str, Any])
    async def studio_roster_state(
        mission_key: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        return studio_roster(service).report(
            mission_key, language=chosen_language(request, lang),
        )

    @app.get("/api/studio/roster/{mission_key}/consequence/{role_id}",
             response_model=dict[str, Any])
    async def studio_role_consequence(
        mission_key: str,
        role_id: str,
        request: Request,
        service: Service,
        lang: str | None = None,
        concurrency: str = "sequential",
    ) -> Any:
        """What turning this role on would mean. Reading it turns nothing on."""
        from .studio_roster import RosterRefused

        language = chosen_language(request, lang)
        try:
            return studio_roster(service).consequence(
                mission_key, role_id, concurrency=concurrency,
            ).record(language)
        except (RosterRefused, ValueError) as exc:
            return studio_error(exc, language, "role_refused")

    @app.post("/api/studio/roster/{mission_key}/{role_id}", response_model=dict[str, Any])
    async def studio_change_role(
        mission_key: str,
        role_id: str,
        command: RosterCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
        action: str = "enable",
    ) -> Any:
        from .studio_roster import RosterRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        roster = studio_roster(service)
        try:
            if action == "disable":
                roster.disable(mission_key, role_id, actor=command.actor,
                               reason=command.reason)
                return roster.report(mission_key, language=language)
            if action == "model":
                return roster.assign_model(
                    mission_key, role_id, provider=command.provider,
                    model=command.model, actor=command.actor, reason=command.reason,
                ).record(language)
            consequence = roster.enable(
                mission_key, role_id, actor=command.actor, provider=command.provider,
                model=command.model, concurrency=command.concurrency,
                reason=command.reason,
            )
        except (RosterRefused, ValueError) as exc:
            return studio_error(exc, language, "role_refused")
        return {
            "consequence": consequence.record(language),
            **roster.report(mission_key, language=language),
        }

    @app.get("/api/studio/cost/{mission_key}", response_model=dict[str, Any])
    async def studio_cost(
        request: Request,
        service: Service,
        mission_key: str,
        lang: str | None = None,
        remaining_tasks: int = 0,
        stage: str = "",
        next_step: float = 0.0,
    ) -> Any:
        """Spent, reserved, the limit, and a forecast only where one is earned."""
        return studio_costs(service).report(
            mission_key, language=chosen_language(request, lang),
            remaining_tasks=remaining_tasks, stage_key=stage, next_step=next_step,
        )

    @app.post("/api/studio/cost/{mission_key}/limit", response_model=dict[str, Any])
    async def studio_set_limit(
        mission_key: str,
        command: SetLimitCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .studio_cost import CostRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            limit = studio_costs(service).set_limit(
                mission_key, amount=command.amount, actor=command.actor,
                unit=command.unit, reason=command.reason,
            )
        except CostRefused as exc:
            return studio_error(exc, language, "limit_refused")
        return limit.record()

    @app.get("/api/studio/paid-tools/{mission_key}", response_model=dict[str, Any])
    async def studio_paid_tools(
        mission_key: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        return paid_tools(service).report(
            mission_key, language=chosen_language(request, lang),
        )

    @app.post("/api/studio/paid-tools/{mission_key}", response_model=dict[str, Any])
    async def studio_ask_paid_tool(
        mission_key: str,
        command: PaidToolCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .localisation import Message

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        alternative = (
            Message(command.alternative_uk, command.alternative_en)
            if command.alternative_uk and command.alternative_en else None
        )
        choice = paid_tools(service).ask(
            mission_key, command.tool,
            reason=Message(command.reason_uk, command.reason_en),
            blocks=command.blocks, alternative=alternative, task_key=command.task_key,
        )
        return choice.record(language)

    @app.post("/api/studio/paid-tools/choices/{choice_id}", response_model=dict[str, Any])
    async def studio_answer_paid_tool(
        choice_id: int,
        command: PaidToolAnswerCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        """Take one of the four ways out. Declining says what it removes."""
        from .studio_paid_tools import PaidToolRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            answered, rebuild = paid_tools(service).answer(
                choice_id, choice=command.choice, actor=command.actor,
            )
        except KeyError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "unknown_choice", "message": str(exc).strip("'"),
                }},
            )
        except PaidToolRefused as exc:
            return studio_error(exc, language, "choice_refused")
        return {
            **answered.record(language),
            "rebuild": rebuild.record(language) if rebuild else None,
        }

    @app.get("/api/studio/slices/{mission_key}", response_model=dict[str, Any])
    async def studio_slices(
        mission_key: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        return stage_boundaries(service).report(
            mission_key, language=chosen_language(request, lang),
        )

    @app.post("/api/studio/slices/{mission_key}/{stage_key}", response_model=dict[str, Any])
    async def studio_declare_slice(
        mission_key: str,
        stage_key: str,
        command: DeclareSliceCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        """Declare how a stage ended: a slice that exists, or an honest reason."""
        from .localisation import Message
        from .studio_slices import SliceRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        boundaries = stage_boundaries(service)
        try:
            if command.outcome == "playable":
                boundary = boundaries.playable(
                    mission_key, stage_key,
                    project_key=command.project_key,
                    version_digest=command.version_digest,
                    declared_by=command.declared_by,
                )
            else:
                boundary = boundaries.nothing_to_test(
                    mission_key, stage_key,
                    reason=Message(command.reason_uk, command.reason_en),
                    declared_by=command.declared_by,
                )
        except (SliceRefused, ValueError) as exc:
            return studio_error(exc, language, "slice_refused")
        return boundary.record(language)

    @app.get("/api/studio/cycles/{mission_key}", response_model=dict[str, Any])
    async def studio_cycle_state(
        mission_key: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        return studio_cycles(service).report(
            mission_key, language=chosen_language(request, lang),
        )

    @app.post("/api/studio/cycles/{mission_key}/pause", response_model=dict[str, Any])
    async def studio_pause(
        mission_key: str,
        command: PauseCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .studio_cycles import CycleRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            paused = studio_cycles(service).pause(
                mission_key, actor=command.actor, finishing=command.finishing,
            )
        except CycleRefused as exc:
            return studio_error(exc, language, "pause_refused")
        return paused.record(language)

    @app.post("/api/studio/cycles/{mission_key}/comments", response_model=dict[str, Any])
    async def studio_comment(
        mission_key: str,
        command: CommentCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .studio_cycles import CycleRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            comment = studio_cycles(service).comment(
                mission_key, command.text, scope=command.scope,
                subject=command.subject, author=command.author,
            )
        except CycleRefused as exc:
            return studio_error(exc, language, "comment_refused")
        return comment.record()

    @app.post("/api/studio/cycles/{mission_key}/resume", response_model=dict[str, Any])
    async def studio_resume(
        mission_key: str,
        command: PauseCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .studio_cycles import CycleRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            resumed = studio_cycles(service).resume(mission_key, actor=command.actor)
        except CycleRefused as exc:
            return studio_error(exc, language, "resume_refused")
        return resumed.record(language)

    @app.get("/api/studio/plan/{mission_key}", response_model=dict[str, Any])
    async def studio_plan(
        mission_key: str, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        """The whole plan, by stage, in the words of the person who asked."""
        from .studio_backlog import BacklogRefused, from_mission

        language = chosen_language(request, lang)
        try:
            return from_mission(
                service.storage, mission_key,
                playable=stage_boundaries(service).playable_map(mission_key),
            ).record(language)
        except BacklogRefused as exc:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "unknown_mission", "message": exc.text(language),
                }},
            )

    @app.get("/api/studio/decisions", response_model=dict[str, Any])
    async def studio_decisions(
        request: Request,
        service: Service,
        mission: str = "",
        lang: str | None = None,
        limit: Limit = 50,
    ) -> Any:
        language = chosen_language(request, lang)
        return autonomy_journal(service).report(
            mission=mission, language=language, limit=limit,
        )

    @app.post("/api/studio/questions/{question_id}/answer", response_model=dict[str, Any])
    async def studio_answer(
        question_id: int,
        command: AnswerQuestionCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .localisation import LocalisedError
        from .studio_autonomy import AutonomyRefused

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            return autonomy_journal(service).answer(
                question_id, answer=command.answer, actor=command.actor,
            )
        except KeyError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "unknown_question", "message": str(exc).strip("'"),
                }},
            )
        except AutonomyRefused as exc:
            return JSONResponse(
                status_code=409,
                content={"error": {
                    "code": "answer_refused", "message": exc.text(language),
                }},
            )

    def feedback_journal(service: Service):
        from .game_feedback import FeedbackJournal

        return FeedbackJournal(service.storage)

    def feedback_note(project_key: str, command: FeedbackCommand):
        from .game_feedback import Attachment, Feedback, PlayedBuild

        return Feedback.create(
            build=PlayedBuild(
                project_key, command.played_version, command.engine, command.played_at,
            ),
            wish=command.wish,
            steps=command.steps,
            attachments=[
                Attachment(
                    item.kind, item.name, item.size_bytes, item.digest,
                    item.leaves_machine,
                )
                for item in command.attachments
            ],
        )

    def feedback_error(exc: Exception, language: str) -> JSONResponse:
        from .game_feedback import FeedbackRefused
        from .localisation import LocalisedError

        code = "feedback_refused" if isinstance(exc, FeedbackRefused) else "invalid_feedback"
        status = 404 if isinstance(exc, KeyError) else 409
        message = (
            exc.text(language) if isinstance(exc, LocalisedError)
            else str(exc).strip("'")
        )
        if isinstance(exc, KeyError):
            code = "unknown_feedback"
        return JSONResponse(
            status_code=status, content={"error": {"code": code, "message": message}}
        )

    @app.post("/api/games/{project_key}/feedback/preview", response_model=dict[str, Any])
    async def feedback_preview(
        project_key: str,
        command: FeedbackCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        """Exactly what would be sent, before anything is stored or sent."""
        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            return feedback_note(project_key, command).preview(language)
        except (ValueError, KeyError) as exc:
            return feedback_error(exc, language)

    @app.post("/api/games/{project_key}/feedback", response_model=dict[str, Any])
    async def feedback_record(
        project_key: str,
        command: FeedbackCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        try:
            note = feedback_note(project_key, command)
            identifier = feedback_journal(service).record(note)
        except (ValueError, KeyError) as exc:
            return feedback_error(exc, language)
        return {"feedback_id": identifier, **note.preview(language)}

    @app.get("/api/games/{project_key}/feedback", response_model=dict[str, Any])
    async def feedback_history(
        project_key: str, service: Service, limit: Limit = 20
    ) -> Any:
        return {"feedback": list(feedback_journal(service).history(project_key, limit=limit))}

    @app.get("/api/feedback/{feedback_id}", response_model=dict[str, Any])
    async def feedback_detail(
        feedback_id: int,
        request: Request,
        service: Service,
        lang: str | None = None,
        previous_version: str = "",
    ) -> Any:
        language = chosen_language(request, lang)
        journal = feedback_journal(service)
        try:
            note = journal.feedback(feedback_id)
            verdict = journal.verdict(feedback_id, previous_version=previous_version)
        except KeyError as exc:
            return feedback_error(exc, language)
        rows = service.storage.db.execute(
            "SELECT id,plan_json,accepted_by,accepted_at FROM game_feedback_plans"
            " WHERE feedback_id=? ORDER BY id", (int(feedback_id),),
        ).fetchall()
        return {
            "feedback_id": int(feedback_id),
            **note.preview(language),
            "plans": [
                {
                    "plan_id": int(row["id"]),
                    "accepted_by": row["accepted_by"],
                    "accepted_at": row["accepted_at"],
                    "plan": json.loads(row["plan_json"]),
                }
                for row in rows
            ],
            "verdict": verdict.record(language),
        }

    @app.post("/api/feedback/{feedback_id}/plans", response_model=dict[str, Any])
    async def feedback_plan(
        feedback_id: int,
        command: ChangePlanCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        """Record a proposed change. Proposing it is not accepting it."""
        from .game_feedback import Change, Cost, RequirementImpact, plan_change
        from .localisation import Message

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        journal = feedback_journal(service)
        try:
            note = journal.feedback(feedback_id)
            plan = plan_change(
                feedback=note,
                changes=[
                    Change(Message(item.uk, item.en), tuple(item.touches))
                    for item in command.changes
                ],
                impacts=[
                    RequirementImpact(item.requirement, item.impact, Message(item.uk, item.en))
                    for item in command.impacts
                ],
                added_scope=[Message(item.uk, item.en) for item in command.added_scope],
                cost=Cost(
                    command.cost.amount, command.cost.unit,
                    Message(command.cost.basis.uk, command.cost.basis.en)
                    if command.cost.basis else None,
                ),
                current_version=command.current_version,
            )
            plan_id = journal.record_plan(feedback_id, plan)
        except (ValueError, KeyError) as exc:
            return feedback_error(exc, language)
        return {"plan_id": plan_id, **plan.record(language)}

    @app.post("/api/feedback/plans/{plan_id}/accept", response_model=dict[str, Any])
    async def feedback_accept(
        plan_id: int,
        command: AcceptPlanCommand,
        request: Request,
        service: Service,
        confirmation: Confirmation = None,
        lang: str | None = None,
    ) -> Any:
        from .game_feedback import ChangePlan, Cost, accept_plan
        from .localisation import Message

        _require_confirmation(command, confirmation)
        language = chosen_language(request, lang)
        row = service.storage.db.execute(
            "SELECT feedback_id,plan_json,cost_amount,cost_unit,accepted_by"
            " FROM game_feedback_plans WHERE id=?", (int(plan_id),),
        ).fetchone()
        if row is None:
            return feedback_error(KeyError(f"Unknown plan {plan_id}"), language)
        stored = json.loads(row["plan_json"])
        # The stored plan decides what has to be accepted; the request only says
        # what the person accepted, so a plan cannot talk itself into being free.
        plan = ChangePlan(
            feedback_digest=stored["feedback"],
            changes=(), impacts=(),
            added_scope=tuple(
                Message(item, item) for item in stored.get("added_scope", [])
            ),
            cost=Cost(float(row["cost_amount"]), row["cost_unit"]),
            applies_to=stored.get("applies_to", ""),
            accepted_by=row["accepted_by"],
        )
        try:
            accepted = accept_plan(
                plan, actor=command.actor,
                accept_cost=command.accept_cost, accept_scope=command.accept_scope,
            )
            feedback_journal(service).accept(
                plan_id, actor=accepted.accepted_by, at=accepted.accepted_at,
            )
        except (ValueError, KeyError) as exc:
            return feedback_error(exc, language)
        except sqlite3.IntegrityError:
            return JSONResponse(
                status_code=409,
                content={"error": {
                    "code": "already_accepted",
                    "message": "A plan may only gain its acceptance once",
                }},
            )
        return {
            "plan_id": int(plan_id),
            "accepted_by": accepted.accepted_by,
            "accepted_at": accepted.accepted_at,
        }

    def work_reader(service: Service):
        from .work_status_store import WorkStatusReader

        return WorkStatusReader(service.storage)

    @app.get("/api/work/runs", response_model=dict[str, Any])
    async def work_runs(
        request: Request, service: Service, lang: str | None = None, limit: Limit = 20
    ) -> Any:
        language = chosen_language(request, lang)
        return {
            "language": language,
            "runs": list(work_reader(service).open_runs(limit=limit)),
        }

    @app.get("/api/work/runs/{run_id}", response_model=dict[str, Any])
    async def work_run(
        run_id: int, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        language = chosen_language(request, lang)
        try:
            return work_reader(service).report(run_id, language=language)
        except KeyError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "unknown_run", "message": str(exc).strip("'")}},
            )

    @app.get("/api/work/runs/{run_id}/stop-plan", response_model=dict[str, Any])
    async def work_stop_plan(
        run_id: int, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        """What a stop would stop. Reading the plan changes nothing."""
        language = chosen_language(request, lang)
        try:
            return work_reader(service).stop_plan(run_id).record(language)
        except KeyError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "unknown_run", "message": str(exc).strip("'")}},
            )

    @app.get("/api/work/runs/{run_id}/after-restart", response_model=dict[str, Any])
    async def work_after_restart(
        run_id: int, request: Request, service: Service, lang: str | None = None
    ) -> Any:
        language = chosen_language(request, lang)
        try:
            return work_reader(service).after_restart(run_id).record(language)
        except KeyError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "unknown_run", "message": str(exc).strip("'")}},
            )

    @app.post("/api/github/preview", response_model=dict[str, Any])
    async def github_preview(
        command: GitHubPreviewCommand,
        service: Service,
        confirmation: Confirmation = None,
    ) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        return service.preview_github_sync(
            command.repo, command.backlog_path, command.existing_issues
        )

    @app.get("/api/integrations", response_model=list[IntegrationStatus])
    async def integrations(service: Service) -> list[IntegrationStatus]:
        provider_states = await provider_snapshot()
        unhealthy = sum(item.status not in {"ready", "disabled"} for item in provider_states)
        return [
            IntegrationStatus(
                name="providers",
                status="ready" if unhealthy == 0 else "degraded",
                detail=f"{unhealthy} configured providers unavailable or unhealthy",
            ),
            IntegrationStatus(
                name="github",
                status="unconfigured",
                detail="Repository context is supplied only to explicit dry-run sync requests",
            ),
        ]

    @app.get("/api/control/actions", response_model=list[dict[str, Any]])
    async def control_actions(tenant_id: str, request: Request) -> list[dict[str, Any]]:
        principal = request.state.local_principal
        if "*" not in principal.tenants and tenant_id not in principal.tenants:
            raise HTTPException(403, "Tenant scope required")
        storage = SQLiteStorage(database)
        try:
            return HumanControlPlaneService(storage).list_actions(tenant_id)
        finally:
            storage.close()

    @app.post("/api/control/actions", response_model=dict[str, Any], status_code=201)
    async def control_action(command: ControlActionCommand, request: Request, confirmation: Confirmation = None) -> dict[str, Any]:
        _require_confirmation(command, confirmation)
        principal = request.state.local_principal
        if (command.actor != principal.actor or command.role != principal.role
                or ("*" not in principal.tenants and command.tenant_id not in principal.tenants)):
            raise HTTPException(403, "Authenticated action scope does not match")
        storage = SQLiteStorage(database)
        try:
            return HumanControlPlaneService(storage).act(**command.model_dump(exclude={"confirmed"}))
        finally:
            storage.close()

    @app.get('/operations', include_in_schema=False)
    async def operations_shell(request: Request) -> FileResponse:
        if request.state.local_principal is None and isinstance(access, SsoAccess):
            return await login_shell()
        return FileResponse(static_directory / ('operations.html' if request.state.local_principal else 'login.html'))

    @app.get("/settings", include_in_schema=False)
    async def settings_shell(request: Request, lang: str | None = None) -> FileResponse:
        from .localisation import LANGUAGE_COOKIE, normalise

        if not request.state.local_principal:
            return FileResponse(static_directory / "login.html")
        response = FileResponse(static_directory / "settings.html")
        if lang:
            # Remember the explicit choice so the next page opens in it.
            response.set_cookie(
                LANGUAGE_COOKIE, normalise(lang), max_age=31_536_000,
                samesite="lax", httponly=False,
            )
        return response

    @app.get("/studio", include_in_schema=False)
    async def studio_shell(request: Request, lang: str | None = None) -> FileResponse:
        from .localisation import LANGUAGE_COOKIE, normalise

        if not request.state.local_principal:
            return FileResponse(static_directory / "login.html")
        response = FileResponse(static_directory / "studio.html")
        if lang:
            response.set_cookie(
                LANGUAGE_COOKIE, normalise(lang), max_age=31_536_000,
                samesite="lax", httponly=False,
            )
        return response

    @app.get("/work", include_in_schema=False)
    async def work_shell(request: Request, lang: str | None = None) -> FileResponse:
        from .localisation import LANGUAGE_COOKIE, normalise

        if not request.state.local_principal:
            return FileResponse(static_directory / "login.html")
        response = FileResponse(static_directory / "work.html")
        if lang:
            response.set_cookie(
                LANGUAGE_COOKIE, normalise(lang), max_age=31_536_000,
                samesite="lax", httponly=False,
            )
        return response

    @app.get("/", include_in_schema=False)
    async def dashboard_shell(request: Request) -> FileResponse:
        if request.state.local_principal is None and isinstance(access, SsoAccess):
            return await login_shell()
        return FileResponse(static_directory / ("index.html" if request.state.local_principal else "login.html"))

    return app
