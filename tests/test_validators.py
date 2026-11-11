import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agent_factory.models import WorkItem
from agent_factory.sandbox import (
    SandboxBackend,
    SandboxManager,
    SandboxUnavailableError,
    UnavailableSandboxBackend,
    _canonical_command,
)
from agent_factory.storage import SQLiteStorage
from agent_factory.validators import (
    VALIDATOR_CATEGORIES,
    ValidatorPack,
    ValidatorRunner,
    load_validator_pack,
)


class RecordingBackend(SandboxBackend):
    name = "recording-enforced"

    def __init__(self):
        self.commands = []

    def availability(self):
        return True, "test backend"

    def wrap(self, policy, command, control_dir):
        del policy, control_dir
        self.commands.append(command)
        return list(command)


class ToolRootBlockedBackend(SandboxBackend):
    """Mirrors WindowsAppContainerBackend rejecting a command outside tool roots."""

    name = "appcontainer-test"

    def __init__(self, blocked_categories):
        self.blocked_categories = set(blocked_categories)
        self.commands = []

    def availability(self):
        return True, "test backend"

    def wrap(self, policy, command, control_dir):
        del policy, control_dir
        self.commands.append(command)
        joined = " ".join(command)
        if any(f"{category}:ok" in joined for category in self.blocked_categories):
            raise SandboxUnavailableError(
                "Sandbox command is outside every qualified tool root"
            )
        return list(command)


class DeterministicValidatorRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        self.worktree = self.workspace / "worktrees" / "candidate"
        self.worktree.mkdir(parents=True)
        self.storage = SQLiteStorage(self.workspace / "state.db")
        self.project_id = self.storage.create_project("Validators", "AF-052")
        self.criterion = "All project validators pass"
        self.task_id = self.storage.create_task(WorkItem(
            "Validate candidate", "Run project pack", self.project_id,
            acceptance_criteria=[self.criterion],
        ))
        self.claim = self.storage.claim_runnable_task(
            self.task_id, "validator", "validator-runner",
            conflict_domains=["path:validators"],
        )
        self.attempt_id = self.storage.create_assignment_attempt(
            self.claim.assignment_id, self.claim.fencing_token
        )
        self.worktree_id = self.storage.create_managed_worktree(
            assignment_id=self.claim.assignment_id,
            fencing_token=self.claim.fencing_token,
            repository=str(self.workspace / "repository"), base_sha="a" * 40,
            branch="agent-factory/validator", path=str(self.worktree),
            attempt_id=self.attempt_id,
        )
        self.storage.transition_managed_worktree(self.worktree_id, "ready")
        self.backend = RecordingBackend()
        self.runner = ValidatorRunner(
            self.storage, self.workspace,
            SandboxManager(self.storage, self.workspace, backend=self.backend),
        )

    def tearDown(self):
        self.storage.close()
        self.temporary.cleanup()

    def pack(self, *, failing=False):
        commands = {}
        for category in VALIDATOR_CATEGORIES:
            code = f"print('{category}:ok')"
            if category == "test":
                code = "from pathlib import Path; p=Path('validation-marker.txt'); p.write_text('candidate'); p.unlink(); print(Path.cwd().name)"
            if failing and category == "lint":
                code = "print('lint failed'); raise SystemExit(2)"
            commands[category] = (sys.executable, "-c", code)
        return ValidatorPack.create("test-pack", commands)

    def mappings(self):
        return {category: (self.criterion,) for category in VALIDATOR_CATEGORIES}

    def test_allowlisted_pack_runs_shell_free_in_candidate_and_maps_evidence(self):
        result = self.runner.run(
            assignment_id=self.claim.assignment_id,
            fencing_token=self.claim.fencing_token,
            attempt_id=self.attempt_id,
            worktree_id=self.worktree_id,
            candidate_digest="b" * 64,
            pack=self.pack(), criterion_mappings=self.mappings(),
            max_seconds=5, max_output_chars=1000,
        )
        self.assertTrue(result.passed)
        self.assertEqual(len(result.results), 5)
        self.assertFalse((self.worktree / "validation-marker.txt").exists())
        self.assertFalse((self.workspace / "validation-marker.txt").exists())
        self.assertEqual(len(self.backend.commands), 5)
        self.assertTrue(all(isinstance(command, tuple) for command in self.backend.commands))
        rows = self.storage.db.execute("SELECT * FROM validator_results ORDER BY id").fetchall()
        self.assertEqual({row["category"] for row in rows}, set(VALIDATOR_CATEGORIES))
        for row in rows:
            self.assertEqual(len(row["command_digest"]), 64)
            self.assertEqual(json.loads(row["criterion_mappings_json"]), [self.criterion])
            environment = json.loads(row["environment_json"])
            self.assertEqual(environment["cwd_scope"], "candidate_worktree")
            self.assertEqual(environment["network"], "deny")
            self.assertNotIn("PATH", environment)
        with self.assertRaisesRegex(sqlite3.DatabaseError, "immutable"):
            self.storage.db.execute("UPDATE validator_results SET status='failed' WHERE id=?", (rows[0]["id"],))

    def test_validator_scratch_writes_do_not_count_as_modifying_the_candidate(self):
        commands = {
            category: (
                sys.executable, "-c",
                "import os; from pathlib import Path; "
                "Path(os.environ['TMP'], 'cache.bin').write_text('scratch'); "
                f"print('{category}:ok')",
            )
            for category in VALIDATOR_CATEGORIES
        }
        result = self.runner.run(
            assignment_id=self.claim.assignment_id,
            fencing_token=self.claim.fencing_token,
            attempt_id=self.attempt_id,
            worktree_id=self.worktree_id,
            candidate_digest="c" * 64,
            pack=ValidatorPack.create("scratch-pack", commands),
            criterion_mappings=self.mappings(),
            max_seconds=15, max_output_chars=1000,
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            {entry.status for entry in result.results}, {"succeeded"}
        )
        self.assertFalse((self.worktree / "cache.bin").exists())
        self.assertEqual(sorted(path.name for path in self.worktree.iterdir()), [])
        rows = self.storage.db.execute(
            "SELECT stderr FROM validator_results ORDER BY id"
        ).fetchall()
        for row in rows:
            self.assertNotIn("modified candidate files", row["stderr"])
        # The manager removes each declared scratch path once evidence is captured.
        self.assertFalse(
            (self.workspace / ".agent-factory" / "sandbox-temp" / "validator").exists()
            and any(
                (self.workspace / ".agent-factory" / "sandbox-temp" / "validator").rglob("cache.bin")
            )
        )

    def test_pack_rejects_shell_strings_and_failed_command_is_bounded_evidence(self):
        default_pack = load_validator_pack(
            Path(__file__).parents[1] / "src" / "agent_factory" / "defaults" / "validator-packs.json",
            "python-unittest",
        )
        self.assertEqual(set(default_pack.commands), set(VALIDATOR_CATEGORIES))
        invalid = {category: (sys.executable, "--version") for category in VALIDATOR_CATEGORIES}
        invalid["test"] = "python -m unittest"
        with self.assertRaisesRegex(ValueError, "never shell strings"):
            ValidatorPack.create("invalid", invalid)

        result = self.runner.run(
            assignment_id=self.claim.assignment_id,
            fencing_token=self.claim.fencing_token,
            attempt_id=self.attempt_id,
            worktree_id=self.worktree_id,
            candidate_digest="c" * 64,
            pack=self.pack(failing=True), criterion_mappings=self.mappings(),
            max_seconds=5, max_output_chars=100,
        )
        self.assertFalse(result.passed)
        lint = self.storage.db.execute(
            "SELECT * FROM validator_results WHERE candidate_digest=? AND category='lint'",
            ("c" * 64,),
        ).fetchone()
        self.assertEqual(lint["status"], "failed")
        self.assertEqual(lint["exit_code"], 2)
        self.assertLessEqual(len(lint["stdout"]) + len(lint["stderr"]), 100)

    def test_tool_root_block_records_honest_evidence_for_every_category(self):
        backend = ToolRootBlockedBackend({"lint", "security_scan"})
        runner = ValidatorRunner(
            self.storage, self.workspace,
            SandboxManager(self.storage, self.workspace, backend=backend),
        )
        result = runner.run(
            assignment_id=self.claim.assignment_id,
            fencing_token=self.claim.fencing_token,
            attempt_id=self.attempt_id,
            worktree_id=self.worktree_id,
            candidate_digest="e" * 64,
            pack=self.pack(), criterion_mappings=self.mappings(),
            max_seconds=5, max_output_chars=1000,
        )
        self.assertFalse(result.passed)
        self.assertEqual(len(result.results), 5)
        statuses = {entry.category: entry.status for entry in result.results}
        self.assertEqual(statuses["lint"], "failed")
        self.assertEqual(statuses["security_scan"], "failed")
        self.assertEqual(statuses["test"], "succeeded")
        self.assertEqual(statuses["type_check"], "succeeded")
        self.assertEqual(statuses["build"], "succeeded")
        rows = {
            row["category"]: row
            for row in self.storage.db.execute(
                "SELECT * FROM validator_results WHERE candidate_digest=?", ("e" * 64,)
            ).fetchall()
        }
        for category in ("lint", "security_scan"):
            self.assertIsNone(rows[category]["exit_code"])
            self.assertIn("outside every qualified tool root", rows[category]["stderr"])
            self.assertIn("before execution", rows[category]["stderr"])
            # Same digest the sandbox records for an executed command, so blocked
            # and executed rows of one command stay comparable.
            canonical = json.dumps(
                list(_canonical_command(self.pack().commands[category])),
                separators=(",", ":"), ensure_ascii=False,
            )
            self.assertEqual(
                rows[category]["command_digest"],
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(len(rows[category]["evidence_digest"]), 64)
            self.assertEqual(rows[category]["evidence_directory"], "")
        for category in ("test", "type_check", "build"):
            self.assertEqual(rows[category]["status"], "succeeded")
            self.assertEqual(rows[category]["exit_code"], 0)
        events = self.storage.db.execute(
            "SELECT event_type FROM events WHERE event_type='validator.blocked'"
        ).fetchall()
        self.assertEqual(len(events), 2)

    def test_unavailable_backend_records_every_category_instead_of_raising(self):
        runner = ValidatorRunner(
            self.storage, self.workspace,
            SandboxManager(
                self.storage, self.workspace,
                backend=UnavailableSandboxBackend("tool roots are not configured"),
            ),
        )
        result = runner.run(
            assignment_id=self.claim.assignment_id,
            fencing_token=self.claim.fencing_token,
            attempt_id=self.attempt_id,
            worktree_id=self.worktree_id,
            candidate_digest="f" * 64,
            pack=self.pack(), criterion_mappings=self.mappings(),
            max_seconds=5, max_output_chars=1000,
        )
        self.assertFalse(result.passed)
        self.assertEqual(
            [entry.category for entry in result.results], list(VALIDATOR_CATEGORIES)
        )
        self.assertTrue(all(entry.status == "failed" for entry in result.results))
        self.assertTrue(all(entry.exit_code is None for entry in result.results))
        stderr = [
            row["stderr"] for row in self.storage.db.execute(
                "SELECT stderr FROM validator_results WHERE candidate_digest=?",
                ("f" * 64,),
            ).fetchall()
        ]
        self.assertEqual(len(stderr), len(VALIDATOR_CATEGORIES))
        self.assertTrue(all("tool roots are not configured" in text for text in stderr))

    def test_mapping_outside_task_contract_fails_before_execution(self):
        mappings = self.mappings()
        mappings["test"] = ("undeclared criterion",)
        with self.assertRaisesRegex(ValueError, "declared acceptance criteria"):
            self.runner.run(
                assignment_id=self.claim.assignment_id,
                fencing_token=self.claim.fencing_token,
                attempt_id=self.attempt_id, worktree_id=self.worktree_id,
                candidate_digest="d" * 64, pack=self.pack(),
                criterion_mappings=mappings,
            )
        self.assertEqual(self.backend.commands, [])


if __name__ == "__main__":
    unittest.main()
