import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Runs:
    """Running the command line, without being a test case itself.

    Inheriting from a TestCase that already has tests reruns those tests in
    every subclass, so what is shared is the helper, not the suite.
    """

    def run_cli(self, workspace: Path, *arguments: str):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_factory",
                "--workspace",
                str(workspace),
                *arguments,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )


class CLITests(Runs, unittest.TestCase):
    def test_from_zero_init_demo_and_approval_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            initialized = self.run_cli(workspace, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            details = json.loads(initialized.stdout)
            self.assertTrue(Path(details["database"]).is_file())
            demonstrated = self.run_cli(workspace, "demo")
            self.assertEqual(demonstrated.returncode, 0, demonstrated.stderr)
            self.assertIn("STOPPED AT HUMAN APPROVAL", demonstrated.stdout)
            repeated = self.run_cli(workspace, "demo")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn("Run 1: delivery", repeated.stdout)
            approvals = self.run_cli(workspace, "approvals", "list")
            self.assertEqual(approvals.returncode, 0, approvals.stderr)
            self.assertEqual(json.loads(approvals.stdout)[0]["status"], "pending")
            reviews = self.run_cli(workspace, "reviews", "list", "--run-id", "1")
            self.assertEqual(reviews.returncode, 0, reviews.stderr)
            self.assertEqual(len(json.loads(reviews.stdout)), 2)

    def test_project_and_work_item_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            created = self.run_cli(
                workspace,
                "project",
                "init",
                "--name",
                "Example Product",
                "--description",
                "Independent example",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            project_id = json.loads(created.stdout)["project_id"]
            item = self.run_cli(
                workspace,
                "work-item",
                "create",
                "--project-id",
                str(project_id),
                "--title",
                "First capability",
                "--description",
                "Deliver a testable result",
                "--kind",
                "task",
                "--acceptance",
                "The result is observable",
            )
            self.assertEqual(item.returncode, 0, item.stderr)
            listed = self.run_cli(
                workspace, "work-item", "list", "--project-id", str(project_id)
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)[0]["title"], "First capability")

    def test_backlog_validate_and_idempotent_local_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            manifest = workspace / "backlog.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": [
                            {
                                "stable_id": "example:task:first",
                                "kind": "task",
                                "title": "First task",
                                "description": "Produce evidence",
                                "acceptance_criteria": ["Evidence is reviewable"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            project = self.run_cli(workspace, "project", "init", "--name", "Example")
            project_id = json.loads(project.stdout)["project_id"]
            validated = self.run_cli(
                workspace, "backlog", "validate", "--path", str(manifest)
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["valid"])
            first = self.run_cli(
                workspace,
                "backlog",
                "import",
                "--path",
                str(manifest),
                "--project-id",
                str(project_id),
            )
            second = self.run_cli(
                workspace,
                "backlog",
                "import",
                "--path",
                str(manifest),
                "--project-id",
                str(project_id),
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(len(json.loads(first.stdout)["created"]), 1)
            self.assertEqual(json.loads(second.stdout)["skipped"], ["example:task:first"])


if __name__ == "__main__":
    unittest.main()


class ParserShapeTests(unittest.TestCase):
    """Guards against a subparser variable shadowing the root parser."""

    def parser(self):
        sys.path.insert(0, str(ROOT / "src"))
        from agent_factory.cli import parser

        return parser()

    def test_the_root_parser_accepts_every_top_level_command(self):
        root = self.parser()
        commands = (
            ("init",),
            ("env", "check"),
            ("godot", "templates"),
            ("assets", "inspect", "--archive", "p.zip"),
            ("playable", "current", "--project", "demo"),
            ("export", "targets"),
            ("support", "categories"),
            ("uninstall", "plan"),
            ("levels", "list"),
            ("unity", "catalogue"),
            ("settings", "show"),
            ("state", "check"),
        )
        for arguments in commands:
            with self.subTest(command=arguments[0]):
                parsed = root.parse_args(["--workspace", ".", *arguments])
                self.assertEqual(parsed.command, arguments[0])

    def test_export_subcommands_keep_their_own_arguments(self):
        root = self.parser()
        parsed = root.parse_args([
            "export", "build", "--project", "demo", "--path", ".",
            "--target", "linux-x86_64", "--output", "out.zip",
        ])
        self.assertEqual((parsed.command, parsed.action), ("export", "build"))
        self.assertEqual(parsed.output, "out.zip")
        preflight = root.parse_args([
            "export", "preflight", "--project", "demo", "--path", ".",
            "--target", "linux-x86_64",
        ])
        self.assertEqual(preflight.action, "preflight")
        self.assertFalse(hasattr(preflight, "output"))


class FeedbackCommandTests(Runs, unittest.TestCase):
    """The command line follows the same rules the API does."""

    def test_a_preview_sends_nothing_and_stores_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.run_cli(workspace, "init")
            previewed = self.run_cli(
                workspace, "feedback", "preview", "--project", "collector",
                "--version", "a" * 64, "--wish", "make the jump higher",
                "--send-file", "jump.png", "--language", "en",
            )
            self.assertEqual(previewed.returncode, 0, previewed.stderr)
            payload = json.loads(previewed.stdout)
            self.assertEqual(payload["leaves_machine"], ["jump.png"])
            listed = self.run_cli(
                workspace, "feedback", "history", "--project", "collector")
            self.assertEqual(json.loads(listed.stdout)["feedback"], [])

    def test_a_recorded_note_comes_back_with_an_unchecked_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.run_cli(workspace, "init")
            added = self.run_cli(
                workspace, "feedback", "add", "--project", "collector",
                "--version", "a" * 64, "--wish", "make the jump higher",
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            identifier = json.loads(added.stdout)["feedback_id"]
            shown = self.run_cli(
                workspace, "feedback", "show", "--id", str(identifier),
                "--language", "en",
            )
            verdict = json.loads(shown.stdout)["verdict"]
            self.assertEqual(verdict["state"], "not_checked")
            self.assertFalse(verdict["confirmed"])


class SupervisorCommandTests(Runs, unittest.TestCase):
    """Handing the studio the keys, and taking them back, from the command line."""

    def delegated(self, workspace, *extra):
        self.run_cli(workspace, "init")
        return self.run_cli(
            workspace, "studio", "mandate", "--mission", "cat-coins",
            "--actor", "Miha", "--allow", "plan", "--language", "en", *extra)

    def test_a_mandate_names_the_person_the_steps_and_its_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            granted = self.delegated(workspace, "--ceiling", "20")
            self.assertEqual(granted.returncode, 0, granted.stderr)
            mandate = json.loads(granted.stdout)
            self.assertEqual(mandate["granted_by"], "Miha")
            self.assertEqual(mandate["steps"], ["plan"])
            self.assertEqual(mandate["ceiling"], 20.0)
            self.assertTrue(mandate["expires_at"])
            self.assertTrue(mandate["live"])

    def test_a_step_the_studio_does_not_know_is_refused_by_the_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.run_cli(workspace, "init")
            refused = self.run_cli(
                workspace, "studio", "mandate", "--mission", "cat-coins",
                "--actor", "Miha", "--allow", "ship_it")
            self.assertNotEqual(refused.returncode, 0)

    def test_running_without_a_source_says_so_and_asks_for_nobody(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.delegated(workspace)
            ran = self.run_cli(
                workspace, "studio", "run", "--mission", "cat-coins",
                "--mission-id", "1", "--once", "--language", "en")
            steps = json.loads(ran.stdout)
            self.assertEqual(steps[0]["outcome"], "waiting")
            self.assertIn("no checked source", steps[0]["summary"])
            # Waiting on the world is not the same as waiting on a person.
            self.assertEqual(ran.returncode, 0)

    def test_running_with_nobody_in_charge_refuses_and_calls_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.run_cli(workspace, "init")
            ran = self.run_cli(
                workspace, "studio", "run", "--mission", "cat-coins",
                "--mission-id", "1", "--once", "--language", "en")
            steps = json.loads(ran.stdout)
            self.assertEqual(steps[0]["outcome"], "refused")
            self.assertIn("Nobody has asked", steps[0]["summary"])

    def test_the_history_shows_the_mandate_and_what_it_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.delegated(workspace)
            listed = self.run_cli(
                workspace, "studio", "steps", "--mission", "cat-coins",
                "--language", "en")
            report = json.loads(listed.stdout)
            self.assertTrue(report["running_on_its_own"])
            self.assertEqual(report["may"], ["plan"])

    def test_taking_the_keys_back_keeps_what_was_already_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.delegated(workspace)
            self.run_cli(workspace, "studio", "run", "--mission", "cat-coins",
                         "--mission-id", "1", "--once")
            revoked = self.run_cli(
                workspace, "studio", "revoke-mandate", "--mission", "cat-coins",
                "--actor", "Miha", "--language", "en")
            self.assertEqual(revoked.returncode, 0, revoked.stderr)
            self.assertFalse(json.loads(revoked.stdout)["live"])
            listed = self.run_cli(
                workspace, "studio", "steps", "--mission", "cat-coins",
                "--language", "en")
            report = json.loads(listed.stdout)
            self.assertFalse(report["running_on_its_own"])
            self.assertEqual(len(report["steps"]), 1, "the record survives the revocation")

    def test_revoking_what_was_never_given_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.run_cli(workspace, "init")
            refused = self.run_cli(
                workspace, "studio", "revoke-mandate", "--mission", "cat-coins",
                "--actor", "Miha", "--language", "en")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("no live mandate", refused.stdout + refused.stderr)
