"""The studio screen, judged on what a person actually sees.

A mission with a plan, a paused cycle, a spending limit and a machine is seeded
into a real database, the page is opened in a real browser, and the claims on
the screen are compared with what the data supports.
"""

from __future__ import annotations

import tempfile
import json
import threading
import time
import unittest
from pathlib import Path

from agent_factory.accessibility import COLLECTOR_SCRIPT, audit
from agent_factory.localisation import Message
from agent_factory.storage import SQLiteStorage
from agent_factory.studio_cost import StudioCosts
from agent_factory.studio_cycles import StudioCycles
from agent_factory.studio_paid_tools import PaidTools
from agent_factory.studio_roster import StudioRoster
from agent_factory.studio_workers import StudioMachines
from agent_factory.web import create_app

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:  # pragma: no cover - the suite skips without a browser
    sync_playwright = None

MISSION = "cat-coins"


def seed(database: Path) -> None:
    storage = SQLiteStorage(database)
    try:
        db = storage.db
        with db:
            db.execute("INSERT INTO projects(id,name,description) VALUES(1,'Кіт і монети','')")
            db.execute(
                """INSERT INTO autonomous_missions
                   (id,identity,mission_key,project_id,name,mission_owner,phase,
                    disposition,configuration_json,configuration_digest)
                   VALUES(1,'mission-1',?,1,'Кіт і монети','miha','DEVELOPMENT',
                          'RUNNING','{}',?)""",
                (MISSION, "c" * 64))
            db.execute(
                """INSERT INTO autonomous_backlog_revisions
                   (id,identity,mission_id,revision_number,origin,created_by,rationale,
                    schema_version,source_sha256,snapshot_json,revision_digest,item_count)
                   VALUES(1,'rev-1',1,1,'HUMAN','miha','first',2,?,'{}',?,3)""",
                ("a" * 64, "b" * 64))
            items = (
                (1, "AF-M-E1", "База гри", "epic", 0, None, None),
                (2, "AF-M-001", "Кіт стрибає", "task", 1, "AF-M-E1", "DONE"),
                (3, "AF-M-002", "Монети рахуються", "task", 1, "AF-M-E1", "RUNNING"),
                (4, "AF-M-003", "Другий рівень", "task", 1, "AF-M-E1", "BLOCKED"),
            )
            for item_id, stable_id, title, kind, executable, parent, status in items:
                db.execute(
                    """INSERT INTO autonomous_backlog_items
                       (id,identity,revision_id,stable_id,kind,executable,title,description,
                        parent_stable_id,dependencies_json,priority,acceptance_criteria_json,
                        validation_method_json,required_components_json,
                        required_infrastructure_json,expected_artifacts_json,
                        definition_of_done_json,assigned_role,source_references_json,
                        review_notes_json,labels_json,item_digest)
                       VALUES(?,?,1,?,?,?,?,'',?,'[]','P1','[]','[]','[]','[]','[]','[]',
                              'developer','[]','[]','[]',?)""",
                    (item_id, f"item-{item_id}", stable_id, kind, executable, title,
                     parent, f"{item_id:064d}"))
                if status:
                    db.execute(
                        """INSERT INTO autonomous_backlog_item_states
                           (identity,item_id,sequence,status,actor,command_id,reason)
                           VALUES(?,?,1,?,'system','cmd','seeded')""",
                        (f"state-{item_id}", item_id, status))
        costs = StudioCosts(storage)
        costs.set_limit(MISSION, amount=5.0, actor="miha")
        costs.record(MISSION, amount=0.8, task_key="t1", role="developer",
                     provider="claude", model="opus")
        costs.record(MISSION, amount=0.3, kind="estimated", task_key="t2",
                     role="planner")
        StudioRoster(storage).enable(MISSION, "tester", actor="miha")
        cycles = StudioCycles(storage)
        cycles.pause(MISSION, actor="miha", finishing=["збірка рівня"])
        cycles.comment(MISSION, "хочу подвійний стрибок", author="miha")
        PaidTools(storage).ask(
            MISSION, "Unity",
            reason=Message("Потрібен Unity для 3D.", "Unity is needed for 3D."),
            blocks=["3D level"])
        machines = StudioMachines(storage)
        machines.register("web", name="lokvetia-core-web", kind="web_container")
        machines.register(
            "pc", name="desktop-tefqhlo", kind="this_pc",
            capabilities=["godot"], video_memory_gb=8.0)
    finally:
        storage.db.close()


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class StudioPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        database = cls.root / "state.db"
        seed(database)
        cls.app = create_app(cls.root, database)
        cls.server = uvicorn.Server(uvicorn.Config(
            cls.app, host="127.0.0.1", port=0, log_level="error", access_log=False,
        ))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 20
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("The studio server did not start")
        port = cls.server.servers[0].sockets[0].getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(10)
        cls.directory.cleanup()

    def open(self, path: str = "/studio", width: int = 1280, height: int = 900):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"{self.url}{path}", wait_until="networkidle")
        page.wait_for_selector(".stage")
        page.wait_for_timeout(200)
        return page

    def test_the_whole_plan_is_on_the_screen_with_its_states(self):
        page = self.open("/studio?lang=en")
        try:
            plan = page.inner_text("#stages")
            self.assertIn("Кіт стрибає", plan)
            self.assertIn("Done", plan)
            self.assertIn("In progress", plan)
            self.assertIn("Blocked", plan)
        finally:
            page.close()

    def test_create_explains_missing_qualification_and_stays_disabled(self):
        page = self.open("/studio?lang=en")
        try:
            expect(page.locator("#create-game")).to_be_disabled()
            expect(page.locator("#create-readiness")).to_contain_text("Qualify")
        finally:
            page.close()

    def test_create_sends_one_confirmed_request_and_opens_its_mission(self):
        page = self.browser.new_page()
        sent = []
        page.route("**/api/studio/local-readiness?*", lambda route: route.fulfill(
            json={"can_start": True, "summary": "Qualified test source"}))
        def create(route):
            sent.append(json.loads(route.request.post_data))
            route.fulfill(status=202, json={"mission_key": MISSION, "mission_id": 1})
        page.route("**/api/studio/create?*", create)
        try:
            page.goto(self.url + "/studio?lang=en")
            expect(page.locator("#create-game")).to_be_enabled()
            page.fill("#game-title", "Coins")
            page.fill("#game-idea", "A small coin game")
            page.click("#create-game")
            expect(page.locator("#create-result")).not_to_be_empty()
            self.assertEqual(len(sent), 1)
            self.assertTrue(sent[0]["confirmed"])
            self.assertEqual(sent[0]["idea"], "A small coin game")
            self.assertEqual(len(sent[0]["command_id"]), 36)
            expect(page.locator("#mission")).to_have_value(MISSION)
        finally:
            page.close()

    def test_no_internal_code_reaches_the_screen(self):
        page = self.open("/studio?lang=en")
        try:
            self.assertNotRegex(page.inner_text("#main"), r"AF-[A-Z]{1,3}-\d")
        finally:
            page.close()

    def test_a_stage_with_no_build_offers_no_play_button(self):
        page = self.open("/studio?lang=en")
        try:
            self.assertIn("nothing to test", page.inner_text(".boundary"))
            self.assertEqual(page.locator(".stage-head a").count(), 0)
        finally:
            page.close()

    def test_the_open_questions_are_the_ones_a_person_must_answer(self):
        page = self.open("/studio?lang=en")
        try:
            questions = page.inner_text("#questions")
            self.assertIn("Unity", questions)
            self.assertIn("Decline", questions)
        finally:
            page.close()

    def test_money_shows_spent_reserved_and_an_unearned_forecast_as_unknown(self):
        page = self.open("/studio?lang=en")
        try:
            money = page.inner_text("#money")
            self.assertIn("0.80 USD", money)
            self.assertIn("0.30 USD", money)
            self.assertIn("No forecast", page.inner_text("#forecast"))
            self.assertIn("developer", page.inner_text("#by-role"))
        finally:
            page.close()

    def test_the_team_shows_who_accepts_the_work(self):
        page = self.open("/studio?lang=en")
        try:
            self.assertIn("no longer accepts", page.inner_text("#acceptance"))
            self.assertIn("Tester", page.inner_text("#team"))
        finally:
            page.close()

    def test_a_game_nobody_delegated_says_so_and_offers_the_keys(self):
        page = self.open("/studio?lang=en")
        try:
            self.assertIn("Nobody has asked", page.inner_text("#run-state"))
            self.assertTrue(page.locator("#run-grant").is_visible())
            self.assertFalse(page.locator("#run-revoke").is_visible())
            self.assertIn("has not done anything here yet", page.inner_text("#run-empty"))
        finally:
            page.close()

    def test_handing_the_studio_the_keys_takes_two_clicks_and_a_name(self):
        page = self.open("/studio?lang=en")
        try:
            # Granting changes state, so it gets a game of its own rather than
            # disturbing the seeded one every other test reads.
            page.fill("#mission", "keys-of-its-own")
            page.dispatch_event("#mission", "change")
            page.wait_for_selector("#plan-empty:visible")
            page.click("#run-grant")
            self.assertIn("Enter a name", page.inner_text("#run-result"))
            page.fill("#run-actor", "Miha")
            page.click("#run-grant")
            # The first click with a name explains what the studio will then do.
            self.assertIn("permits planning in your name",
                          page.inner_text("#run-result"))
            self.assertIn("Nobody has asked", page.inner_text("#run-state"))
            page.click("#run-grant")
            page.wait_for_selector("#run-revoke:visible")
            self.assertIn("Miha asked the studio", page.inner_text("#run-state"))
            self.assertIn("plan", page.inner_text("#run-mandate"))
        finally:
            page.close()

    def test_the_loop_shows_the_pause_and_what_was_asked_for(self):
        page = self.open("/studio?lang=en")
        try:
            self.assertIn("Paused", page.inner_text("#cycle-state"))
            self.assertIn("подвійний стрибок", page.inner_text("#comments"))
        finally:
            page.close()

    def test_continuing_needs_a_name_and_changes_nothing_without_one(self):
        page = self.open("/studio?lang=en")
        try:
            page.click("#resume")
            page.wait_for_timeout(200)
            self.assertIn("Enter a name", page.inner_text("#loop-result"))
            self.assertIn("Paused", page.inner_text("#cycle-state"))
        finally:
            page.close()

    def test_pausing_and_continuing_moves_a_game_to_the_next_cycle(self):
        # A game of its own, so mutating the loop cannot disturb the other tests.
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(f"{self.url}/studio?lang=en", wait_until="networkidle")
            page.wait_for_selector("#cycle-state:not(:empty)")
            page.fill("#mission", "loop-demo")
            page.dispatch_event("#mission", "change")
            page.wait_for_timeout(400)
            page.fill("#actor", "miha")
            page.click("#pause")
            expect(page.locator("#cycle-state")).to_contain_text("Paused")
            self.assertIn("Paused", page.inner_text("#cycle-state"))
            page.click("#resume")
            expect(page.locator("#cycle-state")).to_contain_text("Running")
            self.assertIn("Running", page.inner_text("#cycle-state"))
            self.assertIn("Cycle 2", page.inner_text("#cycle-state"))
        finally:
            page.close()

    def test_the_machines_are_named_and_the_container_is_marked(self):
        page = self.open("/studio?lang=en")
        try:
            machines = page.inner_text("#machines")
            self.assertIn("your PC: desktop-tefqhlo", machines)
            self.assertIn("games are not built here", machines)
        finally:
            page.close()

    def test_adding_a_role_shows_what_it_costs_before_it_changes_anything(self):
        # A game of its own: this test switches a role on.
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(f"{self.url}/studio?lang=en", wait_until="networkidle")
            page.wait_for_selector("#team li")
            page.fill("#mission", "roster-demo")
            page.dispatch_event("#mission", "change")
            # The switch is done when the empty plan of the new game is showing;
            # clicking before that would be clicking on the old game's list.
            page.wait_for_selector("#plan-empty:not([hidden])")
            page.fill("#actor", "miha")

            artist = page.locator("#team li", has_text="Artist")
            artist.get_by_role("button", name="Turn on").click()
            artist.locator(".note").wait_for()
            expect(artist.locator(".note")).to_contain_text("unknown")
            note = artist.locator(".note").inner_text()
            self.assertIn("unknown", note)
            self.assertIn("one subscription is enough", note)
            self.assertIn(
                "Off", artist.inner_text(),
                "showing the consequence must not enable the role")

            artist.get_by_role("button", name="Turn it on anyway").click()
            expect(page.locator("#team li", has_text="Artist")).not_to_contain_text("Off")
            self.assertNotIn(
                "Off", page.locator("#team li", has_text="Artist").inner_text())
        finally:
            page.close()

    def test_a_role_cannot_be_added_without_a_name(self):
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(f"{self.url}/studio?lang=en", wait_until="networkidle")
            page.wait_for_selector("#team li")
            page.fill("#mission", "roster-unnamed")
            page.dispatch_event("#mission", "change")
            page.wait_for_selector("#plan-empty:not([hidden])")
            sound = page.locator("#team li", has_text="Sound designer")
            sound.get_by_role("button", name="Turn on").click()
            sound.locator(".note").wait_for()
            sound.get_by_role("button", name="Turn it on anyway").click()
            page.wait_for_timeout(400)
            self.assertIn("Enter a name", page.inner_text("#loop-result"))
            self.assertIn("Off", page.locator("#team li", has_text="Sound designer").inner_text())
        finally:
            page.close()

    def test_the_page_meets_the_accessibility_criteria_in_both_languages(self):
        for path in ("/studio", "/studio?lang=en"):
            for width, height in ((320, 720), (1280, 900)):
                with self.subTest(page=path, width=width):
                    page = self.open(path, width, height)
                    try:
                        payload = page.evaluate(COLLECTOR_SCRIPT)
                        payload["url"] = path
                        result = audit(payload)
                        self.assertTrue(result.passed, result.report())
                    finally:
                        page.close()


if __name__ == "__main__":
    unittest.main()
