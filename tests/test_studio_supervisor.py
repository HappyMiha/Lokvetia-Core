"""Work starts by itself, under a mandate, and says so in both languages."""

import tempfile
import unittest
from pathlib import Path

from agent_factory.localisation import LANGUAGES, Message
from agent_factory.storage import SQLiteStorage
from agent_factory.studio_autonomy import AutonomyJournal, decide, irreversible
from agent_factory.studio_cost import StudioCosts
from agent_factory.studio_first_run import FirstRun
from agent_factory.studio_supervisor import (
    Advanced,
    CoreMissionDriver,
    MissionState,
    STEP_GATES,
    Supervisor,
    SupervisorRefused,
    next_step,
)

FITS = Message("Влазить у памʼять.", "It fits in memory.")


class Driver:
    """A mission that moves when it is told to, and remembers who told it."""

    def __init__(self, phase="DRAFT", *, owner="Miha", cost=0.0, fails=None):
        self.phase = phase
        self.owner = owner
        self.cost = cost
        self.fails = fails
        self.calls = []

    def state(self):
        return MissionState(1, self.owner, self.phase, "RUNNING", 3)

    def plan(self, *, actor, command_id):
        self.calls.append(("plan", actor, command_id))
        if self.fails is not None:
            raise self.fails
        self.phase = "WAITING_FOR_BACKLOG_APPROVAL"
        return Advanced(self.phase, Message("Готово.", "Done."), self.cost)

    def approve(self, *, actor, command_id):
        self.calls.append(("approve", actor, command_id))
        if self.fails is not None:
            raise self.fails
        self.phase = "APPROVED"
        return Advanced(self.phase)


class Fixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.storage = SQLiteStorage(Path(folder.name) / "state.db")
        self.addCleanup(self.storage.close)
        self.supervisor = Supervisor(self.storage)
        self.mission = "game-1"

    def connect_a_source(self):
        FirstRun(self.storage).connect(
            "ollama", kind="local_model", name="qwen3", state="verified", detail=FITS)

    def mandate(self, *steps, **extra):
        return self.supervisor.grant(
            self.mission, steps=steps or ("plan",), granted_by="Miha", **extra)


class MandateTests(Fixture):
    def test_a_mandate_needs_a_name(self):
        with self.assertRaises(SupervisorRefused):
            self.supervisor.grant(self.mission, steps=("plan",), granted_by="  ")

    def test_a_mandate_needs_at_least_one_step(self):
        with self.assertRaises(SupervisorRefused):
            self.supervisor.grant(self.mission, steps=(), granted_by="Miha")

    def test_a_step_nobody_defined_is_refused(self):
        with self.assertRaises(SupervisorRefused) as caught:
            self.supervisor.grant(self.mission, steps=("ship_it",), granted_by="Miha")
        self.assertIn("ship_it", caught.exception.text("en"))

    def test_a_mandate_that_never_runs_out_is_refused(self):
        with self.assertRaises(SupervisorRefused):
            self.supervisor.grant(self.mission, steps=("plan",), granted_by="Miha", hours=0)

    def test_granting_again_supersedes_rather_than_edits(self):
        first = self.mandate("plan")
        second = self.mandate("plan", "approve_plan")
        history = self.supervisor.mandates(self.mission)
        self.assertEqual(len(history), 2)
        self.assertNotEqual(first.identifier, second.identifier)
        self.assertTrue(history[-1].revoked_at, "the superseded mandate is closed, not gone")
        self.assertEqual(self.supervisor.mandate(self.mission).steps,
                         ("plan", "approve_plan"))

    def test_what_a_person_allowed_can_never_be_rewritten(self):
        mandate = self.mandate("plan")
        with self.assertRaises(Exception) as caught:
            with self.storage.db:
                self.storage.db.execute(
                    "UPDATE studio_mandates SET steps_json='[\"approve_plan\"]' WHERE id=?",
                    (mandate.identifier,))
        self.assertIn("may only gain its revocation", str(caught.exception))

    def test_a_revocation_is_not_rewritten_either(self):
        mandate = self.mandate("plan")
        self.supervisor.revoke(self.mission, actor="Miha")
        with self.assertRaises(Exception):
            with self.storage.db:
                self.storage.db.execute(
                    "UPDATE studio_mandates SET revoked_at='2020-01-01T00:00:00+00:00' "
                    "WHERE id=?", (mandate.identifier,))

    def test_revoking_nothing_is_refused_rather_than_pretended(self):
        with self.assertRaises(SupervisorRefused):
            self.supervisor.revoke(self.mission, actor="Miha")

    def test_an_expired_mandate_is_still_readable(self):
        # Saying "it ran out" needs the record; saying "nobody ever asked" would
        # be a different and false statement.
        self.supervisor.grant(self.mission, steps=("plan",), granted_by="Miha",
                              hours=1, at="2020-01-01T00:00:00+00:00")
        mandate = self.supervisor.mandate(self.mission)
        self.assertIsNotNone(mandate)
        self.assertFalse(mandate.live())

    def test_the_record_reads_in_both_languages(self):
        mandate = self.mandate("plan")
        for language in LANGUAGES:
            self.assertTrue(mandate.record(language)["summary"].strip())
        self.assertIn("Miha", mandate.record("en")["summary"])


