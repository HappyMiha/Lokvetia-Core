"""Shared local studio intake used by Core and downstream creator products."""
from contextlib import closing
import hashlib
import socket
from pathlib import Path

from .autonomous_mission import AutonomousMissionConfiguration
from .environment_model_probe import model_inventory, provider_profile
from .local_games import local_games_lock
from .local_role_qualification import PROFILE, ROLES
from .machine_identity import require_a_build_machine
from .mission_intake import AutonomousMissionIntakeService
from .storage import SQLiteStorage
from .studio_first_run import FirstRun
from .studio_supervisor import Supervisor
from .studio_workers import StudioMachines


def checked_local_source(storage, workspace):
    """A manually marked source or a model that only fits VRAM is insufficient."""
    machine = require_a_build_machine("Local studio")
    if not machine.is_the_users_computer:
        raise ValueError("local_worker_required")
    provider_profile(workspace)
    source = FirstRun(storage).source("ollama")
    if (not source.usable or source.kind != "local_model"
            or source.machine_key != "local-" + socket.gethostname().lower()):
        raise ValueError("local_source_not_qualified")
    reports = StudioMachines(storage).reports(machine_key=source.machine_key)
    evidence = next((row for row in reports if row["kind"] == "local_model_qualification"), None)
    if evidence is None or not evidence.get("results"):
        raise ValueError("local_source_not_qualified")
    summary = evidence.get("summary", {})
    if (summary.get("scope") != "local-role-contract-smoke-only"
            or summary.get("profile_sha256") != hashlib.sha256(PROFILE.read_bytes()).hexdigest()
            or {row.get("role") for row in evidence["results"]} != set(ROLES)
            or not all(row.get("passed") is True and row.get("cli_effective_model") == "local:" + source.name
                       for row in evidence["results"])):
        raise ValueError("local_source_not_qualified")
    if model_inventory("local:" + source.name) != summary.get("model_digest"):
        raise ValueError("local_model_changed")
    return source


def create_local_game(database, workspace, *, actor, command_id, title, idea, runner):
    if not actor.strip() or not title.strip() or not idea.strip():
        raise ValueError("Name and idea are required")
    with local_games_lock(database):
        with closing(SQLiteStorage(database)) as storage:
            source = checked_local_source(storage, workspace)
            model = "local:" + source.name
            key = hashlib.sha256((actor + ":" + str(command_id)).encode()).hexdigest()
            created = AutonomousMissionIntakeService(storage).create_from_text(
                name=title, specification=idea, actor=actor,
                mission_owner=actor, command_id="studio-create:" + key,
                source_name="idea.txt", provenance="studio-create",
                configuration=AutonomousMissionConfiguration(
                    repository_path=str(Path(workspace).resolve()), default_model=model,
                    role_models={role: model for role in ROLES}, local_provider_ids=("ollama",)))
            mission = created.mission
            supervisor = Supervisor(storage)
            if not supervisor.mandates(mission.mission_key):
                supervisor.grant(mission.mission_key, steps=("plan",),
                                 granted_by=actor, ceiling=0, reason="Create game: local planning")
                launch = True
            else:
                launch = not supervisor.history(mission.mission_key)
    if launch:
        runner.submit(mission.id)
    return {"mission_id": mission.id, "mission_key": mission.mission_key,
            "status": runner.status(mission.id),
            "url": "/studio?mission=" + mission.mission_key, "scope": "local_planning"}
