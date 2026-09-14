"""Accessibility judged on the rendered interface, in a real browser.

The success criteria this suite covers cannot be checked by reading markup:
contrast is a property of the colours that actually painted, target size of the
box that actually laid out, focus visibility of the style that actually applied,
and reflow of the document that actually overflowed.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_factory.accessibility import COLLECTOR_SCRIPT, audit, summarise
from agent_factory.web import create_app

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the suite skips without a browser
    sync_playwright = None

PAGES = ("/", "/settings", "/settings?lang=en", "/work", "/work?lang=en",
         "/studio", "/studio?lang=en", "/hardware", "/login")
VIEWPORTS = (
    ("phone", 320, 720),
    ("laptop", 1280, 800),
    # 200% zoom on a 1280x800 laptop lays out as a 640x400 CSS viewport.
    ("zoom-200", 640, 400),
)


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class AccessibilityBrowserTests(unittest.TestCase):
    """One browser, every page, every viewport, judged by the shared rules."""

    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.app = create_app(cls.root, cls.root / "state.db")
        cls.server = uvicorn.Server(uvicorn.Config(
            cls.app, host="127.0.0.1", port=0, log_level="error", access_log=False,
        ))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 20
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Accessibility server did not start")
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

    def snapshot(self, path: str, width: int, height: int):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        try:
            page.goto(f"{self.url}{path}", wait_until="networkidle")
            page.wait_for_timeout(250)
            payload = page.evaluate(COLLECTOR_SCRIPT)
        finally:
            page.close()
        payload["url"] = path
        return payload

    def test_every_page_meets_the_criteria_at_every_viewport(self) -> None:
        results = []
        for path in PAGES:
            for label, width, height in VIEWPORTS:
                with self.subTest(page=path, viewport=label):
                    result = audit(self.snapshot(path, width, height))
                    results.append(result)
                    self.assertTrue(result.passed, result.report())
        summary = summarise(results)
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["problems"], 0)
        self.assertGreaterEqual(len(summary["pages"]), len(PAGES) * len(VIEWPORTS))

    def test_the_settings_page_is_accessible_in_english_too(self) -> None:
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(f"{self.url}/settings?lang=en", wait_until="networkidle")
            page.wait_for_selector(".section-card")
            page.wait_for_timeout(250)
            self.assertEqual(page.evaluate("document.documentElement.lang"), "en")
            self.assertEqual(page.locator("h1").inner_text(), "Settings")
            self.assertEqual(
                page.locator(".section-card h2").first.inner_text(), "Godot engine",
            )
            payload = page.evaluate(COLLECTOR_SCRIPT)
            payload["url"] = "/settings?lang=en"
            result = audit(payload)
            self.assertTrue(result.passed, result.report())
        finally:
            page.close()

    def test_the_settings_page_can_be_operated_from_the_keyboard(self) -> None:
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(f"{self.url}/settings", wait_until="networkidle")
            page.wait_for_selector(".section-card")

            page.keyboard.press("Tab")
            self.assertEqual(
                page.evaluate("document.activeElement.className"), "skip",
                "the first stop must be the link that skips the navigation",
            )
            page.keyboard.press("Enter")
            page.wait_for_timeout(150)

            reached = page.evaluate(
                """() => {
                    const stops = [];
                    for (let index = 0; index < 40; index += 1) {
                        stops.push(document.activeElement.id || document.activeElement.tagName);
                    }
                    return stops.length;
                }"""
            )
            self.assertEqual(reached, 40)

            page.focus("#actor")
            page.keyboard.type("miha")
            self.assertEqual(page.input_value("#actor"), "miha")
        finally:
            page.close()

    def test_a_dialog_returns_focus_to_the_control_that_opened_it(self) -> None:
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(f"{self.url}/settings", wait_until="networkidle")
            page.wait_for_selector(".section-card")
            page.fill("#actor", "miha")
            # Sections are folded shut so the page is scannable; a person opens
            # the one they came for, and so does this test.
            page.locator(".section-card").filter(
                has_text="Захищати закріплені версії"
            ).first.locator("summary").click()
            field = page.locator(".field").filter(
                has_text="Захищати закріплені версії"
            ).first
            field.locator("input[type=checkbox]").first.uncheck()
            field.get_by_role("button", name="Зберегти").click()
            page.wait_for_timeout(400)
            self.assertEqual(page.locator("#confirm[open]").count(), 1)

            inside = page.evaluate(
                "() => document.getElementById('confirm').contains(document.activeElement)"
            )
            self.assertTrue(inside, "focus must move into the dialog that opened")

            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            self.assertEqual(page.locator("#confirm[open]").count(), 0)
            self.assertTrue(
                page.evaluate("() => document.body.contains(document.activeElement)")
            )
        finally:
            page.close()

    def test_no_page_scrolls_sideways_on_a_narrow_screen(self) -> None:
        for path in PAGES:
            with self.subTest(page=path):
                payload = self.snapshot(path, 320, 720)
                self.assertLessEqual(
                    payload["document_width"], payload["viewport_width"] + 1,
                    f"{path} overflows a 320px viewport",
                )


if __name__ == "__main__":
    unittest.main()