class RefusalTests(Fixture):
    """Every reason not to start is a sentence, never silence."""

    def test_without_a_mandate_nothing_happens_at_all(self):
        driver = Driver()
        step = self.supervisor.advance(self.mission, driver)
        self.assertEqual(step.outcome, "refused")
        self.assertEqual(driver.calls, [])
        self.assertIn("Nobody has asked", step.summary.text("en"))

    def test_an_expired_mandate_stops_the_work_and_says_when(self):
        self.connect_a_source()
        self.supervisor.grant(self.mission, steps=("plan",), granted_by="Miha",
                              hours=1, at="2020-01-01T00:00:00+00:00")
        driver = Driver()
        step = self.supervisor.advance(self.mission, driver)
        self.assertEqual(step.outcome, "waiting")
        self.assertIn("2020-01-01", step.summary.text("en"))
        self.assertEqual(driver.calls, [])

    def test_no_checked_source_means_nothing_to_start_with(self):
        self.mandate("plan")
        step = self.supervisor.advance(self.mission, Driver())
        self.assertEqual(step.outcome, "waiting")
        self.assertIn("no checked source", step.summary.text("en"))

    def test_an_unverified_source_does_not_unlock_the_studio(self):
        FirstRun(self.storage).connect(
            "sub", kind="own_subscription", name="Someone's plan", state="unverified")
        self.mandate("plan")
        self.assertEqual(self.supervisor.advance(self.mission, Driver()).outcome, "waiting")

    def test_an_open_question_holds_the_work(self):
        self.connect_a_source()
        self.mandate("plan")
        AutonomyJournal(self.storage).record(
            decide("environment_profile", question=irreversible("delete the volume")),
            mission=self.mission)
        driver = Driver()
        step = self.supervisor.advance(self.mission, driver)
        self.assertEqual(step.outcome, "waiting")
        self.assertIn("waiting on an answer", step.summary.text("en"))
        self.assertEqual(driver.calls, [])

    def test_a_step_outside_the_mandate_stays_with_a_person(self):
        self.connect_a_source()
        self.mandate("approve_plan")
        driver = Driver(phase="DRAFT")
        step = self.supervisor.advance(self.mission, driver)
        self.assertEqual(step.outcome, "refused")
        self.assertIn("plan", step.summary.text("en"))
        self.assertEqual(driver.calls, [])

    def test_a_phase_this_studio_does_not_drive_is_said_plainly(self):
        self.connect_a_source()
        self.mandate("plan", "approve_plan")
        step = self.supervisor.advance(self.mission, Driver(phase="DEVELOPMENT"))
        self.assertEqual(step.outcome, "nothing_to_do")
        self.assertIn("DEVELOPMENT", step.summary.text("en"))

    def test_every_refusal_reads_in_both_languages(self):
        self.mandate("plan")
        step = self.supervisor.advance(self.mission, Driver())
        for language in LANGUAGES:
            self.assertTrue(step.summary.text(language).strip())
        self.assertNotEqual(step.summary.text("uk"), step.summary.text("en"))


