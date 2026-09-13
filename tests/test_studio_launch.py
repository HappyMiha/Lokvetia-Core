"""Owner-bound one-click intake, replay, qualification and queue controls."""
from contextlib import closing
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient

from agent_factory.storage import SQLiteStorage
from agent_factory.studio_first_run import FirstRun
from agent_factory.studio_start import checked_local_source, create_local_game
from agent_factory.studio_supervisor import Supervisor, Mandate
from agent_factory.studio_local_worker import setup
from agent_factory.localisation import Message


class Queue:
    def __init__(self): self.submitted = []
    def submit(self, mission): self.submitted.append(mission)
    def status(self, mission): return "queued"


class StudioLaunchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / "state.db"
        self.queue = Queue()

    def create(self, actor="owner", key="same-click", idea="A Godot 2D coin collecting game"):
        with patch("agent_factory.studio_start.checked_local_source",
                   return_value=SimpleNamespace(name="qwen2.5-coder:7b")):
            return create_local_game(self.database, self.root, actor=actor,
                                     command_id=key, title="Coins", idea=idea, runner=self.queue)

    def test_creating_grants_only_local_planning_with_no_paid_budget(self):
        result = self.create()
        with closing(SQLiteStorage(self.database)) as storage:
            mandate = Supervisor(storage).mandate(result["mission_key"])
            self.assertEqual((mandate.granted_by, mandate.steps, mandate.ceiling), ("owner", ("plan",), 0))
        self.assertEqual(self.queue.submitted, [result["mission_id"]])

    def test_another_owner_using_the_same_command_gets_their_own_mission(self):
        first, second = self.create(), self.create(actor="other")
        self.assertNotEqual(first["mission_id"], second["mission_id"])

    def test_replay_preserves_the_mission_and_never_renews_revoked_authority(self):
        first = self.create()
        with closing(SQLiteStorage(self.database)) as storage:
            supervisor = Supervisor(storage)
            supervisor._write(first["mission_key"], "plan", "waiting", Message("Очікування", "Waiting"))
            supervisor.revoke(first["mission_key"], actor="owner")
        second = self.create()
        self.assertEqual(first["mission_id"], second["mission_id"])
        self.assertEqual(len(self.queue.submitted), 1)
        with closing(SQLiteStorage(self.database)) as storage:
            self.assertIsNone(Supervisor(storage).mandate(first["mission_key"]))

    def test_changed_idea_cannot_reuse_a_command(self):
        self.create()
        with self.assertRaises(ValueError):
            self.create(idea="A different game")
        self.assertEqual(len(self.queue.submitted), 1)

    def test_replay_after_cancelling_a_queued_game_cannot_requeue_it(self):
        first=self.create()
        with closing(SQLiteStorage(self.database)) as storage:
            Supervisor(storage).revoke(first['mission_key'],actor='owner')
        replay=self.create()
        self.assertEqual(first['mission_id'],replay['mission_id'])
        self.assertEqual(len(self.queue.submitted),1)

    def test_manual_verified_flag_cannot_replace_live_qualification(self):
        with closing(SQLiteStorage(self.database)) as storage:
            FirstRun(storage).connect("ollama", kind="local_model", name="qwen2.5-coder:7b",
                                      state="verified", detail=Message("Так", "Yes"), machine_key="pc")
            with self.assertRaises(ValueError), patch("agent_factory.studio_start.model_inventory") as probe:
                checked_local_source(storage, self.root)
            probe.assert_not_called()

    def test_hardware_fit_does_not_start_without_inference(self):
        with closing(SQLiteStorage(self.database)) as storage, patch(
                "agent_factory.studio_local_worker.GodotAdapter") as engine:
            engine.return_value.health.return_value.healthy = True
            source = setup(storage, self.root, actor="owner",
                           collector=lambda _: {"gpus": [{"dedicated_total_bytes": 8 * 1024**3}]})
            self.assertFalse(source["can_start"])
            self.assertEqual(source["source"]["state"], "unverified")

    def test_model_failure_revokes_previous_readiness(self):
        with closing(SQLiteStorage(self.database)) as storage, patch(
                "agent_factory.studio_local_worker.GodotAdapter"):
            def failed(_): raise ValueError("failed")
            with self.assertRaises(ValueError):
                setup(storage, self.root, actor="owner", run_live=True,
                      collector=lambda _: {"gpus": [{"dedicated_total_bytes": 8 * 1024**3}]}, qualifier=failed)
            self.assertFalse(FirstRun(storage).readiness().can_start)

    def test_malformed_and_not_yet_valid_mandates_fail_closed(self):
        for start, end, now in [("bad", "bad", "bad"),
                                ("2026-09-13T00:00:00+00:00", "2026-09-13T01:00:00+00:00", "2026-09-13T01:00:00+00:00"),
                                ("2026-09-13T00:00:00+00:00", "2026-09-14T00:00:00+00:00", "2026-09-12T00:00:00+00:00")]:
            self.assertFalse(Mandate("m", "owner", ("plan",), start, end).live(at=now))

    def test_nonfinite_money_cannot_remove_a_ceiling(self):
        with closing(SQLiteStorage(self.database)) as storage:
            for value in (float("nan"), float("inf"), -1):
                with self.assertRaises(ValueError):
                    Supervisor(storage).grant("m", steps=("plan",), granted_by="owner", ceiling=value)

    def test_http_uses_session_owner_and_requires_explicit_start(self):
        from agent_factory.web import create_app
        with TestClient(create_app(self.root, self.database), base_url="http://127.0.0.1") as client:
            command = {"command_id": str(uuid4()), "title": "Coins", "idea": "Collect coins", "confirmed": True}
            with patch("agent_factory.studio_launch_web.create_local_game",
                       return_value={"mission_id": 1}) as launch:
                self.assertEqual(client.post("/api/studio/create", json=command).status_code, 400)
                result = client.post("/api/studio/create", json=command,
                                     headers={"X-Agent-Factory-Confirm": "true"})
                self.assertEqual(result.status_code, 202, result.text)
                self.assertEqual(launch.call_args.kwargs["actor"], client.get("/auth/session").json()["actor"])
                command["actor"] = "someone-else"
                self.assertEqual(client.post("/api/studio/create", json=command,
                                 headers={"X-Agent-Factory-Confirm": "true"}).status_code, 422)

    def test_http_returns_clear_blocker_when_no_local_model_is_qualified(self):
        from agent_factory.web import create_app
        with TestClient(create_app(self.root, self.database), base_url="http://127.0.0.1") as client:
            result = client.post("/api/studio/create", json={"command_id": str(uuid4()),
                "title": "Coins", "idea": "Collect coins", "confirmed": True},
                headers={"X-Agent-Factory-Confirm": "true"})
            self.assertEqual(result.status_code, 409)
            self.assertEqual(client.get("/api/games/missions").json()["items"], [])

    def test_two_local_apps_share_one_inference_turn(self):
        from agent_factory.studio_runner import StudioRunner
        first, second = self.create(key="one"), self.create(key="two")
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        def run(_, driver):
            if driver == first["mission_id"]:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release first inference")
            else:
                second_entered.set()
            return ()
        runners = [StudioRunner(self.database, self.root, source_check=lambda *_: None,
                                driver_factory=lambda _, ident, **kw: ident) for _ in range(2)]
        try:
            with patch("agent_factory.studio_runner.Supervisor.run", side_effect=run):
                runners[0].submit(first["mission_id"])
                self.assertTrue(entered.wait(5))
                runners[1].submit(second["mission_id"])
                self.assertFalse(second_entered.wait(0.5))
                self.assertEqual(runners[1].status(second["mission_id"]), "queued")
                release.set()
                for runner, result in zip(runners, (first, second)):
                    runner.jobs[result["mission_id"]].result(timeout=5)
                self.assertTrue(second_entered.is_set())
        finally:
            release.set()
            for runner in runners:
                runner.close()

    def test_source_changed_while_queued_records_a_blocker_without_inference(self):
        from agent_factory.studio_runner import StudioRunner
        result = self.create()
        def changed(*_): raise ValueError("changed")
        runner = StudioRunner(self.database, self.root, source_check=changed)
        try:
            with patch("agent_factory.studio_runner.CoreMissionDriver.plan") as plan:
                runner.submit(result["mission_id"])
                runner.jobs[result["mission_id"]].result(timeout=5)
                plan.assert_not_called()
            with closing(SQLiteStorage(self.database)) as storage:
                history = Supervisor(storage).history(result["mission_key"])
                self.assertEqual(history[-1].outcome, "waiting")
                self.assertIn("configuration changed", history[-1].summary.en)
        finally:
            runner.close()

    def test_cancelling_a_model_wait_does_not_wait_for_the_other_game(self):
        from agent_factory.studio_runner import StudioRunner
        from agent_factory.local_games import local_games_lock
        import time
        result=self.create()
        runner=StudioRunner(self.database,self.root,source_check=lambda *_:None)
        try:
            with local_games_lock(str(self.database)+'.studio-inference'),patch('agent_factory.studio_runner.CoreMissionDriver.plan') as plan:
                runner.submit(result['mission_id'])
                future=runner.jobs[result['mission_id']]
                deadline=time.monotonic()+2
                while not future.running() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(future.running())
                runner.cancel(result['mission_id'])
                future.result(timeout=3)
                plan.assert_not_called()
        finally:runner.close()

    def test_qualification_from_another_pc_cannot_enable_this_worker(self):
        with closing(SQLiteStorage(self.database)) as storage:
            FirstRun(storage).connect("ollama", kind="local_model", name="qwen2.5-coder:7b",
                                      state="verified", detail=Message("Так", "Yes"),
                                      machine_key="local-another-pc")
            with patch("agent_factory.studio_start.provider_profile"), patch(
                    "agent_factory.studio_start.model_inventory") as probe:
                with self.assertRaises(ValueError):
                    checked_local_source(storage, self.root)
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
