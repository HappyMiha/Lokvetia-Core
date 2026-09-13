import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from fastapi.testclient import TestClient

from agent_factory.application import AgentFactoryService
from agent_factory.backlog import load_backlog
from agent_factory.cli import _control_center_url, _schedule_browser_open, parser
from agent_factory.providers import DeterministicProvider
from agent_factory.runtime import AgentRuntime
from agent_factory.storage import SQLiteStorage
from agent_factory.web import create_app, validate_loopback_host

ROOT = Path(__file__).resolve().parent.parent


def seed(workspace: Path, database: Path) -> tuple[int, int, int]:
    storage = SQLiteStorage(database)
    runtime = AgentRuntime(
        {
            name: DeterministicProvider()
            for name in (
                "deterministic",
                "codex",
                "claude",
                "gemini",
                "antigravity",
                "ollama",
            )
        },
        workspace=workspace,
    )
    service = AgentFactoryService(storage, runtime=runtime, workspace=workspace)
    project = service.create_project("Control Center API")
    item = service.create_work_item(
        project_id=project.project_id,
        title="Read operations",
        description="Expose typed state",
        acceptance_criteria=["Responses are bounded"],
    )
    run = service.run_workflow(item.id)
    storage.close()
    return project.project_id, item.id, run.id


class WebHostTests(unittest.TestCase):
    def test_web_open_flag_uses_loopback_url_without_blocking_shutdown(self):
        arguments = parser().parse_args(["--workspace", ".", "web", "--open"])
        self.assertTrue(arguments.open_browser)
        opened: list[str] = []
        timer = _schedule_browser_open(
            _control_center_url("127.0.0.1", 8765), delay=0, opener=opened.append
        )
        timer.join(timeout=2)
        self.assertEqual(opened, ["http://127.0.0.1:8765/"])
        self.assertTrue(timer.daemon)
        self.assertEqual(_control_center_url("::1", 8765), "http://[::1]:8765/")
        documentation = (ROOT / "docs" / "local-control-center.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'python -m pip install -e ".[web]"; if ($LASTEXITCODE -eq 0) '
            "{ python -m agent_factory --workspace . web --open }",
            documentation,
        )
        self.assertIn("Press `Ctrl+C`", documentation)
        self.assertIn("does not require or change `Set-ExecutionPolicy`", documentation)

    def test_documented_cli_command_starts_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = int(reservation.getsockname()[1])
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "src")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agent_factory",
                    "--workspace",
                    str(workspace),
                    "web",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                payload = None
                for _ in range(100):
                    if process.poll() is not None:
                        break
                    try:
                        with urlopen(
                            f"http://127.0.0.1:{port}/api/health", timeout=1
                        ) as response:
                            payload = json.load(response)
                        break
                    except (TimeoutError, URLError):
                        time.sleep(0.05)
                self.assertIsNotNone(
                    payload,
                    process.stderr.read() if process.poll() is not None else "server timeout",
                )
                self.assertEqual(payload["status"], "ready")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stderr is not None:
                    process.stderr.close()

    def test_only_loopback_hosts_are_accepted(self):
        self.assertEqual(validate_loopback_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_loopback_host("LOCALHOST"), "localhost")
        self.assertEqual(validate_loopback_host("::1"), "::1")
        for host in ("0.0.0.0", "192.168.1.10", "example.com", ""):
            with self.subTest(host=host), self.assertRaisesRegex(
                ValueError, "loopback"
            ):
                validate_loopback_host(host)

    def test_empty_database_health_and_resource_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            database = workspace / ".agent-factory" / "state.db"
            with TestClient(create_app(workspace, database), base_url="http://localhost") as client:
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ready")
                for endpoint in (
                    "projects",
                    "work-items",
                    "runs",
                    "artifacts",
                    "reviews",
                    "approvals",
                    "events",
                ):
                    response = client.get(f"/api/{endpoint}")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["items"], [])
                    self.assertEqual(response.json()["total"], 0)
                dashboard = client.get("/api/dashboard")
                self.assertEqual(dashboard.status_code, 200)
                self.assertEqual(
                    dashboard.json()["counts"],
                    {
                        "ready": 0,
                        "active": 0,
                        "blocked": 0,
                        "failed": 0,
                        "awaiting_review": 0,
                        "awaiting_approval": 0,
                    },
                )
                self.assertEqual(
                    set(dashboard.json()["operations"]),
                    {
                        "active_sessions", "queued_tasks", "active_leases",
                        "active_worktrees", "failures", "budgets",
                    },
                )

    def test_typed_resources_pagination_missing_and_malformed_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            database = workspace / ".agent-factory" / "state.db"
            project_id, task_id, run_id = seed(workspace, database)
            with TestClient(create_app(workspace, database), base_url="http://localhost") as client:
                projects = client.get("/api/projects", params={"offset": 0, "limit": 1})
                self.assertEqual(projects.status_code, 200)
                self.assertEqual(projects.json()["total"], 1)
                self.assertEqual(projects.json()["items"][0]["id"], project_id)
                self.assertEqual(
                    client.get(f"/api/work-items/{task_id}").json()["title"],
                    "Read operations",
                )
                self.assertEqual(
                    client.get(f"/api/runs/{run_id}").json()["status"],
                    "awaiting_approval",
                )
                self.assertEqual(client.get("/api/artifacts").json()["total"], 4)
                self.assertGreater(client.get("/api/agents").json()["total"], 1)
                self.assertGreater(client.get("/api/providers").json()["total"], 1)
                self.assertEqual(client.get("/api/reviews").json()["total"], 2)
                self.assertEqual(client.get("/api/approvals").json()["total"], 1)
                self.assertGreater(client.get("/api/events").json()["total"], 1)
                self.assertEqual(
                    client.get("/api/settings").json()["workspace"],
                    str(workspace.resolve()),
                )
                integrations = {
                    item["name"]: item for item in client.get("/api/integrations").json()
                }
                self.assertEqual(integrations["github"]["status"], "unconfigured")
                dashboard = client.get("/api/dashboard").json()
                self.assertEqual(dashboard["counts"]["ready"], 1)
                self.assertEqual(dashboard["counts"]["awaiting_review"], 4)
                self.assertEqual(dashboard["counts"]["awaiting_approval"], 1)
                self.assertEqual(dashboard["runs"][0]["id"], run_id)

                missing = client.get("/api/work-items/999")
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(missing.json()["error"]["code"], "not_found")
                malformed = client.get("/api/work-items/not-an-integer")
                self.assertEqual(malformed.status_code, 422)
                self.assertEqual(
                    malformed.json()["error"]["code"], "validation_error"
                )
                for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}):
                    response = client.get("/api/projects", params=params)
                    self.assertEqual(response.status_code, 422)

    def test_concurrent_reads_use_independent_sqlite_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            database = workspace / ".agent-factory" / "state.db"
            _, task_id, _ = seed(workspace, database)
            with TestClient(create_app(workspace, database), base_url="http://localhost") as client:
                paths = [
                    "/api/projects",
                    "/api/work-items",
                    f"/api/work-items/{task_id}",
                    "/api/runs",
                    "/api/events?limit=10",
                ] * 4
                with ThreadPoolExecutor(max_workers=8) as pool:
                    responses = list(pool.map(client.get, paths))
                self.assertTrue(all(response.status_code == 200 for response in responses))

    def test_default_app_composes_hardware_without_manual_installer_or_auto_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {"schema_version": 1, "source": "local_read_only", "gpu_status": "unknown"}
            with patch("agent_factory.hardware_web.collect_inventory", return_value=report) as collect:
                app = create_app(root, root / "state.db")
            self.assertTrue(app.state.hardware_routes_installed)
            with TestClient(app, base_url="http://localhost") as client:
                self.assertIn('href="/hardware"', client.get("/").text)
                self.assertEqual(client.get("/hardware").status_code, 200)
                collect.assert_not_called()
                result = client.post("/api/hardware/scan", json={})
                self.assertEqual(result.status_code, 200, result.text)
                # The collector's own fields are passed through untouched, and
                # the report says which machine produced them.
                answered = result.json()
                machine = answered.pop("machine")
                self.assertEqual(answered, report)
                self.assertIn(machine["kind"], ("this_pc", "web_container", "cloud_worker"))
                self.assertTrue(machine["caveat"])
                collect.assert_called_once_with(root.resolve())
                self.assertFalse((root / "state.db").exists())

    def test_openapi_exposes_only_reviewed_guarded_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with TestClient(
                create_app(workspace, workspace / ".agent-factory" / "state.db"),
                base_url="http://localhost",
            ) as client:
                paths = client.get("/api/openapi.json").json()["paths"]
                self.assertIn("/api/projects", paths)
                mutation_routes = {
                    path
                    for path, operations in paths.items()
                    if "post" in operations
                }
                self.assertEqual(
                    mutation_routes,
                    {
                        "/api/ai-setup",
                        "/api/credential-connections",
                        "/api/settings/values/{key}",
                        "/api/settings/values/{key}/reset",
                        "/api/settings/sections/{section_id}/verify",
                        "/api/games/{project_key}/feedback",
                        "/api/games/{project_key}/feedback/preview",
                        "/api/feedback/{feedback_id}/plans",
                        "/api/feedback/plans/{plan_id}/accept",
                        "/api/studio/questions/{question_id}/answer",
                        "/api/studio/create",
                        "/api/studio/slices/{mission_key}/{stage_key}",
                        "/api/studio/cycles/{mission_key}/pause",
                        "/api/studio/cycles/{mission_key}/comments",
                        "/api/studio/cycles/{mission_key}/resume",
                        "/api/studio/cost/{mission_key}/limit",
                        "/api/studio/paid-tools/{mission_key}",
                        "/api/studio/paid-tools/choices/{choice_id}",
                        "/api/studio/roster/{mission_key}/{role_id}",
                        "/api/studio/machines/{machine_key}",
                        "/api/studio/first-run/{source_key}",
                        "/api/studio/supervisor/{mission_key}",
                        "/api/studio/supervisor/{mission_key}/revoke",
                        "/api/hardware/scan",
                        "/api/configuration-advice",
                        "/api/game-planning/{mission_id}",
                        "/api/installation-plans/{mission_id}",
                        "/api/approvals/installation/{mission_id}",
                        "/api/games/starts",
                        "/api/games/starts/{ident}/save",
                        "/api/games/starts/{ident}/submit",
                        "/api/work-items/{task_id}/claim",
                        "/api/work-items/{task_id}/runs",
                        "/api/work-items/{task_id}/archive",
                        "/api/work-items/archive-all",
                        "/api/executions/runs/{run_id}/cancel",
                        "/api/executions/runs/{run_id}/pause",
                        "/api/executions/runs/{run_id}/resume",
                        "/api/executions/sessions/{session_id}/stop",
                        "/api/executions/leases/release",
                        "/api/artifacts/{artifact_id}/review",
                        "/api/agents/{agent_id}/enabled",
                        "/api/agents/{agent_id}/provider",
                        "/api/settings/{key}",
                        "/api/github/preview",
                        "/api/founder-decisions/{gate_id}",
                        "/api/backlog/import",
                        "/api/backlog/analyze-upload",
                        "/api/control/actions",
                        "/api/autonomous-missions/{mission_id}/environment/check",
                    },
                )

    def test_running_epic_returns_conflict_instead_of_server_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            storage = SQLiteStorage(workspace / "state.db")
            service = AgentFactoryService(storage, workspace=workspace)
            project = service.create_project("Planning")
            item = service.create_work_item(project_id=project.project_id, title="Epic", description="Planning", kind="epic", acceptance_criteria=["Ready"])
            storage.db.execute("UPDATE work_items SET kind='epic' WHERE id=?", (item.id,))
            storage.db.commit()
            storage.close()
            with TestClient(create_app(workspace, workspace / "state.db"), base_url="http://localhost") as client:
                response = client.post(f"/api/work-items/{item.id}/runs", json={"workflow_id": "delivery", "mode": "simulation", "confirmed": True}, headers={"X-Agent-Factory-Confirm": "true"})
            self.assertEqual(response.status_code, 409)
            self.assertIn("kind:epic", response.json()["error"]["message"])

    def test_founder_decision_packet_and_idempotent_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            database = workspace / ".agent-factory" / "state.db"
            _, task_id, run_id = seed(workspace, database)
            headers = {"X-Agent-Factory-Confirm": "true"}
            with TestClient(create_app(workspace, database), base_url="http://localhost") as client:
                packets = client.get("/api/founder-decisions").json()
                self.assertEqual(len(packets), 1)
                packet = packets[0]
                gate_id = packet["approval"]["id"]
                self.assertEqual(packet["run"]["id"], run_id)
                self.assertEqual(
                    packet["work_item"]["acceptance_criteria"],
                    ["Responses are bounded"],
                )
                self.assertIn("implementation", [item["stage"] for item in packet["artifacts"]])
                self.assertIn("validation", [item["stage"] for item in packet["artifacts"]])
                self.assertEqual(len(packet["reviews"]), 2)
                self.assertIn("unresolved_findings", packet)
                for review in packet["reviews"]:
                    self.assertNotIn(
                        review["reviewer_model"].casefold(),
                        {
                            producer["model"].casefold()
                            for producer in review["producer_agents"]
                        },
                    )

                artifact_review = client.post(
                    f"/api/artifacts/{packet['artifacts'][0]['id']}/review",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "task_id": task_id,
                        "decision": "approved",
                        "note": "Reviewer evidence only",
                    },
                )
                self.assertEqual(artifact_review.status_code, 200)
                self.assertEqual(
                    client.get("/api/founder-decisions").json()[0]["approval"]["status"],
                    "pending",
                )
                reviewer_actor = client.post(
                    f"/api/founder-decisions/{gate_id}",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "decision": "approved",
                        "note": "Not authorized",
                        "actor": "Proxy Reviewer",
                    },
                )
                self.assertEqual(reviewer_actor.status_code, 422)
                unconfirmed = client.post(
                    f"/api/founder-decisions/{gate_id}",
                    json={"confirmed": False, "decision": "approved", "actor": "Founder"},
                )
                self.assertEqual(unconfirmed.status_code, 400)

                approved = client.post(
                    f"/api/founder-decisions/{gate_id}",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "decision": "approved",
                        "note": "Founder accepts evidence",
                        "actor": "Founder",
                    },
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                receipt = approved.json()
                self.assertFalse(receipt["idempotent"])
                self.assertEqual(receipt["actor"], "Founder")
                self.assertEqual(receipt["previous_state"], "pending")
                self.assertEqual(receipt["resulting_state"], "approved")
                self.assertEqual(receipt["target"], f"workflow_run:{run_id}")
                self.assertTrue(receipt["timestamp"])

                replay = client.post(
                    f"/api/founder-decisions/{gate_id}",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "decision": "approved",
                        "note": "Replay does not rewrite note",
                        "actor": "Founder",
                    },
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertTrue(replay.json()["idempotent"])
                self.assertEqual(replay.json()["previous_state"], "approved")
                conflict = client.post(
                    f"/api/founder-decisions/{gate_id}",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "decision": "rejected",
                        "note": "Conflicting replay",
                        "actor": "Founder",
                    },
                )
                self.assertEqual(conflict.status_code, 400)
                self.assertEqual(client.get("/api/founder-decisions").json(), [])
                decided = client.get(
                    "/api/founder-decisions", params={"include_decided": True}
                ).json()[0]
                self.assertEqual(decided["approval"]["decision_note"], "Founder accepts evidence")
                events = client.get(
                    "/api/events", params={"action": "approval.approved", "limit": 200}
                ).json()["items"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["payload"]["actor"], "Founder")
                self.assertEqual(events[0]["payload"]["previous_state"], "pending")
                self.assertEqual(events[0]["payload"]["resulting_state"], "approved")
                self.assertEqual(
                    events[0]["payload"]["target"],
                    {"type": "workflow_run", "id": run_id},
                )
                all_event_types = {
                    item["event_type"]
                    for item in client.get("/api/events?limit=200").json()["items"]
                }
                self.assertFalse(
                    any(
                        token in event_type
                        for event_type in all_event_types
                        for token in ("merge", "close", "release", "github.apply")
                    )
                )

    def test_audit_settings_and_github_dry_run_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            database = workspace / ".agent-factory" / "state.db"
            project_id, task_id, run_id = seed(workspace, database)
            source = workspace / "backlog.json"
            source.write_bytes((ROOT / "examples" / "development-backlog.json").read_bytes())
            proposal = load_backlog(source)
            desired = proposal.items[0].issue()
            existing = [
                {
                    "number": 7,
                    "title": desired["title"],
                    "body": desired["body"].replace("Deliver", "Previously deliver", 1),
                    "labels": desired["labels"],
                }
            ]
            headers = {"X-Agent-Factory-Confirm": "true"}
            with TestClient(create_app(workspace, database), base_url="http://localhost") as client:
                settings = client.get("/api/settings").json()
                by_key = {item["key"]: item for item in settings["runtime_settings"]}
                self.assertEqual(by_key["dashboard_refresh_seconds"]["value"], 5)
                self.assertEqual(by_key["dashboard_refresh_seconds"]["version"], 0)

                unconfirmed = client.post(
                    "/api/settings/dashboard_refresh_seconds",
                    json={"confirmed": False, "value": 10},
                )
                self.assertEqual(unconfirmed.status_code, 400)
                for value, version in ((10, 1), (12, 2)):
                    updated = client.post(
                        "/api/settings/dashboard_refresh_seconds",
                        headers=headers,
                        json={"confirmed": True, "value": value},
                    )
                    self.assertEqual(updated.status_code, 200, updated.text)
                    self.assertEqual(updated.json()["version"], version)
                rejected_secret = client.post(
                    "/api/settings/github_token",
                    headers=headers,
                    json={"confirmed": True, "value": 1},
                )
                self.assertEqual(rejected_secret.status_code, 400)
                out_of_range = client.post(
                    "/api/settings/audit_page_size",
                    headers=headers,
                    json={"confirmed": True, "value": 1000},
                )
                self.assertEqual(out_of_range.status_code, 400)

                preview = client.post(
                    "/api/github/preview",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "repo": "owner/repository",
                        "backlog_path": "backlog.json",
                        "existing_issues": existing,
                    },
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                plan = preview.json()
                self.assertTrue(plan["dry_run"])
                self.assertEqual(len(plan["plan_hash"]), 64)
                self.assertEqual(plan["gate_status"], "pending")
                self.assertTrue(any(op["action"] == "update_issue" for op in plan["operations"]))
                self.assertTrue(any(op["action"] == "create_issue" for op in plan["operations"]))
                self.assertTrue(all(not item["executed"] for item in plan["preview"]["results"]))

                escaped = client.post(
                    "/api/github/preview",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "repo": "owner/repository",
                        "backlog_path": "../outside.json",
                        "existing_issues": [],
                    },
                )
                self.assertEqual(escaped.status_code, 400)

                audit = client.get(
                    "/api/events",
                    params={
                        "project_id": project_id,
                        "task_id": task_id,
                        "run_id": run_id,
                        "agent_id": "coding-worker-codex",
                        "provider": "deterministic",
                    },
                ).json()
                self.assertGreater(audit["total"], 0)
                self.assertTrue(
                    any(item["related_artifact_ids"] for item in audit["items"])
                )
                settings_audit = client.get(
                    "/api/events", params={"action": "settings", "outcome": "success"}
                ).json()
                self.assertEqual(settings_audit["total"], 2)
                newest = client.get("/api/events", params={"limit": 1}).json()["items"][0]
                bounded = client.get(
                    "/api/events",
                    params={"from_time": newest["created_at"], "to_time": newest["created_at"]},
                ).json()
                self.assertGreaterEqual(bounded["total"], 1)

            storage = SQLiteStorage(database)
            versions = storage.db.execute(
                """SELECT version,value_json FROM runtime_setting_versions
                     WHERE key='dashboard_refresh_seconds' ORDER BY version"""
            ).fetchall()
            self.assertEqual([(row["version"], row["value_json"]) for row in versions], [(1, "10"), (2, "12")])
            gate = storage.db.execute(
                "SELECT status FROM github_mutation_gates WHERE id=?", (plan["gate_id"],)
            ).fetchone()
            self.assertEqual(gate["status"], "pending")
            storage.close()

    def test_guarded_agent_provider_and_reviewer_routing_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            database = workspace / ".agent-factory" / "state.db"
            _, _, run_id = seed(workspace, database)
            headers = {"X-Agent-Factory-Confirm": "true"}
            with TestClient(create_app(workspace, database), base_url="http://localhost") as client:
                agents = client.get("/api/agents", params={"limit": 200}).json()
                guardian = next(
                    item for item in agents["items"] if item["id"] == "policy-guardian"
                )
                self.assertIn("reviewer_assignment_count", guardian)
                self.assertIn("last_claimed_task_id", guardian)

                providers = client.get("/api/providers", params={"limit": 200}).json()
                codex = next(
                    item for item in providers["items"] if item["id"] == "codex"
                )
                self.assertIn("Policy Reviewer", codex["allowed_roles"])
                self.assertIn("health_details", codex)

                reviews = client.get(
                    "/api/reviews", params={"run_id": run_id, "limit": 200}
                ).json()["items"]
                self.assertEqual(len(reviews), 2)
                for review in reviews:
                    producer_models = {
                        producer["model"].casefold()
                        for producer in review["producer_agents"]
                    }
                    self.assertNotIn(review["reviewer_model"].casefold(), producer_models)
                    self.assertEqual(
                        review["strategy"], "least-used-model-aware-round-robin"
                    )

                unconfirmed = client.post(
                    "/api/agents/policy-guardian/enabled",
                    json={"confirmed": False, "enabled": False},
                )
                self.assertEqual(unconfirmed.status_code, 400)
                disabled = client.post(
                    "/api/agents/policy-guardian/enabled",
                    headers=headers,
                    json={"confirmed": True, "enabled": False},
                )
                self.assertEqual(disabled.status_code, 200, disabled.text)
                self.assertFalse(disabled.json()["agent"]["enabled"])
                self.assertIn("existing evidence remains immutable", disabled.json()["impact_summary"])

                incompatible = client.post(
                    "/api/agents/policy-guardian/provider",
                    headers=headers,
                    json={"confirmed": True, "provider": "openclaw", "model": "x"},
                )
                self.assertEqual(incompatible.status_code, 400)
                self.assertIn("incompatible", incompatible.json()["error"]["message"])

                unsupported = client.post(
                    "/api/agents/policy-guardian/provider", headers=headers,
                    json={"confirmed": True, "provider": "codex", "model": "openai:unknown"},
                )
                self.assertEqual(unsupported.status_code, 400)
                self.assertIn("unknown or unsupported", unsupported.json()["error"]["message"])

                replaced = client.post(
                    "/api/agents/policy-guardian/provider",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "provider": "codex",
                        "model": "openai:gpt-6-astra",
                    },
                )
                self.assertEqual(replaced.status_code, 200, replaced.text)
                self.assertEqual(replaced.json()["agent"]["provider"], "codex")
                persisted = next(
                    item
                    for item in client.get("/api/agents?limit=200").json()["items"]
                    if item["id"] == "policy-guardian"
                )
                self.assertFalse(persisted["enabled"])
                self.assertEqual(persisted["model"], "openai:gpt-6-astra")
                event_names = {
                    item["event_type"]
                    for item in client.get("/api/events?limit=200").json()["items"]
                }
                self.assertIn("agent.disabled", event_names)
                self.assertIn("agent.provider.replaced", event_names)

    def test_guarded_work_item_run_and_review_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            database = workspace / ".agent-factory" / "state.db"
            storage = SQLiteStorage(database)
            service = AgentFactoryService(storage, workspace=workspace)
            project = service.create_project("Controls")
            item = service.create_work_item(
                project_id=project.project_id,
                title="Controlled task",
                description="Confirm every mutation",
                kind="task",
                inputs={"labels": ["priority:high"]},
                acceptance_criteria=["Mutation is audited"],
            )
            dependent = service.create_work_item(
                project_id=project.project_id,
                title="Dependent task",
                description="Exercise dependency filters",
                dependencies=[item.id],
            )
            storage.close()
            headers = {"X-Agent-Factory-Confirm": "true"}
            with TestClient(create_app(workspace, database), base_url="http://localhost") as client:
                rejected = client.post(
                    f"/api/work-items/{item.id}/claim",
                    json={"confirmed": False, "agent_id": "coding-worker-codex"},
                )
                self.assertEqual(rejected.status_code, 400)
                claimed = client.post(
                    f"/api/work-items/{item.id}/claim",
                    headers=headers,
                    json={"confirmed": True, "agent_id": "coding-worker-codex"},
                )
                self.assertEqual(claimed.status_code, 200, claimed.text)
                filtered = client.get(
                    "/api/work-items", params={"assignee": "coding-worker-codex"}
                ).json()
                self.assertEqual(filtered["total"], 1)
                for key, value, expected_id in (
                    ("project_id", project.project_id, item.id),
                    ("kind", "task", item.id),
                    ("status", "pending", item.id),
                    ("priority", "high", item.id),
                    ("dependency", item.id, dependent.id),
                ):
                    with self.subTest(filter=key):
                        result = client.get(
                            "/api/work-items", params={key: value}
                        ).json()
                        self.assertIn(expected_id, [row["id"] for row in result["items"]])
                started = client.post(
                    f"/api/work-items/{item.id}/runs",
                    headers=headers,
                    json={"confirmed": True, "workflow_id": "delivery", "mode": "simulation"},
                )
                self.assertEqual(started.status_code, 200, started.text)
                run_id = started.json()["id"]
                detail = client.get(f"/api/runs/{run_id}/detail").json()
                self.assertEqual(
                    [artifact["stage"] for artifact in detail["artifacts"]],
                    ["policy-precheck", "implementation", "validation", "policy-postcheck"],
                )
                self.assertEqual(detail["stopped_reason"], "Founder decision required")
                artifact_id = detail["artifacts"][0]["id"]
                reviewed = client.post(
                    f"/api/artifacts/{artifact_id}/review",
                    headers=headers,
                    json={
                        "confirmed": True,
                        "task_id": item.id,
                        "decision": "approved",
                        "note": "Evidence checked",
                    },
                )
                self.assertEqual(reviewed.status_code, 200, reviewed.text)
                self.assertEqual(reviewed.json()["status"], "approved")

    def test_dashboard_shell_has_live_navigation_and_explicit_ui_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with TestClient(
                create_app(workspace, workspace / ".agent-factory" / "state.db"),
                base_url="http://localhost",
            ) as client:
                page = client.get("/operations")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Local Control Center", page.text)
                for target in ("#overview", "#work", "#runs", "#agents", "#reviews", "#audit"):
                    self.assertIn(f'href="{target}"', page.text)
                script = client.get("/assets/app.js")
                styles = client.get("/assets/styles.css")
                self.assertEqual(script.status_code, 200)
                self.assertEqual(styles.status_code, 200)
                self.assertIn("setInterval(refresh, 5000)", script.text)
                self.assertIn("Showing the last successful local snapshot", script.text)
                self.assertIn("Dashboard data is unavailable", script.text)
                self.assertIn("No workflow runs yet", script.text)
                self.assertIn('id="work-filters"', page.text)
                self.assertIn('id="confirm-dialog"', page.text)
                self.assertIn("Explicit confirmation", page.text)
                self.assertIn('"X-Agent-Factory-Confirm": "true"', script.text)
                self.assertIn("Run simulation", script.text)
                self.assertIn("Resume unavailable", script.text)
                self.assertIn("Cancel unavailable", script.text)
                self.assertIn('id="agent-list"', page.text)
                self.assertIn('id="routing-list"', page.text)
                self.assertIn("Compatible provider", script.text)
                self.assertIn("Candidate exclusions", script.text)
                self.assertIn("prior approval snapshots will not be reused", script.text)
                self.assertIn('id="audit-filters"', page.text)
                self.assertIn('id="settings-list"', page.text)
                self.assertIn('id="github-preview-form"', page.text)
                self.assertIn("DRY RUN", script.text)
                self.assertIn("unrestricted command arguments", script.text)
                self.assertNotIn("Available in AF-042", page.text)
                self.assertIn('id="founder-dialog"', page.text)
                self.assertIn("Only this separately confirmed Founder action", script.text)
                self.assertIn("no merge, close, release, or GitHub mutation", script.text)
                self.assertIn('id="backlog-import-form"', page.text)
                self.assertNotIn("Available in AF-039", page.text)
                self.assertIn("prefers-reduced-motion", styles.text)