class MoneyTests(Fixture):
    def setUp(self):
        super().setUp()
        self.connect_a_source()
        self.costs = StudioCosts(self.storage)

    def test_a_step_past_the_mandate_ceiling_asks_instead_of_spending(self):
        self.mandate("plan", ceiling=5.0)
        self.costs.record(self.mission, amount=4.5, kind="reported", task_key="t1")
        driver = Driver()
        step = self.supervisor.advance(self.mission, driver, next_step_cost=2.0)
        self.assertEqual(step.outcome, "asked")
        self.assertEqual(driver.calls, [])
        self.assertIn("0.5", step.summary.text("en"))

    def test_the_question_reaches_the_feed_where_a_person_can_answer_it(self):
        self.mandate("plan", ceiling=1.0)
        self.supervisor.advance(self.mission, Driver(), next_step_cost=5.0)
        questions = AutonomyJournal(self.storage).open_questions(mission=self.mission)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["reason"], "over_budget")

    def test_a_step_inside_the_ceiling_runs(self):
        self.mandate("plan", ceiling=50.0)
        step = self.supervisor.advance(self.mission, Driver(), next_step_cost=2.0)
        self.assertEqual(step.outcome, "advanced")

    def test_the_game_limit_stops_the_studio_even_with_a_larger_ceiling(self):
        # Two separate promises: what the person allowed this game to cost, and
        # what they allowed the studio to spend unattended. The smaller wins.
        self.costs.set_limit(self.mission, amount=1.0, actor="Miha")
        self.mandate("plan", ceiling=100.0)
        driver = Driver()
        step = self.supervisor.advance(self.mission, driver, next_step_cost=9.0)
        self.assertEqual(step.outcome, "asked")
        self.assertEqual(driver.calls, [])

    def test_what_a_step_really_cost_is_recorded_against_the_game(self):
        self.mandate("plan", ceiling=50.0)
        self.supervisor.advance(self.mission, Driver(cost=1.25))
        self.assertEqual(self.costs.totals(self.mission)["spent"], 1.25)


class AdvanceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.connect_a_source()

    def test_a_draft_game_is_planned_without_anybody_pressing_anything(self):
        self.mandate("plan")
        driver = Driver(phase="DRAFT")
        step = self.supervisor.advance(self.mission, driver)
        self.assertEqual(step.outcome, "advanced")
        self.assertEqual(driver.phase, "WAITING_FOR_BACKLOG_APPROVAL")
        self.assertEqual(driver.calls[0][0], "plan")

    def test_the_work_is_signed_with_the_owners_name_and_the_mandate(self):
        self.mandate("plan")
        step = self.supervisor.advance(self.mission, Driver(owner="Miha"))
        self.assertEqual(step.actor, "Miha")
        self.assertEqual(step.granted_by, "Miha")

    def test_the_gate_is_only_answered_once_the_step_has_happened(self):
        self.mandate("plan")
        self.supervisor.advance(self.mission, Driver())
        decisions = AutonomyJournal(self.storage).decisions(mission=self.mission)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["gate"], STEP_GATES["plan"])
        self.assertTrue(decisions[0]["automatic"])

    def test_a_step_that_failed_claims_no_permission(self):
        self.mandate("plan")
        step = self.supervisor.advance(
            self.mission, Driver(fails=RuntimeError("the provider never answered")))
        self.assertEqual(step.outcome, "waiting")
        self.assertIn("never answered", step.summary.text("en"))
        self.assertEqual(AutonomyJournal(self.storage).decisions(mission=self.mission), ())

    def test_a_driver_that_refuses_is_answered_not_retried(self):
        self.mandate("approve_plan")
        driver = Driver(phase="WAITING_FOR_BACKLOG_APPROVAL",
                        fails=SupervisorRefused(Message("Ще ні.", "Not yet.")))
        step = self.supervisor.advance(self.mission, driver)
        self.assertEqual(step.outcome, "refused")
        self.assertEqual(step.summary.text("en"), "Not yet.")

    def test_the_same_answer_is_not_written_into_the_ledger_twice(self):
        # A runner asks every half minute. The history is a history of changes.
        self.mandate("approve_plan")
        for _ in range(5):
            self.supervisor.advance(self.mission, Driver(phase="DRAFT"))
        self.assertEqual(len(self.supervisor.history(self.mission)), 1)

    def test_a_changed_answer_is_written(self):
        self.mandate("approve_plan")
        self.supervisor.advance(self.mission, Driver(phase="DRAFT"))
        self.supervisor.advance(self.mission, Driver(phase="DEVELOPMENT"))
        outcomes = [step.outcome for step in self.supervisor.history(self.mission)]
        self.assertEqual(outcomes, ["nothing_to_do", "refused"])

    def test_a_run_stops_the_moment_the_mission_stops_moving(self):
        self.mandate("plan")
        driver = Driver(phase="DRAFT")
        taken = self.supervisor.run(self.mission, driver, passes=6)
        self.assertEqual([step.outcome for step in taken], ["advanced", "refused"])
        self.assertEqual(len(driver.calls), 1)

    def test_a_step_that_reports_success_without_moving_is_not_repeated(self):
        # A driver can be wrong. Progress is the mission moving, not the call
        # returning, so a runner cannot be talked into a circle.
        self.mandate("plan")

        class Restless(Driver):
            def plan(self, *, actor, command_id):
                self.calls.append(("plan", actor, command_id))
                return Advanced("DRAFT")

        driver = Restless()
        taken = self.supervisor.run(self.mission, driver, passes=5)
        self.assertEqual([step.outcome for step in taken], ["waiting"])
        self.assertEqual(len(driver.calls), 1)
        self.assertIn("stayed where it was", taken[0].summary.text("en"))


class RestartTests(Fixture):
    def setUp(self):
        super().setUp()
        self.connect_a_source()
        self.mandate("plan")

    def interrupt(self):
        """Leave a step exactly as a killed process would leave it."""
        return self.supervisor._open(self.mission, "plan", mandate=None, actor="Miha",
                                     command_id="c1", at="2026-09-13T07:00:00+00:00")

    def test_an_interrupted_step_is_never_repeated(self):
        self.interrupt()
        driver = Driver()
        step = self.supervisor.advance(self.mission, driver)
        self.assertEqual(step.outcome, "unknown")
        self.assertEqual(driver.calls, [], "paid work is not re-run to make sure")

    def test_the_person_is_told_it_may_already_have_been_paid_for(self):
        self.interrupt()
        step = self.supervisor.reconcile(self.mission)[0]
        self.assertIn("may have been paid for", step.summary.text("en"))
        for language in LANGUAGES:
            self.assertTrue(step.summary.text(language).strip())

    def test_reconciling_twice_finds_nothing_the_second_time(self):
        self.interrupt()
        self.assertEqual(len(self.supervisor.reconcile(self.mission)), 1)
        self.assertEqual(self.supervisor.reconcile(self.mission), ())

    def test_a_finished_step_is_never_rewritten(self):
        self.supervisor.advance(self.mission, Driver())
        finished = self.supervisor.history(self.mission)[0]
        with self.assertRaises(Exception) as caught:
            with self.storage.db:
                self.storage.db.execute(
                    "UPDATE studio_supervisor_steps SET outcome='waiting' WHERE id=?",
                    (finished.identifier,))
        self.assertIn("finished step is not rewritten", str(caught.exception))


