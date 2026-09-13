"""Publish complete reports and serve them only after the gateway session check."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


publisher = module("tested_progress_publisher", "scripts/progress_report.py")
gateway = module("tested_progress_gateway", "ops/test-deploy/gateway.py")


class PublicationTests(unittest.TestCase):
    def test_one_unreadable_project_preserves_the_previous_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.json"
            output = root / "progress.json"
            config.write_text(json.dumps({"projects": [{"id": "core"}, {"id": "cloud"}]}))
            output.write_text('{"previous": true}')
            with patch.object(sys, "argv", ["report", "--config", str(config), "--output", str(output)]), \
                    patch.object(publisher, "collect", side_effect=[object(), publisher.ProgressError("missing manifest")]):
                self.assertEqual(publisher.main(), 1)
            self.assertEqual(output.read_text(), '{"previous": true}')

    def test_a_missing_configured_manifest_fails_instead_of_shrinking_the_total(self):
        entry = {"id": "core", "repository": "HappyMiha/Lokvetia-Core",
                 "repo_path": str(ROOT), "manifests": ["missing.json"]}
        with patch.object(publisher, "read_manifest", side_effect=publisher.ProgressError("missing")), \
                patch.object(publisher, "revision", return_value="a" * 40), \
                patch.object(publisher, "read_commits", return_value=()):
            with self.assertRaises(publisher.ProgressError):
                publisher.collect(entry)


class GatewayTests(unittest.TestCase):
    def check_route(self, path, authenticated=True, auth_status=200, workspace_access=True):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "routes.json").write_text(json.dumps({"test.lokvetia.com": {"container": "test-core"}}))
            (root / "progress.html").write_text("<h1>Progress</h1>")
            (root / "progress.json").write_text(json.dumps({"projects": [], "private_test_marker": True}))
            def auth(request):
                self.assertEqual(request.url.path, "/auth/session")
                return httpx.Response(auth_status, json={"authenticated": authenticated, "workspace_access": workspace_access})
            with patch.object(gateway, "state", root), \
                    patch.object(gateway.httpx, "AsyncClient", return_value=httpx.AsyncClient(transport=httpx.MockTransport(auth))), \
                    TestClient(gateway.app, base_url="http://test.lokvetia.com", follow_redirects=False) as client:
                return client.get(path)

    def test_guest_cannot_read_the_report(self):
        response = self.check_route("/progress/status", authenticated=False)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("private_test_marker", response.text)

    def test_personal_account_cannot_read_operator_report(self):
        response = self.check_route('/progress/status', workspace_access=False)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('private_test_marker', response.text)

    def test_guest_page_redirects_to_sign_in(self):
        response = self.check_route("/progress", authenticated=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_failed_authentication_service_denies_the_report(self):
        self.assertEqual(self.check_route("/progress/status", auth_status=503).status_code, 401)

    def test_signed_in_user_receives_uncached_report_and_page(self):
        for path in ("/progress", "/progress/status"):
            with self.subTest(path=path):
                response = self.check_route(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
