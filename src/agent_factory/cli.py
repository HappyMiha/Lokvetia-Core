"""Lokvetia Core CLI, including the compatible agent-factory entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .backlog import BacklogProposal, diff_issues, issue_operations, load_backlog
from .github import GitHubClient
from .storage import SQLiteStorage
from .studio_supervisor import STEPS as SUPERVISOR_STEPS


def _version() -> str:
    try:
        return version("agent-factory-orchestrator")
    except ImportError:  # The source checkout has no installed distribution metadata.
        return "0.1.0"


def parser() -> argparse.ArgumentParser:
    executable = Path(sys.argv[0]).stem
    command = argparse.ArgumentParser(
        prog=executable if executable in {"lokvetia", "agent-factory"} else "lokvetia",
        description="Lokvetia Core: provider-neutral orchestration for traceable, human-approved agent delivery.",
    )
    command.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    command.add_argument(
        "--workspace",
        default=os.getenv("AGENT_FACTORY_WORKSPACE", "."),
        help="Project workspace used for state and configuration (default: current directory).",
    )
    command.add_argument(
        "--db",
        default=os.getenv("AGENT_FACTORY_DB"),
        help="SQLite path (default: WORKSPACE/.agent-factory/state.db).",
    )
    sub = command.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create the generic example project and work item.")
    sub.add_parser("bootstrap", help="Alias for init.")
    sub.add_parser("demo", help="Run the offline deterministic delivery demo.")
    web = sub.add_parser("web", help="Start the loopback-only Local Control Center API.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", default=8765, type=int)
    web.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="Open the Local Control Center in the default browser after startup.",
    )

    project = sub.add_parser("project").add_subparsers(dest="action", required=True)
    project_init = project.add_parser("init")
    project_init.add_argument("--name", required=True)
    project_init.add_argument("--description", default="")
    project.add_parser("list")

    work_item = sub.add_parser("work-item").add_subparsers(dest="action", required=True)
    create_item = work_item.add_parser("create")
    create_item.add_argument("--project-id", required=True, type=int)
    create_item.add_argument("--title", required=True)
    create_item.add_argument("--description", required=True)
    create_item.add_argument("--kind", default="task")
    create_item.add_argument("--acceptance", action="append", required=True)
    list_items = work_item.add_parser("list")
    list_items.add_argument("--project-id", type=int)
    show_item = work_item.add_parser("show")
    show_item.add_argument("task_id", type=int)

    providers = sub.add_parser("providers").add_subparsers(
        dest="provider_action", required=True
    )
    providers.add_parser("status")
    providers.add_parser("gates")
    providers.add_parser("reconcile")
    request = providers.add_parser("request")
    request.add_argument("provider")
    request.add_argument("--agent", required=True)
    request.add_argument("--task-id", required=True, type=int)
    for action in ("approve", "reject", "cancel"):
        gate = providers.add_parser(action)
        gate.add_argument("gate_id", type=int)
        gate.add_argument("--note", default="")
    invoke = providers.add_parser("invoke")
    invoke.add_argument("gate_id", type=int)

    env = sub.add_parser("env").add_subparsers(dest="action", required=True)
    env.add_parser("check")

    agents = sub.add_parser("agents").add_subparsers(dest="action", required=True)
    agents.add_parser("list")
    for action in ("enable", "disable"):
        agent = agents.add_parser(action)
        agent.add_argument("agent_id")
    replace = agents.add_parser("replace")
    replace.add_argument("agent_id")
    replace.add_argument("--provider", required=True)
    replace.add_argument(
        "--model",
        default="",
        help="Stable model identity used by independent-review routing.",
    )

    reviews = sub.add_parser("reviews").add_subparsers(
        dest="review_action", required=True
    )
    review_list = reviews.add_parser("list")
    review_list.add_argument("--run-id", type=int)
    review_list.add_argument("--limit", type=int, default=100)

    backlog = sub.add_parser("backlog").add_subparsers(dest="action", required=True)
    validate = backlog.add_parser("validate")
    validate.add_argument("--path", required=True)
    import_items = backlog.add_parser("import")
    import_items.add_argument("--path", required=True)
    import_items.add_argument("--project-id", required=True, type=int)
    sync = backlog.add_parser("sync")
    sync.add_argument("--path")
    sync.add_argument("--repo")
    sync.add_argument("--existing-json")
    sync.add_argument("--apply", action="store_true")
    sync.add_argument("--plan-id", type=int)
    sync.add_argument("--gate-id", type=int)
    backlog.add_parser("gates")
    for action in ("approve", "reject"):
        gate = backlog.add_parser(action)
        gate.add_argument("gate_id", type=int)
        gate.add_argument("--note", default="")

    task = sub.add_parser("task").add_subparsers(dest="action", required=True)
    claim = task.add_parser("claim")
    claim.add_argument("task_id", type=int)
    claim.add_argument("--agent", default="coding-worker-codex")
    run_task = task.add_parser("run")
    run_task.add_argument("task_id", type=int)
    run_task.add_argument("--workflow", default="delivery")
    review = task.add_parser("review")
    review.add_argument("task_id", type=int)
    review.add_argument("--artifact-id", type=int)
    review.add_argument("--decision", choices=["approved", "rejected"])
    review.add_argument("--note", default="")

    workflow = sub.add_parser("workflow").add_subparsers(dest="action", required=True)
    workflow_run = workflow.add_parser("run")
    workflow_run.add_argument("--task-id", type=int, required=True)
    workflow_run.add_argument("--workflow", default="delivery")
    workflow_run.add_argument("--mode", choices=["simulation", "live"], default="simulation")

    approvals = sub.add_parser("approvals").add_subparsers(dest="action", required=True)
    approvals.add_parser("list")
    for action in ("approve", "reject"):
        gate = approvals.add_parser(action)
        gate.add_argument("gate_id", type=int)
        gate.add_argument("--note", default="")

    audit = sub.add_parser("audit").add_subparsers(dest="action", required=True)
    audit_list = audit.add_parser("list")
    audit_list.add_argument("--limit", type=int, default=100)

    godot = sub.add_parser(
        "godot", help="Godot game pack templates, project preview, and engine build."
    ).add_subparsers(dest="action", required=True)
    godot.add_parser("templates", help="List the templates shipped by the Godot pack.")
    godot_health = godot.add_parser("health", help="Qualify the installed Godot engine.")
    godot_health.add_argument("--executable", action="append")
    godot_plan = godot.add_parser("plan", help="Preview the files a template would write.")
    godot_plan.add_argument("--template", required=True)
    godot_plan.add_argument("--path", required=True)
    godot_apply = godot.add_parser("apply", help="Write the previewed template files.")
    godot_apply.add_argument("--template", required=True)
    godot_apply.add_argument("--path", required=True)
    godot_apply.add_argument(
        "--approve-overwrite",
        action="append",
        help="Project-relative file the human approves for overwrite; repeatable.",
    )
    godot_apply.add_argument("--actor", help="Human approver recorded for overwrites.")
    godot_build = godot.add_parser(
        "build", help="Import, parse, run headless, and export a real artifact."
    )
    godot_build.add_argument("--template", required=True)
    godot_build.add_argument("--path", required=True)
    godot_build.add_argument("--preset", required=True)
    godot_build.add_argument("--output", required=True)
    godot_build.add_argument("--executable", action="append")
    godot_build.add_argument("--commit", default="unknown")
    godot_build.add_argument("--frames", type=int, default=180)

    playable = sub.add_parser(
        "playable", help="The verified game version a player can launch right now."
    ).add_subparsers(dest="action", required=True)
    playable_current = playable.add_parser("current", help="Show the playable version.")
    playable_current.add_argument("--project", required=True)
    playable_history = playable.add_parser("history", help="List recorded versions.")
    playable_history.add_argument("--project", required=True)
    playable_history.add_argument("--limit", type=int, default=20)
    playable_promote = playable.add_parser(
        "promote", help="Offer an engine build record; only a verified build is promoted."
    )
    playable_promote.add_argument("--project", required=True)
    playable_promote.add_argument("--engine", default="godot")
    playable_promote.add_argument(
        "--build", required=True, help="Path to the JSON build record to promote."
    )
    playable_promote.add_argument("--command-id", required=True)
    playable_promote.add_argument("--actor", required=True)
    playable_restore = playable.add_parser(
        "restore", help="Preview and restore an earlier verified version."
    )
    playable_restore.add_argument("--project", required=True)
    playable_restore.add_argument("--version", required=True)
    playable_restore.add_argument("--branch", required=True)
    playable_restore.add_argument("--command-id")
    playable_restore.add_argument("--actor")
    playable_restore.add_argument(
        "--confirm", action="store_true",
        help="Apply the previewed restore; without it only the preview is printed.",
    )
    playable_verify = playable.add_parser(
        "verify", help="Re-check the stored artifact against its recorded build."
    )
    playable_verify.add_argument("--project", required=True)

    assets = sub.add_parser(
        "assets", help="Asset provenance, safe import, rights and hardware budgets."
    ).add_subparsers(dest="action", required=True)
    assets_inspect = assets.add_parser(
        "inspect", help="Report an archive's members; nothing is extracted."
    )
    assets_inspect.add_argument("--archive", required=True)
    assets_list = assets.add_parser("list", help="List imported assets and their rights.")
    assets_list.add_argument("--path", required=True)
    assets_rights = assets.add_parser(
        "rights", help="Check whether the project may export, share, publish or sell."
    )
    assets_rights.add_argument("--path", required=True)
    assets_rights.add_argument("--operation", default="export")
    assets_add = assets.add_parser(
        "add", help="Preview and import one asset with recorded provenance."
    )
    assets_add.add_argument("--path", required=True)
    assets_add.add_argument("--file", required=True)
    assets_add.add_argument("--name", required=True, help="Project-relative destination.")
    assets_add.add_argument("--licence", default="unknown")
    assets_add.add_argument("--source", default="")
    assets_add.add_argument("--attribution", default="")
    assets_add.add_argument("--budget", default="baseline-pc")
    assets_add.add_argument("--approve-overwrite", action="store_true")
    assets_add.add_argument("--actor", default="")
    assets_add.add_argument(
        "--confirm", action="store_true",
        help="Apply the previewed import; without it only the preview is printed.",
    )

    export = sub.add_parser(
        "export", help="Package a verified version, and share it as a separate act."
    ).add_subparsers(dest="action", required=True)
    export.add_parser("targets", help="List export targets and why any are unavailable.")
    for export_action in ("preflight", "build"):
        # A distinct name: `command` is the root parser this function returns.
        export_parser = export.add_parser(
            export_action,
            help="Check a package before building." if export_action == "preflight"
            else "Build the package after a passing check.",
        )
        export_parser.add_argument("--project", required=True, help="Playable project key.")
        export_parser.add_argument("--path", required=True, help="Game project folder.")
        export_parser.add_argument("--target", required=True)
        export_parser.add_argument("--exclude", action="append")
        if export_action == "build":
            export_parser.add_argument("--output", required=True)
            export_parser.add_argument("--name", default="Game")
    export_share = export.add_parser(
        "share", help="Preview a publication; publishing needs an explicit decision."
    )
    export_share.add_argument("--bundle", required=True)
    export_share.add_argument("--destination", required=True)
    export_share.add_argument("--visibility", default="private-link")
    export_share.add_argument("--actor", default="")
    export_share.add_argument("--confirm", action="store_true")
    export_share.add_argument("--cancel", action="store_true")

    support = sub.add_parser(
        "support", help="Diagnostics you can read in full before sending them."
    ).add_subparsers(dest="action", required=True)
    support.add_parser("categories", help="What can be collected, and what never is.")
    support_preview = support.add_parser(
        "preview", help="Show exactly what a bundle would contain."
    )
    support_preview.add_argument(
        "--include", action="append",
        help="Opt in to a category; repeatable. Nothing optional is collected without it.",
    )
    support_preview.add_argument("--show", help="Print one collected item in full.")
    support_bundle = support.add_parser(
        "bundle", help="Package the previewed diagnostics."
    )
    support_bundle.add_argument("--include", action="append")
    support_bundle.add_argument("--output", required=True)
    support_bundle.add_argument("--actor", required=True)

    update = sub.add_parser(
        "update", help="Check an application update before anything moves."
    ).add_subparsers(dest="action", required=True)
    update_plan = update.add_parser(
        "plan", help="Verify the signature and compatibility of an update manifest."
    )
    update_plan.add_argument("--manifest", required=True)
    update_plan.add_argument(
        "--project-pin", action="append",
        help="key:engine:version[:model:version]; repeatable.",
    )
    update_plan.add_argument(
        "--separate-plan", default="",
        help="Name of the approved plan that covers moving a pinned engine or model.",
    )
    update_plan.add_argument("--trust-key", help="Trust-root key id for verification.")
    update_plan.add_argument("--trust-secret", help="Trust-root material (testing).")
    uninstall_command = sub.add_parser(
        "uninstall", help="Preview what an uninstall would remove. It removes nothing."
    ).add_subparsers(dest="action", required=True)
    uninstall_preview = uninstall_command.add_parser("plan")
    uninstall_preview.add_argument("--project", action="append")
    uninstall_preview.add_argument("--remove-projects", action="store_true")

    levels = sub.add_parser(
        "levels", help="Declared support levels and what the evidence permits saying."
    ).add_subparsers(dest="action", required=True)
    for levels_action in ("list", "show", "scope"):
        levels_parser = levels.add_parser(levels_action)
        levels_parser.add_argument(
            "--evidence", help="Path to a capability evidence document."
        )
        if levels_action == "show":
            levels_parser.add_argument("--level", required=True)
        if levels_action == "scope":
            levels_parser.add_argument("--feature", action="append", required=True)
            levels_parser.add_argument("--dimension", default="2d")
            levels_parser.add_argument("--engine", default="godot")
            levels_parser.add_argument("--description", default="a game")

    unity = sub.add_parser(
        "unity", help="Unity setup, project compatibility and editor qualification."
    ).add_subparsers(dest="action", required=True)
    unity.add_parser(
        "catalogue", help="Pinned editors and modules, and the steps that are yours."
    )
    unity_project = unity.add_parser(
        "project", help="Read what a Unity project on disk needs. No editor is run."
    )
    unity_project.add_argument("--path", required=True)
    unity_project.add_argument("--target", default="StandaloneWindows64")
    unity_project.add_argument("--editor", default="")
    unity_health = unity.add_parser(
        "health", help="Qualify an installed editor. The licence state is yours to give."
    )
    unity_health.add_argument("--executable", action="append")
    unity_health.add_argument(
        "--licence", default="unknown",
        help="active|inactive|expired|unknown, as Unity Hub reports it to you.",
    )

    settings_command = sub.add_parser(
        "settings", help="See, check and change every setting without editing files."
    ).add_subparsers(dest="action", required=True)
    settings_show = settings_command.add_parser("show", help="Current values and origins.")
    settings_show.add_argument("--section")
    settings_show.add_argument("--language", default="uk", choices=("uk", "en"))
    settings_verify = settings_command.add_parser("verify", help="Check a section.")
    settings_verify.add_argument("--section")
    settings_verify.add_argument("--language", default="uk", choices=("uk", "en"))
    settings_set = settings_command.add_parser("set", help="Change one setting.")
    settings_set.add_argument("--key", required=True)
    settings_set.add_argument("--value", required=True)
    settings_set.add_argument("--actor", required=True)
    settings_set.add_argument("--reason", default="")
    settings_set.add_argument(
        "--acknowledge", action="store_true",
        help="Confirm the declared consequence of a sensitive setting.",
    )
    settings_reset = settings_command.add_parser("reset", help="Return one setting to its default.")
    settings_reset.add_argument("--key", required=True)
    settings_reset.add_argument("--actor", required=True)
    settings_reset.add_argument("--reason", default="")
    settings_reset.add_argument("--acknowledge", action="store_true")
    settings_history = settings_command.add_parser("history", help="Who changed what.")
    settings_history.add_argument("--key")
    settings_history.add_argument("--limit", type=int, default=50)

    download = sub.add_parser(
        "download", help="Check a downloaded release against what was published."
    ).add_subparsers(dest="action", required=True)
    download_verify = download.add_parser(
        "verify", help="Verify every file against its published checksum."
    )
    download_verify.add_argument("--manifest", required=True)
    download_verify.add_argument("--directory", default="")
    download_verify.add_argument("--language", default="uk", choices=("uk", "en"))
    download_show = download.add_parser(
        "show", help="What the release says about itself."
    )
    download_show.add_argument("--manifest", required=True)
    download_show.add_argument(
        "--platform", default="", choices=("", "windows", "macos", "linux"))
    download_show.add_argument("--language", default="uk", choices=("uk", "en"))

    studio_command = sub.add_parser(
        "studio", help="What the studio decides on its own, and what it still asks."
    ).add_subparsers(dest="action", required=True)
    studio_gates = studio_command.add_parser("gates", help="Every gate and who answers it.")
    studio_gates.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_log = studio_command.add_parser("decisions", help="What was decided, and why.")
    studio_log.add_argument("--mission", default="")
    studio_log.add_argument("--limit", type=int, default=50)
    studio_log.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_plan = studio_command.add_parser(
        "plan", help="The whole plan of a game, by stage, in human words."
    )
    studio_plan.add_argument("--mission", required=True)
    studio_plan.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_slice = studio_command.add_parser(
        "slice", help="Declare how a stage ended: playable, or nothing to test."
    )
    studio_slice.add_argument("--mission", required=True)
    studio_slice.add_argument("--stage", required=True)
    studio_slice.add_argument("--project")
    studio_slice.add_argument("--version")
    studio_slice.add_argument("--nothing-uk", help="Why there is nothing to test yet.")
    studio_slice.add_argument("--nothing-en", help="The same reason in English.")
    studio_slice.add_argument("--actor", default="")
    studio_slice.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_slices = studio_command.add_parser("slices", help="What is playable, and when.")
    studio_slices.add_argument("--mission", required=True)
    studio_slices.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_pause = studio_command.add_parser(
        "pause", help="Stop handing out new tasks. Work in flight finishes."
    )
    studio_pause.add_argument("--mission", required=True)
    studio_pause.add_argument("--actor", required=True)
    studio_pause.add_argument("--finishing", action="append", default=[])
    studio_pause.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_comment = studio_command.add_parser("comment", help="Say what you want changed.")
    studio_comment.add_argument("--mission", required=True)
    studio_comment.add_argument("--text", required=True)
    studio_comment.add_argument("--scope", default="game", choices=("game", "stage", "task"))
    studio_comment.add_argument("--subject", default="")
    studio_comment.add_argument("--author", default="")
    studio_resume = studio_command.add_parser("resume", help="Continue, with the comments.")
    studio_resume.add_argument("--mission", required=True)
    studio_resume.add_argument("--actor", required=True)
    studio_resume.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_history = studio_command.add_parser("cycles", help="Every cycle and its comments.")
    studio_history.add_argument("--mission", required=True)
    studio_history.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_cost = studio_command.add_parser(
        "cost", help="Spent, reserved, the limit and an earned forecast."
    )
    studio_cost.add_argument("--mission", required=True)
    studio_cost.add_argument("--remaining-tasks", type=int, default=0)
    studio_cost.add_argument("--stage", default="")
    studio_cost.add_argument("--next-step", type=float, default=0.0)
    studio_cost.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_limit = studio_command.add_parser("limit", help="Set the spending limit.")
    studio_limit.add_argument("--mission", required=True)
    studio_limit.add_argument("--amount", type=float, required=True)
    studio_limit.add_argument("--actor", required=True)
    studio_limit.add_argument("--unit", default="USD")
    studio_limit.add_argument("--reason", default="")
    studio_paid = studio_command.add_parser(
        "paid-tools", help="Paid tools in the way, and what was decided."
    )
    studio_paid.add_argument("--mission", required=True)
    studio_paid.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_choose = studio_command.add_parser(
        "choose", help="Take one of the four ways past a paid tool."
    )
    studio_choose.add_argument("--choice", type=int, required=True)
    studio_choose.add_argument(
        "--way", required=True,
        choices=("own_subscription", "buy_through_platform", "free_alternative", "decline"),
    )
    studio_choose.add_argument("--actor", required=True)
    studio_choose.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_team = studio_command.add_parser("team", help="Who is in the studio.")
    studio_team.add_argument("--mission", required=True)
    studio_team.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_add = studio_command.add_parser(
        "add-role", help="Turn a role on, after seeing what it costs."
    )
    studio_add.add_argument("--mission", required=True)
    studio_add.add_argument("--role", required=True)
    studio_add.add_argument("--actor", required=True)
    studio_add.add_argument("--provider", default="")
    studio_add.add_argument("--model", default="")
    studio_add.add_argument("--concurrency", default="sequential",
                            choices=("sequential", "parallel"))
    studio_add.add_argument("--reason", default="")
    studio_add.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_drop = studio_command.add_parser("drop-role", help="Turn a role off.")
    studio_drop.add_argument("--mission", required=True)
    studio_drop.add_argument("--role", required=True)
    studio_drop.add_argument("--actor", required=True)
    studio_drop.add_argument("--reason", default="")
    studio_drop.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_model = studio_command.add_parser(
        "role-model", help="Give one role its own provider and model."
    )
    studio_model.add_argument("--mission", required=True)
    studio_model.add_argument("--role", required=True)
    studio_model.add_argument("--provider", required=True)
    studio_model.add_argument("--model", required=True)
    studio_model.add_argument("--actor", required=True)
    studio_model.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_machines = studio_command.add_parser(
        "machines", help="Which machines exist, and what each may answer for."
    )
    studio_machines.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_register = studio_command.add_parser(
        "register-machine", help="Offer a machine for builds."
    )
    studio_register.add_argument("--machine", required=True)
    studio_register.add_argument("--name", required=True)
    studio_register.add_argument(
        "--kind", required=True, choices=("cloud_worker", "this_pc", "web_container"))
    studio_register.add_argument("--can", action="append", default=[])
    studio_register.add_argument("--video-memory-gb", type=float, default=0.0)
    studio_register.add_argument("--actor", default="")
    studio_register.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_start = studio_command.add_parser(
        "first-run", help="Whether development can start, and what is missing."
    )
    studio_start.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_connect = studio_command.add_parser(
        "connect", help="Connect a subscription, or offer a local model."
    )
    studio_connect.add_argument("--source", required=True)
    studio_connect.add_argument(
        "--kind", required=True,
        choices=("own_subscription", "platform_subscription", "local_model"))
    studio_connect.add_argument("--name", required=True)
    studio_connect.add_argument(
        "--state", default="unverified",
        choices=("verified", "unverified", "unavailable"))
    studio_connect.add_argument("--machine", default="")
    studio_connect.add_argument("--needed-gb", type=float, default=0.0)
    studio_connect.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_mandate = studio_command.add_parser(
        "mandate", help="Let the studio run this game without asking again."
    )
    studio_mandate.add_argument("--mission", required=True)
    studio_mandate.add_argument("--actor", required=True)
    studio_mandate.add_argument(
        "--allow", action="append", default=[], choices=SUPERVISOR_STEPS,
        help="A step the studio may take on its own. Repeat for more than one.")
    studio_mandate.add_argument("--ceiling", type=float, default=None,
                                help="How much it may spend unattended.")
    studio_mandate.add_argument("--unit", default="USD")
    studio_mandate.add_argument("--hours", type=float, default=24.0)
    studio_mandate.add_argument("--reason", default="")
    studio_mandate.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_revoke = studio_command.add_parser(
        "revoke-mandate", help="Stop the studio starting anything else here."
    )
    studio_revoke.add_argument("--mission", required=True)
    studio_revoke.add_argument("--actor", required=True)
    studio_revoke.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_run = studio_command.add_parser(
        "run", help="Move this game as far as the mandate allows."
    )
    studio_run.add_argument("--mission", required=True)
    studio_run.add_argument("--mission-id", type=int, required=True,
                            help="The Core mission this game is.")
    studio_run.add_argument("--once", action="store_true",
                            help="One step, then stop and report.")
    studio_run.add_argument("--passes", type=int, default=8)
    studio_run.add_argument("--next-step-cost", type=float, default=0.0)
    studio_run.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_steps = studio_command.add_parser(
        "steps", help="What the studio did here on its own, and why it stopped."
    )
    studio_steps.add_argument("--mission", required=True)
    studio_steps.add_argument("--limit", type=int, default=20)
    studio_steps.add_argument("--language", default="uk", choices=("uk", "en"))
    studio_answer = studio_command.add_parser("answer", help="Answer one open question.")
    studio_answer.add_argument("--question", type=int, required=True)
    studio_answer.add_argument("--answer", required=True)
    studio_answer.add_argument("--actor", required=True)

    feedback_command = sub.add_parser(
        "feedback", help="Turn a sentence after playing into the next version."
    ).add_subparsers(dest="action", required=True)
    feedback_preview = feedback_command.add_parser(
        "preview", help="Show exactly what would be sent. Sends nothing."
    )
    feedback_add = feedback_command.add_parser("add", help="Record a note about a build.")
    for parser_with_note in (feedback_preview, feedback_add):
        parser_with_note.add_argument("--project", required=True)
        parser_with_note.add_argument("--version", required=True)
        parser_with_note.add_argument("--wish", required=True)
        parser_with_note.add_argument("--step", action="append", default=[])
        parser_with_note.add_argument(
            "--send-file", action="append", default=[],
            help="A file that WOULD leave this computer, named in the preview.",
        )
        parser_with_note.add_argument("--language", default="uk", choices=("uk", "en"))
    feedback_show = feedback_command.add_parser("show", help="A note, its plans and the verdict.")
    feedback_show.add_argument("--id", type=int, required=True)
    feedback_show.add_argument("--previous-version", default="")
    feedback_show.add_argument("--language", default="uk", choices=("uk", "en"))
    feedback_accept = feedback_command.add_parser("accept", help="Accept a proposed change.")
    feedback_accept.add_argument("--plan", type=int, required=True)
    feedback_accept.add_argument("--actor", required=True)
    feedback_accept.add_argument("--accept-cost", action="store_true")
    feedback_accept.add_argument("--accept-scope", action="store_true")
    feedback_accept.add_argument("--language", default="uk", choices=("uk", "en"))
    feedback_history = feedback_command.add_parser("history", help="Notes for a project.")
    feedback_history.add_argument("--project", required=True)
    feedback_history.add_argument("--limit", type=int, default=20)

    work_command = sub.add_parser(
        "work", help="What a run is doing, what it costs, and what a stop would stop."
    ).add_subparsers(dest="action", required=True)
    work_runs = work_command.add_parser("runs", help="Runs that have not finished.")
    work_runs.add_argument("--limit", type=int, default=20)
    work_show = work_command.add_parser("status", help="One truthful account of a run.")
    work_show.add_argument("--run", type=int, required=True)
    work_show.add_argument("--language", default="uk", choices=("uk", "en"))
    work_stop = work_command.add_parser(
        "stop-plan", help="What stopping would stop, and what would finish anyway."
    )
    work_stop.add_argument("--run", type=int, required=True)
    work_stop.add_argument("--language", default="uk", choices=("uk", "en"))
    work_restart = work_command.add_parser(
        "after-restart", help="What survived a restart, and what needs checking."
    )
    work_restart.add_argument("--run", type=int, required=True)
    work_restart.add_argument("--language", default="uk", choices=("uk", "en"))

    state = sub.add_parser("state").add_subparsers(dest="action", required=True)
    state.add_parser("check")
    backup = state.add_parser("backup")
    backup.add_argument("--to", required=True)
    stale = state.add_parser("stale")
    stale.add_argument("--older-than", type=int, default=3600)
    return command


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    workspace = Path(args.workspace).expanduser().resolve()
    db = Path(args.db).expanduser() if args.db else Path(".agent-factory/state.db")
    if not db.is_absolute():
        db = workspace / db
    return workspace, db.resolve()


def _control_center_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}/"


def _schedule_browser_open(
    url: str, *, delay: float = 0.75, opener: Any | None = None
) -> threading.Timer:
    callback = opener or webbrowser.open
    timer = threading.Timer(delay, callback, args=(url,))
    timer.daemon = True
    timer.start()
    return timer


def _workflow_approval_output(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.target_id,
        "status": item.status,
        "decision_note": item.decision_note,
        "created_at": item.created_at,
        "decided_at": item.decided_at,
    }


def _provider_approval_output(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "provider": item.metadata["provider"],
        "agent_id": item.metadata["agent_id"],
        "task_id": item.metadata["task_id"],
        "status": item.status,
        "decision_note": item.decision_note,
        "created_at": item.created_at,
        "decided_at": item.decided_at,
        "consumed_at": item.metadata["consumed_at"],
        "request_hash": item.metadata["request_hash"],
        "definition_hash": item.metadata["definition_hash"],
    }


def _github_approval_output(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "plan_id": item.target_id,
        "repo": item.metadata["repo"],
        "plan_hash": item.metadata["plan_hash"],
        "status": item.status,
        "decision_note": item.decision_note,
        "created_at": item.created_at,
        "decided_at": item.decided_at,
        "consumed_at": item.metadata["consumed_at"],
    }


def _review_output(item: Any) -> dict[str, Any]:
    result = asdict(item)
    for field in (
        "reviewed_stages",
        "reviewed_artifact_ids",
        "producer_agents",
        "excluded_models",
        "excluded_candidates",
    ):
        result[field] = json.dumps(result[field], sort_keys=True)
    return result


def _seed_example(storage: SQLiteStorage) -> tuple[int, int]:
    from .application import AgentFactoryService

    return AgentFactoryService(storage).seed_example()


def _show_run(storage: SQLiteStorage, run_id: int) -> None:
    run = storage.db.execute(
        "SELECT * FROM workflow_runs WHERE id=?", (run_id,)
    ).fetchone()
    if not run:
        raise KeyError(f"Unknown run: {run_id}")
    print(f"Run {run_id}: {run['workflow_id']} - {run['status']}")
    for row in storage.artifacts(run_id):
        print(
            f"\n--- {row['stage']} | {row['agent_id']} | {row['provider']} ---\n"
            f"{row['content']}"
        )
    for assignment in storage.reviewer_assignments(run_id):
        print(
            "\nREVIEW ROUTING: "
            f"{assignment['stage']} -> {assignment['reviewer_agent_id']} "
            f"({assignment['reviewer_model']})"
        )
    gate = storage.db.execute(
        "SELECT * FROM approval_gates WHERE run_id=?", (run_id,)
    ).fetchone()
    if gate:
        print(f"\nSTOPPED AT HUMAN APPROVAL: gate {gate['id']} is {gate['status']}")


def _import_backlog(
    storage: SQLiteStorage, proposal: BacklogProposal, project_id: int
) -> dict[str, Any]:
    from .application import AgentFactoryService

    return asdict(AgentFactoryService(storage).import_backlog(proposal, project_id))


def _canonical_hash(value: Any) -> str:
    from .application import canonical_hash

    return canonical_hash(value)


def _provider_snapshot_hashes(
    provider: str, agent: Any, item: Any
) -> tuple[str, str]:
    from .application import provider_snapshot_hashes

    return provider_snapshot_hashes(provider, agent, item)


def _provider_invoke(storage: SQLiteStorage, registry: Any, gate_id: int) -> int:
    from .application import AgentFactoryService

    result = AgentFactoryService(storage, registry).invoke_provider(gate_id)
    print(json.dumps(asdict(result), indent=2))
    return 0 if result.ok else 3


def _godot(args: argparse.Namespace) -> int:
    from .godot_engine import GodotAdapter
    from .godot_pack import GodotPack, PackConflict

    pack = GodotPack()
    candidates = tuple(getattr(args, "executable", None) or ()) or None
    if args.action == "templates":
        print(json.dumps([template.manifest for template in pack.templates()], indent=2))
        return 0
    if args.action == "health":
        adapter = (
            GodotAdapter(executable_candidates=candidates) if candidates else GodotAdapter()
        )
        health = adapter.health()
        print(json.dumps(asdict(health), indent=2))
        return 0 if health.healthy else 3
    target = Path(args.path).expanduser().resolve()
    if args.action == "plan":
        print(json.dumps(pack.plan(target, args.template).preview(), indent=2))
        return 0
    if args.action == "apply":
        plan = pack.plan(target, args.template)
        try:
            receipt = pack.apply(
                plan,
                approved_overwrites=tuple(args.approve_overwrite or ()),
                actor=args.actor or "",
            )
        except PackConflict as exc:
            print(json.dumps(
                {"status": "refused", "reason": str(exc), "preview": plan.preview()},
                indent=2,
            ))
            return 3
        print(json.dumps(asdict(receipt), indent=2))
        return 0
    template = pack.template(args.template)
    adapter = (
        GodotAdapter(executable_candidates=candidates) if candidates else GodotAdapter()
    )
    artifact = adapter.build(
        target,
        preset=args.preset,
        output=Path(args.output).expanduser().resolve(),
        template_id=template.template_id,
        template_version=template.version,
        project_digest=pack.project_digest(target, template.template_id),
        scripts=[entry.path for entry in template.files if entry.path.endswith(".gd")],
        source_commit=args.commit,
        frames=args.frames,
    )
    print(json.dumps(artifact.manifest, indent=2))
    return 0 if artifact.succeeded else 3


def _assets(args: argparse.Namespace) -> int:
    from .asset_provenance import (
        AssetCandidate, AssetLibrary, AssetProvenance, inspect_archive,
    )

    if args.action == "inspect":
        report = inspect_archive(Path(args.archive).expanduser())
        print(json.dumps(report.record, indent=2))
        return 0 if report.safe else 3
    library = AssetLibrary(
        Path(args.path).expanduser().resolve(),
        budget=getattr(args, "budget", "baseline-pc"),
    )
    if args.action == "list":
        print(json.dumps(library.assets(), indent=2, sort_keys=True))
        return 0
    if args.action == "rights":
        verdict = library.rights_check(args.operation)
        print(json.dumps(verdict.record, indent=2))
        return 0 if verdict.allowed else 3
    payload = Path(args.file).expanduser().read_bytes()
    provenance = (
        AssetProvenance.unrecorded(note="no licence supplied at import")
        if args.licence == "unknown" else
        AssetProvenance.create(
            source=args.source, licence_id=args.licence, attribution=args.attribution,
        )
    )
    candidate = AssetCandidate(args.name, payload, provenance)
    plan = library.plan([candidate])
    if not args.confirm:
        print(json.dumps({
            "preview": plan.preview(),
            "next": "repeat with --confirm to apply",
        }, indent=2))
        return 0 if plan.safe and plan.accepted else 3
    receipt = library.apply(
        plan, [candidate],
        approved_overwrites=plan.conflicts if args.approve_overwrite else (),
        actor=args.actor,
    )
    print(json.dumps({
        "imported": list(receipt.imported), "replaced": list(receipt.replaced),
        "refused": list(receipt.refused), "backup_id": receipt.backup_id,
        "rights": library.rights_check("export").record,
    }, indent=2))
    return 0 if receipt.imported or receipt.replaced else 3


def _levels(args: argparse.Namespace) -> int:
    from .capability_levels import CapabilityCatalogue, ScopeRequest, load_evidence

    evidence = ()
    if getattr(args, "evidence", None):
        evidence = load_evidence(
            json.loads(Path(args.evidence).expanduser().read_text(encoding="utf-8"))
        )
    catalogue = CapabilityCatalogue(evidence)
    if args.action == "list":
        print(json.dumps(catalogue.record, indent=2))
        return 0
    if args.action == "show":
        status = catalogue.status(args.level)
        print(json.dumps(
            {"level": status.level.record, "status": status.record}, indent=2
        ))
        return 0 if status.supported else 3
    answer = catalogue.scope(ScopeRequest.create(
        args.description, features=args.feature,
        dimension=args.dimension, engine=args.engine,
    ))
    print(json.dumps(answer.record, indent=2))
    # Anything short of a guarantee exits non-zero, so a caller cannot read a
    # scoped prototype as a promise.
    return 0 if answer.guarantee else 3


def _unity(args: argparse.Namespace) -> int:
    from .unity_engine import UnityAdapter
    from .unity_setup import (
        DEFAULT_EDITOR, Installation, UnitySetup, read_project_requirement,
    )

    if args.action == "catalogue":
        print(json.dumps(UnitySetup().catalogue(), indent=2))
        return 0
    if args.action == "health":
        adapter = UnityAdapter(
            executable_candidates=tuple(args.executable) if args.executable
            else ("Unity", "unity"),
            licence_probe=lambda: args.licence,
        )
        health = adapter.health()
        print(json.dumps(asdict(health), indent=2))
        return 0 if health.healthy else 3
    setup = UnitySetup(editor_version=args.editor or DEFAULT_EDITOR)
    project = Path(args.path).expanduser().resolve()
    requirement = read_project_requirement(project)
    # No installation is probed here: the factory does not read Unity's own
    # licence or Hub state. The status describes what the project needs.
    status = setup.status(
        Installation(licence_state="unknown"), target=args.target, project=project,
    )
    project_actions = [
        action.record for action in status.actions
        if action.code.startswith("project_")
    ]
    print(json.dumps({
        "project": {
            "path": str(project),
            "editor_version": requirement.editor_version,
            "packages_lock_digest": requirement.packages_lock_digest,
            "found": requirement.found,
            "detail": requirement.detail,
        },
        "selected_editor": setup.editor_version,
        "target": args.target,
        "actions": project_actions,
        "installation_probed": False,
        "note": (
            "Nothing about your machine was inspected here. Run "
            "'lokvetia unity health' for the editor, and Unity Hub reports the "
            "licence - this tool never reads it."
        ),
    }, indent=2))
    return 0 if requirement.found and not project_actions else 3


def _execute(args: argparse.Namespace) -> int:
    workspace, db_path = _paths(args)
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["AGENT_FACTORY_WORKSPACE"] = str(workspace)

    # These imports intentionally happen after workspace selection because configuration
    # supports independent state and overrides for every workspace.
    from .application import AgentFactoryService
    from .environment import as_json
    from .registry import AgentRegistry
    from .runtime import AgentRuntime, ExecutionMode

    if args.command == "web":
        try:
            import uvicorn

            from .web import create_app, validate_loopback_host
        except ImportError as exc:
            raise RuntimeError(
                'Local Control Center dependencies are missing; install with pip install -e ".[web]"'
            ) from exc
        host = validate_loopback_host(args.host)
        if args.port < 1 or args.port > 65_535:
            raise ValueError("--port must be between 1 and 65535")
        if args.open_browser:
            url = _control_center_url(host, args.port)
            print(f"Opening Local Control Center at {url}")
            _schedule_browser_open(url)
        uvicorn.run(create_app(workspace, db_path), host=host, port=args.port)
        return 0

    if args.command == "godot":
        return _godot(args)

    if args.command == "assets":
        return _assets(args)

    if args.command == "levels":
        return _levels(args)

    if args.command == "unity":
        return _unity(args)

    storage = SQLiteStorage(db_path)
    registry = AgentRegistry()
    runtime = AgentRuntime(workspace=workspace)
    service = AgentFactoryService(
        storage, registry, runtime, workspace=workspace
    )
    try:
        if args.command in {"init", "bootstrap"}:
            project_id, task_id = service.seed_example()
            print(
                json.dumps(
                    {"workspace": str(workspace), "database": str(db_path), "project_id": project_id, "task_id": task_id},
                    indent=2,
                )
            )
        elif args.command == "env":
            print(json.dumps(as_json(), indent=2))
        elif args.command == "project":
            if args.action == "init":
                print(
                    json.dumps(
                        asdict(service.create_project(args.name, args.description))
                    )
                )
            else:
                print(json.dumps([asdict(item) for item in service.projects()], indent=2))
        elif args.command == "work-item":
            if args.action == "create":
                item = service.create_work_item(
                    project_id=args.project_id,
                    title=args.title,
                    description=args.description,
                    kind=args.kind,
                    acceptance_criteria=args.acceptance,
                )
                print(json.dumps({"task_id": item.id}))
            elif args.action == "show":
                output = asdict(service.work_item(args.task_id))
                output.pop("created_at")
                output.pop("priority")
                output.pop("assignee")
                print(json.dumps(output, indent=2))
            else:
                items = service.work_items(args.project_id)
                print(
                    json.dumps(
                        [
                            {
                                "id": item.id,
                                "project_id": item.project_id,
                                "title": item.title,
                                "description": item.description,
                                "status": item.status,
                                "kind": item.kind,
                            }
                            for item in items
                        ],
                        indent=2,
                    )
                )
        elif args.command == "providers":
            if args.provider_action == "status":
                print(
                    json.dumps(
                        [
                            item.health_details
                            for item in service.providers()
                            if item.id in service.runtime.providers
                        ],
                        indent=2,
                    )
                )
            elif args.provider_action == "gates":
                print(
                    json.dumps(
                        [
                            _provider_approval_output(item)
                            for item in service.approvals()
                            if item.kind == "provider"
                        ],
                        indent=2,
                    )
                )
            elif args.provider_action == "reconcile":
                print(
                    json.dumps(
                        {
                            "reconciled": service.reconcile_provider_attempts(),
                            "retry_requires_new_gate": True,
                        },
                        indent=2,
                    )
                )
            elif args.provider_action == "request":
                gate_id = service.request_provider_execution(
                    args.provider, args.agent, args.task_id
                )
                print(
                    f"Provider execution gate {gate_id} is pending human approval."
                )
            elif args.provider_action in {"approve", "reject", "cancel"}:
                decision = (
                    "cancelled"
                    if args.provider_action == "cancel"
                    else "approved" if args.provider_action == "approve" else "rejected"
                )
                service.decide_provider_execution(args.gate_id, decision, args.note)
                print(f"Provider execution gate {args.gate_id} {decision} by Human.")
            else:
                result = service.invoke_provider(args.gate_id)
                print(json.dumps(asdict(result), indent=2))
                return 0 if result.ok else 3
        elif args.command == "agents":
            if args.action == "list":
                print(
                    json.dumps(
                        [
                            {
                                "id": item.id,
                                "name": item.name,
                                "role": item.role,
                                "enabled": item.enabled,
                                "provider": item.provider,
                                "model": item.model,
                            }
                            for item in service.agents()
                        ],
                        indent=2,
                    )
                )
            elif args.action == "replace":
                agent = service.replace_agent_provider(
                    args.agent_id, args.provider, args.model
                )
                print(
                    json.dumps(
                        {
                            "agent_id": agent.id,
                            "provider": agent.provider,
                            "model": agent.model,
                        }
                    )
                )
            else:
                agent = service.set_agent_enabled(
                    args.agent_id, args.action == "enable"
                )
                print(json.dumps({"agent_id": agent.id, "enabled": agent.enabled}))
        elif args.command == "reviews":
            print(
                json.dumps(
                    [_review_output(item) for item in service.reviews(args.run_id, limit=args.limit)],
                    indent=2,
                )
            )
        elif args.command == "backlog":
            if args.action == "validate":
                proposal = load_backlog(Path(args.path))
                print(
                    json.dumps(
                        {
                            "valid": True,
                            "source_sha256": proposal.source_sha256,
                            "item_count": len(proposal.items),
                            "stable_ids": [item.stable_id for item in proposal.items],
                        },
                        indent=2,
                    )
                )
            elif args.action == "import":
                print(
                    json.dumps(
                        asdict(
                            service.import_backlog(
                                load_backlog(Path(args.path)), args.project_id
                            )
                        ),
                        indent=2,
                    )
                )
            elif args.action == "gates":
                print(
                    json.dumps(
                        [
                            _github_approval_output(item)
                            for item in service.approvals()
                            if item.kind == "github"
                        ],
                        indent=2,
                    )
                )
            elif args.action in {"approve", "reject"}:
                decision = "approved" if args.action == "approve" else "rejected"
                service.decide_github_approval(args.gate_id, decision, args.note)
                print(f"GitHub gate {args.gate_id} {decision} by Human.")
            elif args.apply:
                if args.plan_id is None or args.gate_id is None:
                    raise ValueError("--apply requires --plan-id and --gate-id")
                plan = storage.github_plan(args.plan_id)
                client = GitHubClient(repo=plan["repo"], dry_run=False)
                result = service.apply_github_plan(
                    args.plan_id, args.gate_id, client
                )
                print(json.dumps(result, indent=2))
                return 0 if result.get("ok") else 3
            else:
                if not args.path or not args.repo:
                    raise ValueError("backlog sync requires --path and --repo")
                proposal = load_backlog(Path(args.path))
                client = GitHubClient(repo=args.repo, dry_run=True)
                if args.existing_json:
                    existing = json.loads(Path(args.existing_json).read_text(encoding="utf-8"))
                else:
                    response = client.issues()
                    if not response.get("ok"):
                        raise RuntimeError(response.get("error", "Could not read GitHub issues"))
                    existing = response.get("data", [])
                if not isinstance(existing, list):
                    raise ValueError("GitHub issue source must be a list")
                difference = diff_issues(proposal, existing)
                operations = issue_operations(difference)
                output: dict[str, Any] = {"diff": difference, "operations": operations}
                if operations:
                    output.update(
                        service.preview_github_plan(args.repo, operations, client)
                    )
                print(json.dumps(output, indent=2))
        elif args.command == "task":
            if args.action == "claim":
                print(json.dumps(asdict(service.claim_work_item(args.task_id, args.agent))))
            elif args.action == "run":
                print("Execution mode: simulation")
                _show_run(
                    storage,
                    service.run_workflow(
                        args.task_id, args.workflow, ExecutionMode.SIMULATION
                    ).id,
                )
            elif args.artifact_id is not None or args.decision is not None:
                if args.artifact_id is None or args.decision is None:
                    raise ValueError("artifact review requires --artifact-id and --decision")
                service.review_artifact(
                    args.task_id, args.artifact_id, args.decision, args.note
                )
                print(
                    json.dumps(
                        {"artifact_id": args.artifact_id, "decision": args.decision}
                    )
                )
            else:
                print(
                    json.dumps(
                        [asdict(item) for item in service.artifacts(task_id=args.task_id)],
                        indent=2,
                    )
                )
        elif args.command == "workflow":
            mode = ExecutionMode(args.mode)
            print(
                f"Execution mode: {mode.value} "
                f"({'offline fallback permitted' if mode is ExecutionMode.SIMULATION else 'offline fallback prohibited'})"
            )
            _show_run(
                storage,
                service.run_workflow(args.task_id, args.workflow, mode).id,
            )
        elif args.command == "approvals":
            if args.action == "list":
                print(
                    json.dumps(
                        [
                            _workflow_approval_output(item)
                            for item in service.approvals()
                            if item.kind == "workflow"
                        ],
                        indent=2,
                    )
                )
            else:
                decision = "approved" if args.action == "approve" else "rejected"
                service.decide_workflow_approval(args.gate_id, decision, args.note)
                print(f"Approval {args.gate_id} {decision} by Human.")
        elif args.command == "audit":
            print(
                json.dumps(
                    [
                        {
                            **asdict(item),
                            "payload": json.dumps(item.payload),
                        }
                        for item in service.events(limit=args.limit)
                    ],
                    indent=2,
                )
            )
        elif args.command == "playable":
            from .playable_versions import CandidateBuild, PlayableVersions

            versions = PlayableVersions(storage)
            if args.action == "current":
                current = versions.current(args.project)
                print(json.dumps(current.checkpoint if current else None, indent=2))
                return 0 if current else 3
            if args.action == "history":
                print(json.dumps(
                    [item.checkpoint for item in versions.history(args.project, limit=args.limit)],
                    indent=2,
                ))
            elif args.action == "promote":
                record = json.loads(Path(args.build).expanduser().read_text(encoding="utf-8"))
                result = versions.promote(
                    args.project,
                    CandidateBuild.from_artifact(record, engine=args.engine),
                    command_id=args.command_id, actor=args.actor,
                )
                print(json.dumps({
                    "outcome": result.outcome, "reason": result.reason,
                    "pointer_moved": result.pointer_moved,
                    "reproducible": result.reproducible, "replayed": result.replayed,
                    "version": result.version.checkpoint if result.version else None,
                }, indent=2))
                return 0 if result.accepted or result.outcome == "unchanged" else 3
            elif args.action == "restore":
                preview = versions.restore_preview(
                    args.project, args.version, branch=args.branch,
                )
                if not args.confirm:
                    print(json.dumps({
                        "preview": preview.summary,
                        "next": "repeat with --confirm --command-id ID --actor NAME",
                    }, indent=2))
                    return 0
                if not args.command_id or not args.actor:
                    raise ValueError("--confirm requires --command-id and --actor")
                result = versions.restore(
                    args.project, args.version, branch=args.branch,
                    command_id=args.command_id, actor=args.actor, preview=preview,
                )
                print(json.dumps({
                    "outcome": result.outcome, "reason": result.reason,
                    "replayed": result.replayed,
                    "version": result.version.checkpoint if result.version else None,
                }, indent=2))
            else:
                current = versions.current(args.project)
                if current is None:
                    print(json.dumps({"verified": False, "reason": "no playable version"}, indent=2))
                    return 3
                verified, reason = versions.verify_artifact(current)
                print(json.dumps({
                    "verified": verified, "reason": reason,
                    "version_digest": current.version_digest,
                }, indent=2))
                return 0 if verified else 3
        elif args.command == "export":
            from .asset_provenance import AssetLibrary
            from .export_bundle import (
                TARGETS, ExportBundler, ExportRefused, ShareGate,
                attributions_from_library, load_bundle,
            )
            from .playable_versions import PlayableVersions

            if args.action == "targets":
                print(json.dumps([
                    {
                        "target": target.target_id, "platform": target.platform,
                        "supported": target.supported,
                        "launch": target.launch, "reason": target.reason,
                    }
                    for target in TARGETS.values()
                ], indent=2))
                return 0
            if args.action == "share":
                bundle = load_bundle(Path(args.bundle).expanduser())
                gate = ShareGate()
                preview = gate.prepare(
                    bundle, destination=args.destination, visibility=args.visibility,
                )
                if not (args.confirm or args.cancel):
                    print(json.dumps(preview.record, indent=2))
                    return 0 if preview.allowed else 3
                decision = gate.decide(
                    preview, decision="cancel" if args.cancel else "approve",
                    actor=args.actor, bundle=bundle,
                )
                print(json.dumps(decision.record, indent=2))
                return 0 if decision.outcome in {"published", "cancelled"} else 3
            project_path = Path(args.path).expanduser().resolve()
            current = PlayableVersions(storage).current(args.project)
            if current is None:
                raise ValueError(
                    f"{args.project} has no verified playable version to export"
                )
            library = AssetLibrary(project_path)
            bundler = ExportBundler(project_path)
            preflight = bundler.preflight(
                target_id=args.target, version=current,
                artifact=Path(current.artifact_path),
                rights=library.rights_check("export"),
                attributions=attributions_from_library(library),
                extra_excludes=tuple(args.exclude or ()),
            )
            if args.action == "preflight":
                print(json.dumps(preflight.preview(), indent=2))
                return 0 if preflight.allowed else 3
            try:
                bundle = bundler.build(
                    preflight, artifact=Path(current.artifact_path),
                    output=Path(args.output).expanduser().resolve(),
                    version=current, game_name=args.name,
                )
            except ExportRefused as exc:
                print(json.dumps(
                    {"status": "refused", "reason": str(exc), "preflight": preflight.preview()},
                    indent=2,
                ))
                return 3
            print(json.dumps({
                "package": bundle.path, "checksum": bundle.checksum,
                "size_bytes": bundle.size_bytes, "manifest": dict(bundle.manifest),
            }, indent=2))
        elif args.command == "support":
            from .support_bundle import (
                ALWAYS_INCLUDED, NEVER_COLLECTED, OPT_IN_CATEGORIES, SupportBundler,
            )

            if args.action == "categories":
                print(json.dumps({
                    "always_included": list(ALWAYS_INCLUDED),
                    "opt_in": list(OPT_IN_CATEGORIES),
                    "never_collected": list(NEVER_COLLECTED),
                    "note": "Environment variables are collected by name only.",
                }, indent=2))
                return 0
            bundler = SupportBundler(workspace, storage=storage)
            preview = bundler.preview(include=tuple(args.include or ()))
            if args.action == "preview":
                if args.show:
                    print(preview.read(args.show))
                else:
                    print(json.dumps(preview.record(), indent=2))
                return 0
            result = bundler.build(
                preview, output=Path(args.output).expanduser().resolve(),
                actor=args.actor,
            )
            print(json.dumps({
                "bundle": result.path, "checksum": result.checksum,
                "size_bytes": result.size_bytes,
                "selected": list(result.manifest["selected"]),
                "not_selected": list(result.manifest["not_selected"]),
                "redactions_applied": list(result.manifest["redactions_applied"]),
            }, indent=2))
        elif args.command == "update":
            from . import __version__
            from .application_update import (
                ApplicationUpdater, load_manifest, parse_project_pin,
            )

            payload = json.loads(
                Path(args.manifest).expanduser().read_text(encoding="utf-8")
            )
            schema = storage.db.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            trust = (
                {args.trust_key: args.trust_secret.encode("utf-8")}
                if args.trust_key and args.trust_secret else {}
            )
            updater = ApplicationUpdater(
                current_version=__version__,
                current_schema=int(schema["version"] or 0),
                trust_material=trust,
            )
            plan = updater.plan(
                load_manifest(payload),
                projects=[parse_project_pin(value) for value in (args.project_pin or ())],
                separate_plan=args.separate_plan,
            )
            print(json.dumps(plan.preview(), indent=2))
            return 0 if plan.allowed else 3
        elif args.command == "uninstall":
            from .application_update import uninstall_plan

            plan = uninstall_plan(
                workspace,
                project_paths=[Path(value) for value in (args.project or ())],
                remove_projects=args.remove_projects,
            )
            print(json.dumps({
                **plan.preview(),
                "performed": False,
                "note_cli": "This command previews only; it removes nothing.",
            }, indent=2))
        elif args.command == "settings":
            from .settings_store import SettingsCentre

            centre = SettingsCentre(storage)
            if args.action == "show":
                if args.section:
                    print(json.dumps(
                        centre.section_view(args.section, args.language),
                        indent=2, ensure_ascii=False,
                    ))
                else:
                    print(json.dumps(
                        centre.overview(args.language), indent=2, ensure_ascii=False,
                    ))
            elif args.action == "verify":
                sections = (
                    [args.section] if args.section
                    else [item["section"] for item in centre.overview()["sections"]]
                )
                report = {
                    name: [
                        finding.record(args.language)
                        for finding in centre.verify_section(name)
                    ]
                    for name in sections
                }
                print(json.dumps(report, indent=2, ensure_ascii=False))
                worst = {
                    finding["level"] for findings in report.values() for finding in findings
                }
                return 3 if "problem" in worst else 0
            elif args.action == "set":
                print(json.dumps(centre.set(
                    args.key, args.value, actor=args.actor, reason=args.reason,
                    acknowledged_consequence=args.acknowledge,
                ), indent=2, ensure_ascii=False))
            elif args.action == "reset":
                print(json.dumps(centre.reset(
                    args.key, actor=args.actor, reason=args.reason,
                    acknowledged_consequence=args.acknowledge,
                ), indent=2, ensure_ascii=False))
            else:
                print(json.dumps(
                    [item.record for item in centre.changes(key=args.key, limit=args.limit)],
                    indent=2, ensure_ascii=False,
                ))
        elif args.command == "download":
            from .distribution import describe, load_release, verify_all

            manifest = Path(args.manifest)
            release = load_release(json.loads(manifest.read_text(encoding="utf-8")))
            if args.action == "show":
                print(json.dumps(
                    describe(release, platform=args.platform, language=args.language),
                    indent=2, ensure_ascii=False,
                ))
            else:
                directory = Path(args.directory) if args.directory else manifest.parent
                checked = verify_all(release, directory)
                print(json.dumps({
                    "version": release.version,
                    "verified": [item.record(args.language) for item in checked],
                }, indent=2, ensure_ascii=False))
        elif args.command == "studio":
            from .studio_autonomy import AutonomyJournal, catalogue

            journal = AutonomyJournal(storage)
            if args.action == "gates":
                print(json.dumps(catalogue(args.language), indent=2, ensure_ascii=False))
            elif args.action == "plan":
                from .studio_backlog import from_mission
                from .studio_slices import StageBoundaries

                print(json.dumps(
                    from_mission(
                        storage, args.mission,
                        playable=StageBoundaries(storage).playable_map(args.mission),
                    ).record(args.language),
                    indent=2, ensure_ascii=False,
                ))
            elif args.action == "slice":
                from .localisation import Message
                from .studio_slices import StageBoundaries

                boundaries = StageBoundaries(storage)
                if args.version:
                    boundary = boundaries.playable(
                        args.mission, args.stage,
                        project_key=args.project or "", version_digest=args.version,
                        declared_by=args.actor,
                    )
                else:
                    boundary = boundaries.nothing_to_test(
                        args.mission, args.stage,
                        reason=Message(args.nothing_uk or "", args.nothing_en or ""),
                        declared_by=args.actor,
                    )
                print(json.dumps(boundary.record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "slices":
                from .studio_slices import StageBoundaries

                print(json.dumps(
                    StageBoundaries(storage).report(args.mission, language=args.language),
                    indent=2, ensure_ascii=False,
                ))
            elif args.action == "pause":
                from .studio_cycles import StudioCycles

                print(json.dumps(StudioCycles(storage).pause(
                    args.mission, actor=args.actor, finishing=args.finishing,
                ).record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "comment":
                from .studio_cycles import StudioCycles

                print(json.dumps(StudioCycles(storage).comment(
                    args.mission, args.text, scope=args.scope,
                    subject=args.subject, author=args.author,
                ).record(), indent=2, ensure_ascii=False))
            elif args.action == "resume":
                from .studio_cycles import StudioCycles

                print(json.dumps(StudioCycles(storage).resume(
                    args.mission, actor=args.actor,
                ).record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "machines":
                from .studio_workers import StudioMachines

                print(json.dumps(
                    StudioMachines(storage).overview(language=args.language),
                    indent=2, ensure_ascii=False,
                ))
            elif args.action == "register-machine":
                from .studio_workers import StudioMachines

                print(json.dumps(StudioMachines(storage).register(
                    args.machine, name=args.name, kind=args.kind,
                    capabilities=args.can, video_memory_gb=args.video_memory_gb,
                    registered_by=args.actor,
                ).record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "mandate":
                from .studio_supervisor import Supervisor

                mandate = Supervisor(storage).grant(
                    args.mission, steps=args.allow or ("plan",), granted_by=args.actor,
                    ceiling=args.ceiling, unit=args.unit, hours=args.hours,
                    reason=args.reason,
                )
                print(json.dumps(mandate.record(args.language), indent=2,
                                 ensure_ascii=False))
            elif args.action == "revoke-mandate":
                from .studio_supervisor import Supervisor

                print(json.dumps(Supervisor(storage).revoke(
                    args.mission, actor=args.actor,
                ).record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "steps":
                from .studio_supervisor import Supervisor

                print(json.dumps(Supervisor(storage).report(
                    args.mission, language=args.language, limit=args.limit,
                ), indent=2, ensure_ascii=False))
            elif args.action == "run":
                from .studio_supervisor import CoreMissionDriver, Supervisor

                supervisor = Supervisor(storage)
                driver = CoreMissionDriver(storage, args.mission_id,
                                           workspace=workspace)
                taken = (
                    (supervisor.advance(args.mission, driver,
                                        next_step_cost=args.next_step_cost),)
                    if args.once else
                    supervisor.run(args.mission, driver, passes=args.passes,
                                   next_step_cost=args.next_step_cost)
                )
                print(json.dumps([step.record(args.language) for step in taken],
                                 indent=2, ensure_ascii=False))
                # The exit code is about the person, not about success: 3 means
                # the studio cannot go further without them.
                return 3 if any(step.needs_person for step in taken) else 0
            elif args.action == "first-run":
                from .studio_first_run import FirstRun

                report = FirstRun(storage).report(language=args.language)
                print(json.dumps(report, indent=2, ensure_ascii=False))
                # Nothing can start without a source, and the exit code says so.
                return 0 if report["can_start"] else 3
            elif args.action == "connect":
                from .studio_first_run import FirstRun
                from .studio_workers import StudioMachines

                wizard = FirstRun(storage)
                if args.kind == "local_model":
                    source = wizard.offer_local_model(
                        args.source, name=args.name,
                        machines=StudioMachines(storage),
                        machine_key=args.machine, needed_gb=args.needed_gb,
                    )
                else:
                    source = wizard.connect(
                        args.source, kind=args.kind, name=args.name, state=args.state,
                    )
                print(json.dumps(source.record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "team":
                from .studio_roster import StudioRoster

                print(json.dumps(
                    StudioRoster(storage).report(args.mission, language=args.language),
                    indent=2, ensure_ascii=False,
                ))
            elif args.action == "add-role":
                from .studio_roster import StudioRoster

                print(json.dumps(StudioRoster(storage).enable(
                    args.mission, args.role, actor=args.actor, provider=args.provider,
                    model=args.model, concurrency=args.concurrency, reason=args.reason,
                ).record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "drop-role":
                from .studio_roster import StudioRoster

                roster = StudioRoster(storage)
                roster.disable(args.mission, args.role, actor=args.actor, reason=args.reason)
                print(json.dumps(
                    roster.report(args.mission, language=args.language),
                    indent=2, ensure_ascii=False,
                ))
            elif args.action == "role-model":
                from .studio_roster import StudioRoster

                print(json.dumps(StudioRoster(storage).assign_model(
                    args.mission, args.role, provider=args.provider, model=args.model,
                    actor=args.actor,
                ).record(args.language), indent=2, ensure_ascii=False))
            elif args.action == "cost":
                from .studio_cost import StudioCosts

                report = StudioCosts(storage).report(
                    args.mission, language=args.language,
                    remaining_tasks=args.remaining_tasks, stage_key=args.stage,
                    next_step=args.next_step,
                )
                print(json.dumps(report, indent=2, ensure_ascii=False))
                # Going over the limit is the one thing a person must answer.
                return 3 if report["over"] else 0
            elif args.action == "limit":
                from .studio_cost import StudioCosts

                print(json.dumps(StudioCosts(storage).set_limit(
                    args.mission, amount=args.amount, actor=args.actor,
                    unit=args.unit, reason=args.reason,
                ).record(), indent=2, ensure_ascii=False))
            elif args.action == "paid-tools":
                from .studio_paid_tools import PaidTools

                report = PaidTools(storage).report(args.mission, language=args.language)
                print(json.dumps(report, indent=2, ensure_ascii=False))
                return 3 if report["open"] else 0
            elif args.action == "choose":
                from .studio_paid_tools import PaidTools

                answered, rebuild = PaidTools(storage).answer(
                    args.choice, choice=args.way, actor=args.actor,
                )
                print(json.dumps({
                    **answered.record(args.language),
                    "rebuild": rebuild.record(args.language) if rebuild else None,
                }, indent=2, ensure_ascii=False))
            elif args.action == "cycles":
                from .studio_cycles import StudioCycles

                print(json.dumps(
                    StudioCycles(storage).report(args.mission, language=args.language),
                    indent=2, ensure_ascii=False,
                ))
            elif args.action == "decisions":
                report = journal.report(
                    mission=args.mission, language=args.language, limit=args.limit,
                )
                print(json.dumps(report, indent=2, ensure_ascii=False))
                # An open question is the one thing waiting on a person.
                return 3 if report["questions"] else 0
            else:
                print(json.dumps(journal.answer(
                    args.question, answer=args.answer, actor=args.actor,
                ), indent=2, ensure_ascii=False))
        elif args.command == "feedback":
            from .game_feedback import (
                Attachment, ChangePlan, Cost, Feedback, FeedbackJournal,
                PlayedBuild, accept_plan,
            )
            from .localisation import Message

            journal = FeedbackJournal(storage)
            if args.action in ("preview", "add"):
                note = Feedback.create(
                    build=PlayedBuild(args.project, args.version),
                    wish=args.wish,
                    steps=args.step,
                    attachments=[
                        Attachment("screenshot", name, leaves_machine=True)
                        for name in args.send_file
                    ],
                )
                preview = note.preview(args.language)
                if args.action == "add":
                    preview["feedback_id"] = journal.record(note)
                print(json.dumps(preview, indent=2, ensure_ascii=False))
            elif args.action == "show":
                note = journal.feedback(args.id)
                print(json.dumps({
                    **note.preview(args.language),
                    "verdict": journal.verdict(
                        args.id, previous_version=args.previous_version,
                    ).record(args.language),
                }, indent=2, ensure_ascii=False))
            elif args.action == "accept":
                row = storage.db.execute(
                    "SELECT plan_json,cost_amount,cost_unit,accepted_by"
                    " FROM game_feedback_plans WHERE id=?", (args.plan,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown plan {args.plan}")
                stored = json.loads(row["plan_json"])
                plan = ChangePlan(
                    feedback_digest=stored["feedback"], changes=(), impacts=(),
                    added_scope=tuple(
                        Message(item, item) for item in stored.get("added_scope", [])
                    ),
                    cost=Cost(float(row["cost_amount"]), row["cost_unit"]),
                    applies_to=stored.get("applies_to", ""),
                    accepted_by=row["accepted_by"],
                )
                accepted = accept_plan(
                    plan, actor=args.actor,
                    accept_cost=args.accept_cost, accept_scope=args.accept_scope,
                )
                journal.accept(
                    args.plan, actor=accepted.accepted_by, at=accepted.accepted_at,
                )
                print(json.dumps({
                    "plan_id": args.plan, "accepted_by": accepted.accepted_by,
                    "accepted_at": accepted.accepted_at,
                }, indent=2, ensure_ascii=False))
            else:
                print(json.dumps(
                    {"feedback": list(journal.history(args.project, limit=args.limit))},
                    indent=2, ensure_ascii=False,
                ))
        elif args.command == "work":
            from .work_status_store import WorkStatusReader

            reader = WorkStatusReader(storage)
            if args.action == "runs":
                print(json.dumps(
                    {"runs": list(reader.open_runs(limit=args.limit))},
                    indent=2, ensure_ascii=False,
                ))
            elif args.action == "status":
                report = reader.report(args.run, language=args.language)
                print(json.dumps(report, indent=2, ensure_ascii=False))
                # A blocker is the one thing a person has to act on, so say so
                # in the exit code as well as in the output.
                return 3 if report["blockers"] else 0
            elif args.action == "stop-plan":
                print(json.dumps(
                    reader.stop_plan(args.run).record(args.language),
                    indent=2, ensure_ascii=False,
                ))
            else:
                print(json.dumps(
                    reader.after_restart(args.run).record(args.language),
                    indent=2, ensure_ascii=False,
                ))
        elif args.command == "state":
            if args.action == "check":
                print(json.dumps(storage.integrity_check(), indent=2))
            elif args.action == "backup":
                print(json.dumps({"backup": str(service.backup(Path(args.to)))}, indent=2))
            else:
                print(json.dumps(service.stale_state(args.older_than), indent=2))
        elif args.command == "demo":
            print("Execution mode: simulation (live providers are never invoked implicitly)")
            _show_run(storage, service.run_demo().id)
        return 0
    finally:
        storage.close()


def main(argv: list[str] | None = None) -> int:
    from .localisation import LocalisedError

    arguments = parser().parse_args(argv)
    try:
        return _execute(arguments)
    except (KeyError, ValueError, RuntimeError, OSError, PermissionError) as exc:
        # A refusal that carries its own text is printed in the language the
        # command was asked in, not in the default one.
        language = getattr(arguments, "language", None)
        message = (
            exc.text(language) if isinstance(exc, LocalisedError) and language
            else str(exc)
        )
        print(f"error: {message}", file=sys.stderr)
        return 2