class ReportTests(Fixture):
    def test_a_game_nobody_delegated_says_so(self):
        report = self.supervisor.report(self.mission, language="en")
        self.assertFalse(report["running_on_its_own"])
        self.assertIsNone(report["mandate"])
        self.assertIn("Nobody has asked", report["summary"])

    def test_the_report_names_what_the_studio_may_do(self):
        self.connect_a_source()
        self.mandate("plan")
        self.supervisor.advance(self.mission, Driver())
        report = self.supervisor.report(self.mission, language="uk")
        self.assertTrue(report["running_on_its_own"])
        self.assertEqual(report["may"], ["plan"])
        self.assertEqual(report["steps"][0]["outcome"], "advanced")
        self.assertEqual(report["needs_person"], [])

    def test_what_needs_a_person_is_separated_from_the_rest(self):
        self.connect_a_source()
        self.mandate("plan", ceiling=0.5)
        self.supervisor.advance(self.mission, Driver(), next_step_cost=9.0)
        report = self.supervisor.report(self.mission, language="en")
        self.assertEqual(len(report["needs_person"]), 1)
        self.assertEqual(report["needs_person"][0]["outcome"], "asked")


class PhaseTests(unittest.TestCase):
    def test_the_phases_that_still_need_a_plan(self):
        for phase in ("DRAFT", "SPECIFICATION_ANALYSIS", "BACKLOG_GENERATION"):
            self.assertEqual(next_step(phase), "plan")

    def test_the_phase_that_needs_approving(self):
        self.assertEqual(next_step("WAITING_FOR_BACKLOG_APPROVAL"), "approve_plan")

    def test_a_phase_past_approval_is_not_this_supervisors_business(self):
        for phase in ("APPROVED", "DEVELOPMENT", "COMPLETED", ""):
            self.assertEqual(next_step(phase), "")


class CoreDriverTests(Fixture):
    """What the real driver refuses to pretend."""

    def test_approving_a_plan_is_declined_with_its_reason_in_both_languages(self):
        driver = CoreMissionDriver(self.storage, 1)
        with self.assertRaises(SupervisorRefused) as caught:
            driver.approve(actor="Miha", command_id="c1")
        for language in LANGUAGES:
            self.assertTrue(caught.exception.text(language).strip())
        self.assertIn("executor that does not exist yet", caught.exception.text("en"))

    def test_the_refusal_arrives_through_the_supervisor_as_a_refusal(self):
        self.connect_a_source()
        self.mandate("approve_plan")

        class AtApproval(CoreMissionDriver):
            def state(inner):
                return MissionState(1, "Miha", "WAITING_FOR_BACKLOG_APPROVAL",
                                    "RUNNING", 2)

        step = self.supervisor.advance(self.mission, AtApproval(self.storage, 1))
        self.assertEqual(step.outcome, "refused")
        self.assertIn("stays with a person", step.summary.text("en"))


if __name__ == "__main__":
    unittest.main()


