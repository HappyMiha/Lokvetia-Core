"""Progress reporting keeps merged code and accepted work separate."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_factory.progress import (
    MAX_SUBJECT,
    CommitRecord,
    ProgressError,
    build_project,
    commit_index,
    read_commits,
    read_manifest,
    read_manifest_file,
    report,
    revision,
)


def item(stable_id: str, **overrides) -> dict:
    document = {
        "stable_id": stable_id,
        "kind": "task",
        "title": f"Title {stable_id}",
        "description": f"Description {stable_id}",
        "acceptance_criteria": ["An operator can verify the result."],
        "dependencies": [],
        "labels": [],
    }
    document.update(overrides)
    return document


EVIDENCE = {"kind": "test", "reference": "tests/test_example.py",
            "recorded_by": "Reviewer", "note": "covers the stated criterion"}


def accepted_item(stable_id: str, **overrides) -> dict:
    return item(stable_id, labels=["status:accepted"], evidence=[EVIDENCE], **overrides)


def manifest(*items: dict, name: str = "plan") -> tuple[str, dict, str]:
    document = {"schema_version": 1, "source": {"name": name}, "items": list(items)}
    return (f"examples/{name}.json", document, "a" * 64)


def project(*manifests, commits=(), **overrides):
    arguments = {
        "project_id": "core",
        "name": "Lokvetia Core",
        "repository": "HappyMiha/Lokvetia-Core",
        "revision": "f" * 40,
        "manifests": list(manifests),
        "commits": commits,
    }
    arguments.update(overrides)
    return build_project(**arguments)


def tasks_by_id(result) -> dict:
    return {task.stable_id: task for task in result.tasks}


class CommitIndexTests(unittest.TestCase):
    def test_a_longer_identifier_is_not_credited_to_its_prefix(self):
        commits = [CommitRecord("a" * 40, "Implement AF-CLD-0012 storage")]
        index = commit_index(commits, ["AF-CLD-001", "AF-CLD-0012"])
        self.assertNotIn("AF-CLD-001", index)
        self.assertEqual(len(index["AF-CLD-0012"]), 1)

    def test_identifiers_are_matched_on_token_boundaries(self):
        commits = [
            CommitRecord("b" * 40, "AF-001: add leases"),
            CommitRecord("c" * 40, "(AF-001) follow-up"),
            CommitRecord("d" * 40, "xAF-001y should not match"),
        ]
        index = commit_index(commits, ["AF-001"])
        self.assertEqual(len(index["AF-001"]), 2)

    def test_one_commit_is_counted_once_for_a_repeated_identifier(self):
        commits = [CommitRecord("e" * 40, "AF-001 and AF-001 again")]
        self.assertEqual(len(commit_index(commits, ["AF-001"])["AF-001"]), 1)

    def test_an_empty_identifier_set_returns_no_index(self):
        self.assertEqual(commit_index([CommitRecord("f" * 40, "AF-001")], []), {})

    def test_a_commit_identifier_must_be_hexadecimal(self):
        with self.assertRaises(ProgressError):
            CommitRecord("not-a-sha", "AF-001")


class TrackSeparationTests(unittest.TestCase):
    def test_a_merge_never_declares_acceptance(self):
        result = project(
            manifest(item("AF-001")),
            commits=[CommitRecord("a" * 40, "AF-001 implement the thing")],
        )
        task = tasks_by_id(result)["AF-001"]
        self.assertTrue(task.merged)
        self.assertFalse(task.accepted)
        self.assertEqual(task.state, "merged")

    def test_acceptance_comes_only_from_the_manifest(self):
        result = project(manifest(accepted_item("AF-001")))
        task = tasks_by_id(result)["AF-001"]
        self.assertTrue(task.accepted)
        self.assertFalse(task.merged)
        self.assertEqual(task.state, "accepted")

    def test_recorded_evidence_is_its_own_track(self):
        result = project(manifest(
            item("AF-001", evidence=[EVIDENCE]),
            item("AF-002"),
        ))
        tasks = tasks_by_id(result)
        self.assertTrue(tasks["AF-001"].declared)
        self.assertFalse(tasks["AF-001"].accepted)
        self.assertFalse(tasks["AF-002"].declared)
        self.assertEqual(result.to_dict()["declared"], 1)

    def test_a_manifest_cannot_declare_acceptance_without_evidence(self):
        result = project(manifest(item("AF-001", labels=["status:accepted"])))
        self.assertEqual(result.to_dict()["tasks"], 0)
        self.assertTrue(any("acceptance" in warning for warning in result.warnings))

    def test_an_unreferenced_task_stays_outstanding(self):
        result = project(
            manifest(item("AF-001"), item("AF-002")),
            commits=[CommitRecord("a" * 40, "AF-001 only")],
        )
        self.assertEqual(tasks_by_id(result)["AF-002"].state, "todo")
        self.assertEqual(result.to_dict()["remaining"], 2)

    def test_an_in_progress_label_is_reported_without_claiming_delivery(self):
        result = project(manifest(item("AF-001", labels=["status:in_progress"])))
        task = tasks_by_id(result)["AF-001"]
        self.assertEqual(task.state, "in_progress")
        self.assertFalse(task.merged)
        self.assertFalse(task.accepted)


class DependencyTests(unittest.TestCase):
    def test_an_unmet_dependency_blocks_a_task(self):
        result = project(manifest(item("AF-001"), item("AF-002", dependencies=["AF-001"])))
        task = tasks_by_id(result)["AF-002"]
        self.assertEqual(task.blocked_by, ("AF-001",))
        self.assertEqual(task.state, "blocked")

    def test_a_commit_reference_cannot_release_the_next_task(self):
        result = project(
            manifest(item("AF-001"), item("AF-002", dependencies=["AF-001"])),
            commits=[CommitRecord("a" * 40, "AF-001 done")],
        )
        task = tasks_by_id(result)["AF-002"]
        self.assertEqual(task.blocked_by, ("AF-001",))
        self.assertEqual(task.state, "blocked")
        self.assertNotIn("AF-002", result.to_dict()["ready"])

    def test_a_dependency_on_an_unknown_identifier_is_refused_by_the_loader(self):
        result = project(manifest(item("AF-002", dependencies=["AF-999"])))
        self.assertEqual(result.to_dict()["tasks"], 0)
        self.assertTrue(any("AF-999" in warning for warning in result.warnings))


class GroupingTests(unittest.TestCase):
    def test_a_declared_milestone_wins_over_the_parent(self):
        result = project(
            manifest(
                item("AF-E1", kind="epic", title="Epic", description="Epic"),
                item("AF-001", parent_id="AF-E1", labels=["milestone:m2"]),
            )
        )
        self.assertEqual([block.title for block in result.blocks], ["M2"])
        self.assertEqual(result.blocks[0].kind, "milestone")

    def test_a_block_is_the_topmost_ancestor_not_the_immediate_parent(self):
        result = project(
            manifest(
                item("AF-E1", kind="epic", title="Durable core", description="Epic"),
                item("AF-F1", kind="story", title="Feature", description="Feature", parent_id="AF-E1"),
                item("AF-001", parent_id="AF-F1"),
            )
        )
        self.assertEqual([block.title for block in result.blocks], ["Durable core"])

    def test_a_release_label_groups_a_task_with_no_parent(self):
        result = project(manifest(item("AF-001", labels=["release:r1"])))
        self.assertEqual(result.blocks[0].kind, "release")
        self.assertEqual(result.blocks[0].title, "R1")

    def test_two_manifests_never_merge_two_different_milestones(self):
        result = project(
            manifest(item("AF-001", labels=["milestone:m0"]), name="alpha"),
            manifest(item("AF-GC-001", labels=["milestone:m0"]), name="beta"),
        )
        self.assertEqual(len(result.blocks), 2)
        self.assertEqual({block.track for block in result.blocks}, {"alpha", "beta"})

    def test_a_task_with_no_grouping_signal_is_reported_not_dropped(self):
        result = project(manifest(item("AF-001")))
        self.assertEqual(result.blocks[0].kind, "unassigned")
        self.assertEqual(result.to_dict()["tasks"], 1)


class WeightTests(unittest.TestCase):
    def test_size_labels_weight_the_percentage(self):
        result = project(
            manifest(
                item("AF-001", labels=["size:s"]),
                item("AF-002", labels=["size:l"]),
            ),
            commits=[CommitRecord("a" * 40, "AF-001 small change")],
        )
        document = result.to_dict()
        self.assertEqual(document["weight"], 12)
        self.assertEqual(document["percent"]["merged"], 16.7)
        self.assertEqual(document["percent"]["merged_by_count"], 50.0)

    def test_an_unsized_task_uses_the_documented_default(self):
        result = project(manifest(item("AF-001")))
        self.assertEqual(tasks_by_id(result)["AF-001"].weight, 5)

    def test_an_empty_project_reports_zero_rather_than_failing(self):
        result = build_project(
            project_id="empty",
            name="Empty",
            repository="HappyMiha/Lokiravia",
            revision="f" * 40,
            manifests=[],
        )
        document = result.to_dict()
        self.assertEqual(document["percent"]["merged"], 0.0)
        self.assertTrue(document["warnings"])


class ReportTests(unittest.TestCase):
    def test_totals_add_up_across_projects(self):
        core = project(manifest(item("AF-001")), commits=[CommitRecord("a" * 40, "AF-001")])
        cloud = build_project(
            project_id="lokiravia",
            name="Lokiravia",
            repository="HappyMiha/Lokiravia",
            revision="e" * 40,
            manifests=[manifest(item("AF-CLD-001"), name="cloud")],
        )
        document = report([core, cloud])
        self.assertEqual(document["totals"]["tasks"], 2)
        self.assertEqual(document["totals"]["merged"], 1)
        self.assertEqual(document["totals"]["accepted"], 0)
        self.assertEqual(document["totals"]["remaining"], 2)
        self.assertIn("None of them is inferred from another", document["evidence_note"])

    def test_a_malformed_manifest_is_reported_and_the_rest_still_loads(self):
        broken = ("examples/broken.json", {"schema_version": 9, "items": []}, "b" * 64)
        result = project(manifest(item("AF-001")), broken)
        self.assertEqual(result.to_dict()["tasks"], 1)
        self.assertTrue(any("broken.json" in warning for warning in result.warnings))

    def test_the_published_document_is_json_serialisable(self):
        document = report([project(manifest(item("AF-001")))])
        self.assertIsInstance(json.dumps(document), str)


class RepositoryReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.repo / "examples").mkdir()
        self.manifest_path = self.repo / "examples" / "plan.json"
        self.manifest_path.write_text(
            json.dumps(manifest(item("AF-001"))[1]), encoding="utf-8"
        )
        self.git("add", "-A")
        self.git("commit", "-m", "AF-001 add the plan")

    def git(self, *arguments):
        subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=True,
            capture_output=True,
        )

    def test_commits_are_read_with_their_identifier_and_subject(self):
        commits = read_commits(self.repo)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].subject, "AF-001 add the plan")
        self.assertRegex(commits[0].sha, "^[0-9a-f]{40}$")

    def test_a_long_subject_is_truncated_before_publication(self):
        self.git("commit", "--allow-empty", "-m", "AF-001 " + "x" * 500)
        self.assertTrue(
            all(len(commit.subject) <= MAX_SUBJECT for commit in read_commits(self.repo))
        )

    def test_the_commit_scan_honours_its_limit(self):
        self.git("commit", "--allow-empty", "-m", "AF-002 second")
        self.assertEqual(len(read_commits(self.repo, limit=1)), 1)

    def test_a_manifest_is_readable_from_a_ref(self):
        document, digest = read_manifest(self.repo, "HEAD", "examples/plan.json")
        self.assertEqual(document["items"][0]["stable_id"], "AF-001")
        self.assertRegex(digest, "^[0-9a-f]{64}$")

    def test_a_missing_manifest_reports_a_progress_error(self):
        with self.assertRaises(ProgressError):
            read_manifest(self.repo, "HEAD", "examples/absent.json")

    def test_invalid_manifest_bytes_report_a_progress_error(self):
        self.manifest_path.write_bytes(b"{not json")
        with self.assertRaises(ProgressError):
            read_manifest_file(self.manifest_path)

    def test_the_revision_is_a_resolved_commit(self):
        self.assertRegex(revision(self.repo), "^[0-9a-f]{40}$")

    def test_an_unknown_ref_reports_a_progress_error(self):
        with self.assertRaises(ProgressError):
            revision(self.repo, "refs/heads/absent")

    def test_a_bare_repository_is_supported(self):
        bare = Path(self.directory.name) / "bare.git"
        subprocess.run(
            ["git", "clone", "--bare", str(self.repo), str(bare)],
            check=True,
            capture_output=True,
        )
        self.assertEqual(len(read_commits(bare, "refs/heads/main")), 1)
        document, _ = read_manifest(bare, "refs/heads/main", "examples/plan.json")
        self.assertEqual(document["items"][0]["stable_id"], "AF-001")


if __name__ == "__main__":
    unittest.main()


class PortfolioReportingTests(unittest.TestCase):
    def test_core_default_covers_all_198_executable_cards(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("progress_controller_test", root / "scripts/autodeploy.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        paths = controller.DEFAULT_PROGRESS["projects"][0]["manifests"]
        manifests = [(p, *read_manifest_file(root / p)) for p in paths]
        result = project(*manifests)
        self.assertEqual(result.warnings, ())
        # The count is pinned so a manifest cannot quietly lose or duplicate a
        # card. It moved from 183 to 198 when the AI-studio epics were planned.
        self.assertEqual(len(result.tasks), 198)
        self.assertEqual(len({t.stable_id for t in result.tasks}), 198)

    def test_cross_product_dependency_requires_acceptance(self):
        design = {"portfolio_schema_version": 1, "artifact_kind": "design_backlog",
                  "not_runtime_import": True, "items": [{
            "stable_id": "AF-LW-001", "title": "World task", "outcome": "A result",
            "phase": "W0", "status": "proposed", "acceptance_criteria": ["Verified"],
            "dependencies": ["core:AF-001"]}]}
        world = project(("docs/evolution/backlog.json", design, "a" * 64),
                        project_id="cloud", repository="HappyMiha/Lokiravia")
        for accepted in (False, True):
            prerequisite = accepted_item("AF-001") if accepted else item("AF-001")
            core = project(manifest(prerequisite), commits=[CommitRecord("a" * 40, "AF-001 planned")])
            data = report([core, world])["projects"][1]
            self.assertEqual(data["ready_count"], int(accepted))
            self.assertEqual(data["blocks"][0]["items"][0]["blocked_by"],
                             [] if accepted else ["core:AF-001"])

    def test_an_accepted_task_is_not_counted_as_remaining_without_a_commit(self):
        self.assertEqual(project(manifest(accepted_item("AF-001"))).to_dict()["remaining"], 0)

    def test_cloud_qualified_dependency_uses_the_canonical_namespace(self):
        design = {"portfolio_schema_version": 1, "artifact_kind": "design_backlog",
                  "not_runtime_import": True, "items": [
            {"stable_id": "AF-LW-001", "title": "First", "outcome": "Result",
             "phase": "W0", "status": "accepted", "evidence": [EVIDENCE],
             "acceptance_criteria": ["Verified"], "dependencies": []},
            {"stable_id": "AF-LW-002", "title": "Second", "outcome": "Result",
             "phase": "W0", "status": "proposed", "acceptance_criteria": ["Verified"],
             "dependencies": ["cloud:AF-LW-001"]}]}
        world = project(("docs/evolution/backlog.json", design, "a" * 64),
                        project_id="cloud", repository="HappyMiha/Lokiravia")
        self.assertEqual(world.warnings, ())
        self.assertEqual(report([world])["projects"][0]["ready"], ["AF-LW-002"])
        design["items"][1]["status"] = "blocked"
        world = project(("docs/evolution/backlog.json", design, "a" * 64),
                        project_id="cloud", repository="HappyMiha/Lokiravia")
        document = report([world])["projects"][0]
        self.assertEqual(document["ready"], [])
        self.assertEqual(document["blocks"][0]["items"][1]["state"], "blocked")