if __name__ == "__main__":
    unittest.main()


class SettingsApiTests(unittest.TestCase):
    """The settings page's contract: see it, check it, change it, undo it."""

    HEADERS = {"X-Agent-Factory-Confirm": "true"}

    def client(self, workspace: str):
        root = Path(workspace)
        return TestClient(
            create_app(root, root / ".agent-factory" / "state.db"),
            base_url="http://localhost",
        )

    def test_sections_describe_every_value_and_its_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                payload = client.get("/api/settings/sections").json()
                self.assertEqual(payload["changed_total"], 0)
                sections = payload["sections"]
                self.assertGreaterEqual(len(sections), 8)
                fields = [field for item in sections for field in item["fields"]]
                self.assertTrue(all(field["origin"] == "default" for field in fields))
                self.assertTrue(all(field["default_source"] for field in fields))
                self.assertTrue(any(not field["reconfigurable"] for field in fields))
                self.assertTrue(any(field["risk"] == "sensitive" for field in fields))
                for field in fields:
                    if field["risk"] == "sensitive":
                        self.assertTrue(field["consequence"])

    def test_a_safe_change_is_applied_and_can_be_undone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/settings/values/godot.max_seconds",
                    json={"confirmed": True, "value": "300", "actor": "miha",
                          "reason": "повільний ПК"},
                    headers=self.HEADERS,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["value"], "300")
                self.assertEqual(response.json()["origin"], "override")

                history = client.get("/api/settings/changes").json()["changes"]
                self.assertEqual(history[0]["key"], "godot.max_seconds")
                self.assertEqual(history[0]["actor"], "miha")

                undo = client.post(
                    "/api/settings/values/godot.max_seconds/reset",
                    json={"confirmed": True, "actor": "miha"}, headers=self.HEADERS,
                )
                self.assertEqual(undo.json()["origin"], "default")
                self.assertEqual(
                    client.get("/api/settings/sections").json()["changed_total"], 0,
                )

    def test_a_change_without_the_confirmation_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/settings/values/godot.max_seconds",
                    json={"confirmed": True, "value": "300", "actor": "miha"},
                )
                self.assertGreaterEqual(response.status_code, 400)
                self.assertEqual(
                    client.get("/api/settings/sections").json()["changed_total"], 0,
                )

    def test_a_sensitive_change_is_refused_until_the_consequence_is_acknowledged(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                body = {"confirmed": True, "value": "false", "actor": "miha"}
                refused = client.post(
                    "/api/settings/values/updates.protect_pins",
                    json=body, headers=self.HEADERS,
                )
                self.assertEqual(refused.status_code, 409)
                self.assertEqual(
                    refused.json()["error"]["code"], "confirmation_required",
                )

                applied = client.post(
                    "/api/settings/values/updates.protect_pins",
                    json={**body, "acknowledged_consequence": True,
                          "reason": "міграція"},
                    headers=self.HEADERS,
                )
                self.assertEqual(applied.status_code, 200)
                self.assertEqual(applied.json()["value"], "false")

                verified = client.post(
                    "/api/settings/sections/updates/verify", json={},
                    headers=self.HEADERS,
                ).json()
                self.assertIn(
                    "problem", {item["level"] for item in verified["findings"]},
                )

    def test_a_derived_value_cannot_be_changed_through_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/settings/values/godot.baseline_series",
                    json={"confirmed": True, "value": "1.0", "actor": "miha"},
                    headers=self.HEADERS,
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["error"]["code"], "not_reconfigurable",
                )

    def test_invalid_values_and_unknown_keys_are_reported_readably(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                invalid = client.post(
                    "/api/settings/values/godot.max_seconds",
                    json={"confirmed": True, "value": "2", "actor": "miha"},
                    headers=self.HEADERS,
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertTrue(invalid.json()["error"]["message"])

                unknown = client.post(
                    "/api/settings/values/nope.nope",
                    json={"confirmed": True, "value": "1", "actor": "miha"},
                    headers=self.HEADERS,
                )
                self.assertEqual(unknown.status_code, 404)
                self.assertEqual(unknown.json()["error"]["code"], "unknown_setting")

                self.assertEqual(
                    client.get("/api/settings/sections/nowhere").status_code, 404,
                )

    def test_the_interface_speaks_both_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                ukrainian = client.get("/api/i18n").json()
                english = client.get("/api/i18n?lang=en").json()
                self.assertEqual(ukrainian["language"], "uk")
                self.assertEqual(english["language"], "en")
                self.assertEqual(set(ukrainian["messages"]), set(english["messages"]))
                self.assertNotEqual(
                    ukrainian["messages"]["settings.title"],
                    english["messages"]["settings.title"],
                )
                self.assertEqual(english["available"], ["uk", "en"])

    def test_the_browser_preference_chooses_the_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                asked = client.get(
                    "/api/settings/sections",
                    headers={"Accept-Language": "en-GB,en;q=0.9,uk;q=0.4"},
                ).json()
                self.assertEqual(asked["language"], "en")
                self.assertEqual(asked["sections"][0]["title"], "Godot engine")
                default = client.get("/api/settings/sections").json()
                self.assertEqual(default["language"], "uk")
                self.assertEqual(default["sections"][0]["title"], "Рушій Godot")

    def test_an_explicit_choice_beats_the_browser_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                payload = client.get(
                    "/api/settings/sections?lang=uk",
                    headers={"Accept-Language": "en"},
                ).json()
                self.assertEqual(payload["language"], "uk")

    def test_a_refusal_reaches_the_person_in_their_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                body = {"confirmed": True, "value": "false", "actor": "miha"}
                ukrainian = client.post(
                    "/api/settings/values/updates.protect_pins",
                    json=body, headers=self.HEADERS,
                ).json()["error"]["message"]
                english = client.post(
                    "/api/settings/values/updates.protect_pins?lang=en",
                    json=body, headers=self.HEADERS,
                ).json()["error"]["message"]
                self.assertIn("Підтвердьте наслідок", ukrainian)
                self.assertIn("Acknowledge the consequence", english)

                nameless = client.post(
                    "/api/settings/values/godot.max_seconds?lang=en",
                    json={"confirmed": True, "value": "300", "actor": " "},
                    headers=self.HEADERS,
                )
                self.assertEqual(nameless.status_code, 400)
                self.assertEqual(
                    nameless.json()["error"]["code"], "actor_required",
                )
                self.assertIn("named person", nameless.json()["error"]["message"])

    def test_the_settings_page_is_served_and_carries_the_brand(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                page = client.get("/settings")
                self.assertEqual(page.status_code, 200)
                self.assertIn("brand.css", page.text)
                self.assertIn("settings.js", page.text)


class WorkStatusApiTests(unittest.TestCase):
    """The progress screen's contract: read the truth, and change nothing."""

    def client(self, workspace: str):
        root = Path(workspace)
        return TestClient(
            create_app(root, root / ".agent-factory" / "state.db"),
            base_url="http://localhost",
        )

    def test_an_unknown_run_is_a_clean_404_rather_than_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                for path in ("/api/work/runs/77", "/api/work/runs/77/stop-plan",
                             "/api/work/runs/77/after-restart"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404, path)
                    self.assertEqual(
                        response.json()["error"]["code"], "unknown_run", path)

    def test_with_no_run_in_flight_the_list_is_empty_rather_than_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                payload = client.get("/api/work/runs").json()
                self.assertEqual(payload["runs"], [])
                self.assertIn(payload["language"], ("uk", "en"))

    def test_the_page_and_its_answers_follow_the_asked_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                english = client.get("/api/work/runs?lang=en").json()
                self.assertEqual(english["language"], "en")
                page = client.get("/work?lang=en")
                self.assertEqual(page.status_code, 200)
                self.assertIn("<html", page.text.casefold())

    def test_reading_the_status_never_offers_a_way_to_change_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                paths = client.get("/api/openapi.json").json()["paths"]
                work = {path: set(operations) for path, operations in paths.items()
                        if path.startswith("/api/work/")}
                self.assertTrue(work)
                self.assertTrue(all(methods == {"get"} for methods in work.values()))


class FeedbackApiTests(unittest.TestCase):
    """Nothing is sent without a preview, and nothing costs money without a yes."""

    HEADERS = {"X-Agent-Factory-Confirm": "true"}

    def client(self, workspace: str):
        root = Path(workspace)
        return TestClient(
            create_app(root, root / ".agent-factory" / "state.db"),
            base_url="http://localhost",
        )

    def note(self, **rest):
        body = {
            "confirmed": True,
            "played_version": "a" * 64,
            "wish": "зроби стрибок вищим",
            "steps": ["натиснути пробіл"],
        }
        body.update(rest)
        return body

    def test_a_preview_shows_what_would_be_sent_and_stores_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                preview = client.post(
                    "/api/games/collector/feedback/preview?lang=en",
                    json=self.note(attachments=[{
                        "kind": "screenshot", "name": "jump.png",
                        "leaves_machine": True,
                    }]),
                    headers=self.HEADERS,
                ).json()
                self.assertEqual(preview["leaves_machine"], ["jump.png"])
                self.assertIn("jump.png", preview["transmission"])
                self.assertEqual(
                    client.get("/api/games/collector/feedback").json()["feedback"], [])

    def test_a_note_without_confirmation_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/games/collector/feedback", json=self.note(confirmed=False))
                self.assertEqual(response.status_code, 400)

    def test_an_empty_note_is_refused_in_the_asked_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/games/collector/feedback?lang=en",
                    json=self.note(wish="   "), headers=self.HEADERS,
                )
                self.assertEqual(response.status_code, 409)
                self.assertIn("Say what to change", response.json()["error"]["message"])
                ukrainian = client.post(
                    "/api/games/collector/feedback?lang=uk",
                    json=self.note(wish="   "), headers=self.HEADERS,
                )
                self.assertNotEqual(
                    ukrainian.json()["error"]["message"],
                    response.json()["error"]["message"],
                )

    def test_a_plan_that_costs_money_is_not_accepted_without_saying_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                feedback_id = client.post(
                    "/api/games/collector/feedback", json=self.note(),
                    headers=self.HEADERS,
                ).json()["feedback_id"]
                plan = client.post(
                    f"/api/feedback/{feedback_id}/plans?lang=en",
                    json={
                        "confirmed": True,
                        "changes": [{"uk": "Вищий стрибок", "en": "Higher jump"}],
                        "cost": {"amount": 0.4, "unit": "USD"},
                    },
                    headers=self.HEADERS,
                ).json()
                self.assertFalse(plan["accepted"])
                self.assertTrue(plan["needs_acceptance"])
                refused = client.post(
                    f"/api/feedback/plans/{plan['plan_id']}/accept?lang=en",
                    json={"confirmed": True, "actor": "miha"}, headers=self.HEADERS,
                )
                self.assertEqual(refused.status_code, 409)
                self.assertEqual(refused.json()["error"]["code"], "feedback_refused")

                accepted = client.post(
                    f"/api/feedback/plans/{plan['plan_id']}/accept",
                    json={"confirmed": True, "actor": "miha", "accept_cost": True},
                    headers=self.HEADERS,
                ).json()
                self.assertEqual(accepted["accepted_by"], "miha")

    def test_the_verdict_says_unchecked_until_something_checks_the_wish(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                feedback_id = client.post(
                    "/api/games/collector/feedback", json=self.note(),
                    headers=self.HEADERS,
                ).json()["feedback_id"]
                detail = client.get(f"/api/feedback/{feedback_id}?lang=en").json()
                self.assertEqual(detail["verdict"]["state"], "not_checked")
                self.assertIn("not evidence", detail["verdict"]["summary"])

    def test_an_unknown_note_is_a_clean_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.get("/api/feedback/404")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"], "unknown_feedback")


class StudioLoopApiTests(unittest.TestCase):
    """Pause, comment, continue - and a slice that has to exist to be offered."""

    HEADERS = {"X-Agent-Factory-Confirm": "true"}

    def client(self, workspace: str):
        root = Path(workspace)
        return TestClient(
            create_app(root, root / ".agent-factory" / "state.db"),
            base_url="http://localhost",
        )

    def test_a_slice_with_no_build_behind_it_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/studio/slices/m1/base?lang=en",
                    json={
                        "confirmed": True, "outcome": "playable",
                        "project_key": "collector", "version_digest": "a" * 64,
                    },
                    headers=self.HEADERS,
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "slice_refused")
                self.assertIn("no such built version", response.json()["error"]["message"])

    def test_a_stage_may_say_there_is_nothing_to_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                declared = client.post(
                    "/api/studio/slices/m1/setup?lang=en",
                    json={
                        "confirmed": True, "outcome": "nothing_to_test",
                        "reason_uk": "Лише підготовка середовища.",
                        "reason_en": "Only the environment was prepared.",
                    },
                    headers=self.HEADERS,
                ).json()
                self.assertFalse(declared["playable"])
                report = client.get("/api/studio/slices/m1?lang=en").json()
                self.assertEqual(report["playable"], [])
                self.assertIn("environment", report["stages"]["setup"]["reason"])

    def test_the_loop_pauses_takes_a_comment_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                paused = client.post(
                    "/api/studio/cycles/m1/pause?lang=en",
                    json={"confirmed": True, "actor": "miha", "finishing": ["build"]},
                    headers=self.HEADERS,
                ).json()
                self.assertIn("No new task", paused["summary"])
                client.post(
                    "/api/studio/cycles/m1/comments",
                    json={"confirmed": True, "text": "хочу подвійний стрибок"},
                    headers=self.HEADERS,
                )
                resumed = client.post(
                    "/api/studio/cycles/m1/resume?lang=en",
                    json={"confirmed": True, "actor": "miha"},
                    headers=self.HEADERS,
                ).json()
                self.assertEqual(
                    [item["text"] for item in resumed["comments"]],
                    ["хочу подвійний стрибок"])
                self.assertIn("Until it replans", resumed["note"])
                self.assertEqual(
                    client.get("/api/studio/cycles/m1").json()["cycle"], 2)

    def test_continuing_a_mission_that_is_not_paused_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/studio/cycles/m1/resume?lang=en",
                    json={"confirmed": True, "actor": "miha"}, headers=self.HEADERS,
                )
                self.assertEqual(response.status_code, 409)
                self.assertIn("not paused", response.json()["error"]["message"])

    def test_every_studio_change_needs_the_confirmation_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                for path, body in (
                    ("/api/studio/cycles/m1/pause", {"actor": "miha"}),
                    ("/api/studio/cycles/m1/comments", {"text": "щось"}),
                ):
                    response = client.post(path, json={"confirmed": False, **body})
                    self.assertEqual(response.status_code, 400, path)


class StudioMoneyApiTests(unittest.TestCase):
    """The limit stops the work by asking, and a paid tool is never a dead end."""

    HEADERS = {"X-Agent-Factory-Confirm": "true"}

    def client(self, workspace: str):
        root = Path(workspace)
        return TestClient(
            create_app(root, root / ".agent-factory" / "state.db"),
            base_url="http://localhost",
        )

    def test_with_no_limit_the_report_says_there_is_nothing_to_stop_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                report = client.get("/api/studio/cost/m1?lang=en&next_step=99").json()
                self.assertFalse(report["over"])
                self.assertIn("No limit", report["summary"])
                self.assertFalse(report["forecast"]["known"])

    def test_a_limit_is_recorded_and_a_step_over_it_becomes_a_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                client.post(
                    "/api/studio/cost/m1/limit",
                    json={"confirmed": True, "amount": 1.0, "actor": "miha"},
                    headers=self.HEADERS,
                )
                report = client.get("/api/studio/cost/m1?lang=en&next_step=2").json()
                self.assertTrue(report["over"])
                self.assertEqual(report["question"]["reason"], "over_budget")
                self.assertEqual(
                    report["question"]["options"], ["raise_limit", "stop"])

    def test_a_paid_tool_offers_the_ways_out_and_never_asks_for_a_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                choice = client.post(
                    "/api/studio/paid-tools/m1?lang=en",
                    json={
                        "confirmed": True, "tool": "Unity",
                        "reason_uk": "Потрібен Unity.", "reason_en": "Unity is needed.",
                        "blocks": ["3D level"],
                    },
                    headers=self.HEADERS,
                ).json()
                self.assertIn("decline", [way["key"] for way in choice["ways_out"]])
                self.assertIn("neither ask", choice["credentials"])

                answered = client.post(
                    f"/api/studio/paid-tools/choices/{choice['choice_id']}?lang=en",
                    json={"confirmed": True, "choice": "decline", "actor": "miha"},
                    headers=self.HEADERS,
                ).json()
                self.assertEqual(answered["rebuild"]["cut"], ["3D level"])
                report = client.get("/api/studio/paid-tools/m1?lang=en").json()
                self.assertEqual(report["cut"], ["3D level"])
                self.assertEqual(report["blocked"], [])

    def test_an_unknown_choice_is_a_clean_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/studio/paid-tools/choices/404",
                    json={"confirmed": True, "choice": "decline", "actor": "miha"},
                    headers=self.HEADERS,
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"], "unknown_choice")


class StudioSetupApiTests(unittest.TestCase):
    """Who is in the studio, which machine answers, and whether work can start."""

    HEADERS = {"X-Agent-Factory-Confirm": "true"}

    def client(self, workspace: str):
        root = Path(workspace)
        return TestClient(
            create_app(root, root / ".agent-factory" / "state.db"),
            base_url="http://localhost",
        )

    def test_a_new_game_has_two_roles_and_the_rest_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                report = client.get("/api/studio/roster/m1?lang=en").json()
                self.assertEqual(report["enabled"], ["planner", "developer"])
                self.assertIn("engine", report["acceptance"])

    def test_the_consequence_of_adding_a_role_is_readable_before_adding_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                consequence = client.get(
                    "/api/studio/roster/m1/consequence/tester?lang=en&concurrency=parallel"
                ).json()
                self.assertTrue(consequence["another_subscription"])
                self.assertTrue(consequence["acceptance_changes"])
                self.assertEqual(
                    client.get("/api/studio/roster/m1").json()["enabled"],
                    ["planner", "developer"],
                    "reading a consequence must not enable the role")

    def test_adding_a_tester_moves_acceptance_away_from_the_developer(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                added = client.post(
                    "/api/studio/roster/m1/tester?lang=en",
                    json={"confirmed": True, "actor": "miha"}, headers=self.HEADERS,
                ).json()
                self.assertIn("tester", added["enabled"])
                self.assertFalse(added["developer_accepts_own_work"])
                self.assertIn("no longer accepts", added["acceptance"])

    def test_the_minimum_pair_cannot_be_switched_off_over_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                response = client.post(
                    "/api/studio/roster/m1/developer?action=disable&lang=en",
                    json={"confirmed": True, "actor": "miha"}, headers=self.HEADERS,
                )
                self.assertEqual(response.status_code, 409)
                self.assertIn("minimum", response.json()["error"]["message"])

    def test_the_web_container_is_never_offered_as_a_place_to_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                client.post(
                    "/api/studio/machines/web",
                    json={
                        "confirmed": True, "name": "lokvetia-core-web",
                        "kind": "web_container",
                    },
                    headers=self.HEADERS,
                )
                overview = client.get("/api/studio/machines?lang=en").json()
                self.assertFalse(overview["machines"][0]["builds"])
                self.assertIn("build", overview["never_on_the_web_container"])

    def test_work_cannot_start_until_a_source_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                first = client.get("/api/studio/first-run?lang=en").json()
                self.assertFalse(first["can_start"])
                self.assertIn("no source of execution", first["summary"])

                client.post(
                    "/api/studio/first-run/claude",
                    json={
                        "confirmed": True, "kind": "own_subscription",
                        "name": "Claude",
                    },
                    headers=self.HEADERS,
                )
                waiting = client.get("/api/studio/first-run?lang=en").json()
                self.assertFalse(waiting["can_start"])
                self.assertIn("no source has been checked", waiting["summary"])

                client.post(
                    "/api/studio/first-run/claude",
                    json={
                        "confirmed": True, "kind": "own_subscription",
                        "name": "Claude", "state": "verified",
                    },
                    headers=self.HEADERS,
                )
                self.assertTrue(client.get("/api/studio/first-run").json()["can_start"])

    def test_a_game_nobody_delegated_reports_nobody(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                report = client.get("/api/studio/supervisor/m1?lang=en").json()
                self.assertFalse(report["running_on_its_own"])
                self.assertIsNone(report["mandate"])
                self.assertEqual(report["steps"], [])

    def test_a_mandate_needs_confirmation_like_every_other_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                refused = client.post(
                    "/api/studio/supervisor/m1",
                    json={"confirmed": True, "actor": "Miha", "steps": ["plan"]},
                )
                self.assertEqual(refused.status_code, 400)
                self.assertFalse(
                    client.get("/api/studio/supervisor/m1").json()["running_on_its_own"])

    def test_granting_and_taking_back_leaves_the_record_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                granted = client.post(
                    "/api/studio/supervisor/m1?lang=en",
                    json={"confirmed": True, "actor": "Miha", "steps": ["plan"],
                          "ceiling": 20, "hours": 6},
                    headers=self.HEADERS,
                ).json()
                self.assertEqual(granted["granted_by"], "Miha")
                self.assertTrue(granted["live"])
                self.assertTrue(
                    client.get("/api/studio/supervisor/m1").json()["running_on_its_own"])
                revoked = client.post(
                    "/api/studio/supervisor/m1/revoke?lang=en",
                    json={"confirmed": True, "actor": "Miha"},
                    headers=self.HEADERS,
                ).json()
                self.assertFalse(revoked["live"])
                self.assertFalse(
                    client.get("/api/studio/supervisor/m1").json()["running_on_its_own"])

    def test_a_step_the_studio_does_not_know_is_refused_in_the_persons_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                answer = client.post(
                    "/api/studio/supervisor/m1?lang=en",
                    json={"confirmed": True, "actor": "Miha", "steps": ["ship_it"]},
                    headers=self.HEADERS,
                )
                self.assertEqual(answer.status_code, 409)
                self.assertEqual(answer.json()["error"]["code"], "mandate_refused")
                self.assertIn("ship_it", answer.json()["error"]["message"])

    def test_taking_back_what_was_never_given_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                answer = client.post(
                    "/api/studio/supervisor/m1/revoke?lang=en",
                    json={"confirmed": True, "actor": "Miha"},
                    headers=self.HEADERS,
                )
                self.assertEqual(answer.status_code, 409)
                self.assertIn("no live mandate", answer.json()["error"]["message"])

    def test_a_local_model_is_refused_when_the_card_will_not_carry_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.client(tmp) as client:
                client.post(
                    "/api/studio/machines/pc",
                    json={
                        "confirmed": True, "name": "desktop", "kind": "this_pc",
                        "video_memory_gb": 6,
                    },
                    headers=self.HEADERS,
                )
                source = client.post(
                    "/api/studio/first-run/llama?lang=en",
                    json={
                        "confirmed": True, "kind": "local_model", "name": "Llama 70B",
                        "machine_key": "pc", "needed_gb": 40,
                    },
                    headers=self.HEADERS,
                ).json()
                self.assertFalse(source["usable"])
                self.assertIn("will not run locally", source["detail"])
                self.assertFalse(client.get("/api/studio/first-run").json()["can_start"])