class RealMissionTests(unittest.TestCase):
    """The whole point: a real mission moves, and nobody pressed anything.

    This is the wire that was missing. It uses the planning pipeline's own golden
    fixtures rather than a stand-in, so what is proved here is that the studio
    drives the real services, not a protocol shaped like them.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util

        path = Path(__file__).with_name("test_autonomous_planning_pipeline.py")
        spec = importlib.util.spec_from_file_location("planning_fixtures", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.fixtures = module

    def setUp(self):
        from agent_factory.autonomous_mission import AutonomousMissionConfiguration
        from agent_factory.mission_intake import AutonomousMissionIntakeService
        from agent_factory.models import (
            ExecutionLocation,
            ProviderCapabilities,
        )

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name)
        self.storage = SQLiteStorage(self.workspace / "state.db")
        self.addCleanup(self.storage.close)
        self.capabilities = {
            "local": ProviderCapabilities(
                execution_location=ExecutionLocation.LOCAL,
                location_declared=True,
                text_generation=True,
                structured_output=True,
            )
        }
        created = AutonomousMissionIntakeService(self.storage).create_from_text(
            name="Перша гра",
            mission_owner="Miha",
            specification=(
                "# Product\n\nBuild a safe local health endpoint with deterministic "
                "validation and local persistence."
            ),
            actor="Miha",
            command_id="local-start-first-game",
            mission_key="AFM-FIRST-GAME",
            configuration=AutonomousMissionConfiguration(
                repository_path=str(self.workspace),
                default_model="local-planner",
                local_provider_ids=("local",),
            ),
            # The golden planning fixtures cite this source by name, exactly as a
            # real planning role has to cite whatever the intake was called.
            source_name="specification.md",
        )
        self.mission = created.mission
        self.supervisor = Supervisor(self.storage)
        FirstRun(self.storage).connect(
            "ollama", kind="local_model", name="qwen3", state="verified", detail=FITS)

    def driver(self, **extra):
        return CoreMissionDriver(
            self.storage, self.mission.id,
            invoker=self.fixtures.GoldenPlanningInvoker(**extra),
            capabilities=self.capabilities)

    def test_a_draft_mission_reaches_a_plan_with_nobody_pressing_anything(self):
        from agent_factory.autonomous_mission import AutonomousMissionService

        self.assertEqual(str(self.mission.phase), "DRAFT")
        self.supervisor.grant("AFM-FIRST-GAME", steps=("plan",), granted_by="Miha",
                              ceiling=25.0)
        step = self.supervisor.advance("AFM-FIRST-GAME", self.driver())
        self.assertEqual(step.outcome, "advanced", step.summary.text("en"))
        after = AutonomousMissionService(self.storage).get(self.mission.id)
        self.assertEqual(str(after.phase), "WAITING_FOR_BACKLOG_APPROVAL")

    def test_the_plan_that_appears_is_a_real_backlog_revision(self):
        self.supervisor.grant("AFM-FIRST-GAME", steps=("plan",), granted_by="Miha")
        self.supervisor.advance("AFM-FIRST-GAME", self.driver())
        rows = self.storage.db.execute(
            "SELECT COUNT(*) FROM autonomous_backlog_revisions WHERE mission_id=?",
            (self.mission.id,)).fetchone()[0]
        self.assertGreaterEqual(rows, 1)

    def test_the_owner_is_the_actor_on_every_record_the_studio_wrote(self):
        self.supervisor.grant("AFM-FIRST-GAME", steps=("plan",), granted_by="Miha")
        self.supervisor.advance("AFM-FIRST-GAME", self.driver())
        actors = {row[0] for row in self.storage.db.execute(
            "SELECT DISTINCT actor FROM autonomous_mission_state_versions "
            "WHERE mission_id=?", (self.mission.id,))}
        self.assertEqual(actors, {"Miha"})

    def test_a_planning_role_that_never_produces_a_valid_answer_is_reported(self):
        self.supervisor.grant("AFM-FIRST-GAME", steps=("plan",), granted_by="Miha")
        step = self.supervisor.advance(
            "AFM-FIRST-GAME", self.driver(always_invalid=True))
        self.assertEqual(step.outcome, "waiting")
        self.assertIn("failed", step.summary.text("en"))

    def test_running_the_supervisor_twice_does_not_plan_twice(self):
        self.supervisor.grant("AFM-FIRST-GAME", steps=("plan",), granted_by="Miha")
        first = self.driver()
        self.supervisor.advance("AFM-FIRST-GAME", first)
        second = self.driver()
        step = self.supervisor.advance("AFM-FIRST-GAME", second)
        self.assertNotEqual(step.outcome, "advanced")
