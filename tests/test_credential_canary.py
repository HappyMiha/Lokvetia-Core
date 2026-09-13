"""A planted secret must not survive anywhere it could be read back (AF-GC-010).

Existing tests check one sink each. This one plants a single distinctive canary,
drives the credential paths that handle it, and then sweeps every place the
product writes or returns bytes: both databases and every file under the
workspace, every table value, every readable API response, the published schema,
and the text of the errors callers receive.

The sweep looks for the literal canary. That is deliberate: a redaction that
works only because the value was reformatted on the way out is not a redaction,
and a leak that survives one encoding is still a leak.

What this does not prove: the connection tests use the in-memory credential
store, so they exercise the product's own sinks and not the operating system's
keychain. Whether the OS store itself holds the value safely is a separate
question, answered on the supported operating system, not here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_factory.credentials import REDACTED, CredentialBroker
from agent_factory.storage import SQLiteStorage
from agent_factory.web import create_app
from tests.test_connector_eligibility import AT, approval_fixture
from tests.test_credential_connections import MemoryStore

# Distinctive enough that an accidental match cannot be a coincidence.
CANARY = "sk-canary-7Qx9ZtP4mWn2Vb8LrJ6"
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")


def planted_bytes() -> tuple[bytes, ...]:
    return tuple(CANARY.encode(encoding) for encoding in ENCODINGS)


def files_holding_the_canary(root: Path) -> list[str]:
    """Every file under the workspace that contains the canary in any encoding."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if any(marker in raw for marker in planted_bytes()):
            found.append(path.relative_to(root).as_posix())
    return found


def rows_holding_the_canary(database: Path) -> list[str]:
    """Every table value in the database that contains the canary."""
    found = []
    if not database.exists():
        return found
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        for table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                for value in row:
                    text = value if isinstance(value, str) else (
                        value.decode("utf-8", "replace") if isinstance(value, bytes) else ""
                    )
                    if CANARY in text:
                        found.append(table)
                        break
    finally:
        connection.close()
    return sorted(set(found))


class BrokerCanaryTests(unittest.TestCase):
    """The broker keeps the value in memory; nothing it writes may contain it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.db"
        self.storage = SQLiteStorage(self.database)
        self.addCleanup(self.storage.close)
        self.broker = CredentialBroker(self.storage)
        self.now = datetime(2026, 8, 12, tzinfo=timezone.utc)

    def issue(self) -> str:
        return self.broker.issue(
            tenant_id="tenant-a", mission_id="mission-a", tool_key="github.issue",
            operations=("read",), preapproved_operations={"read"},
            environment_key="GITHUB_API_TOKEN", secret_value=CANARY,
            ttl_seconds=60, actor="Credential Broker", now=self.now,
        )

    def use(self, handle: str, executor, **overrides):
        values = {
            "tenant_id": "tenant-a", "mission_id": "mission-a",
            "tool_key": "github.issue", "operation": "read", "prompt": "List issues.",
            "arguments": {"repository": "example/repo"}, "executor": executor,
            "actor": "Worker", "now": self.now,
        }
        values.update(overrides)
        return self.broker.use(handle, **values)

    def test_a_tool_that_echoes_the_secret_cannot_write_it_to_evidence(self):
        handle = self.issue()
        result = self.use(handle, lambda env, args: {"echo": env["GITHUB_API_TOKEN"], "args": args})
        self.assertEqual(result["echo"], REDACTED)
        self.assertEqual(rows_holding_the_canary(self.database), [])
        self.assertEqual(files_holding_the_canary(self.root), [])

    def test_a_tool_that_fails_with_the_secret_cannot_write_it_to_evidence(self):
        handle = self.issue()
        def explode(env, args):
            raise RuntimeError(f"upstream rejected {env['GITHUB_API_TOKEN']}")
        with self.assertRaises(RuntimeError) as failure:
            self.use(handle, explode)
        # The caller is told what failed, with the value replaced, not removed.
        self.assertNotIn(CANARY, str(failure.exception))
        self.assertIn(REDACTED, str(failure.exception))
        self.assertEqual(rows_holding_the_canary(self.database), [])
        self.assertEqual(files_holding_the_canary(self.root), [])

    def test_a_nested_and_non_string_echo_is_still_redacted(self):
        handle = self.issue()
        result = self.use(
            handle,
            lambda env, args: {
                "outer": [{"inner": env["GITHUB_API_TOKEN"]}],
                CANARY: "a key can carry it too",
            },
        )
        self.assertNotIn(CANARY, json.dumps(result, ensure_ascii=False))
        self.assertEqual(rows_holding_the_canary(self.database), [])

    def test_the_secret_can_never_enter_a_prompt_or_tool_arguments(self):
        handle = self.issue()
        with self.assertRaises(PermissionError) as prompt_denial:
            self.use(handle, lambda env, args: {}, prompt=f"Use {CANARY} please")
        with self.assertRaises(PermissionError) as argument_denial:
            self.use(handle, lambda env, args: {}, arguments={"token": CANARY})
        for denial in (prompt_denial, argument_denial):
            self.assertNotIn(CANARY, str(denial.exception))
        self.assertEqual(rows_holding_the_canary(self.database), [])
        self.assertEqual(files_holding_the_canary(self.root), [])

    def test_revocation_drops_the_value_and_leaves_no_trace(self):
        handle = self.issue()
        self.use(handle, lambda env, args: {"ok": True})
        self.broker.revoke(handle, actor="Owner", reason="rotating the key")
        with self.assertRaises(PermissionError):
            self.use(handle, lambda env, args: {"ok": True})
        self.assertEqual(rows_holding_the_canary(self.database), [])
        self.assertEqual(files_holding_the_canary(self.root), [])

    def test_the_sweep_itself_detects_a_planted_leak(self):
        """A canary sweep that cannot fail proves nothing."""
        (self.root / "leak.log").write_text(f"token={CANARY}\n", encoding="utf-8")
        self.assertEqual(files_holding_the_canary(self.root), ["leak.log"])
        with self.storage.db:
            self.storage.db.execute(
                "CREATE TABLE canary_probe(value TEXT)"
            ).execute("INSERT INTO canary_probe(value) VALUES(?)", (CANARY,))
        self.assertEqual(rows_holding_the_canary(self.database), ["canary_probe"])


class ConnectionApiCanaryTests(unittest.TestCase):
    """The stored connection keeps a reference; the value never comes back out."""

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AGENT_FACTORY_API_TOKEN": "local-synthetic-token",
            "AGENT_FACTORY_API_ACTOR": "Owner",
            "AGENT_FACTORY_API_ROLE": "operations_owner",
            "AGENT_FACTORY_API_SCOPES": "read,write,control",
            "AGENT_FACTORY_API_TENANTS": "local",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MemoryStore()
        self.app = create_app(self.root, self.root / "core.db", credential_store=self.store)
        clock = patch("agent_factory.connector_eligibility.utc_now", return_value=AT)
        clock.start()
        self.addCleanup(clock.stop)
        self.app.state.connector_setup_approval = lambda **scope: approval_fixture(**scope)
        # This canary tests secret-bearing HTTP surfaces, not installed CLI
        # liveness. A slow unrelated CLI must not outlive the temporary DB.
        health = patch("agent_factory.providers.CLIProvider.health", autospec=True,
                       side_effect=lambda provider: {"provider": provider.name,
                           "healthy": False, "error": "Synthetic canary probe"})
        health.start()
        self.addCleanup(health.stop)
        self.client = TestClient(self.app, base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {
            "Authorization": "Bearer local-synthetic-token",
            "X-Agent-Factory-Confirm": "true",
        }

    def connect(self):
        return self.client.post(
            "/api/credential-connections",
            headers=self.headers,
            content=json.dumps({"provider": "openai", "secret": CANARY, "confirmed": True}),
        )

    def readable_surfaces(self) -> dict[str, str]:
        surfaces = {}
        for url in (
            "/api/credential-connections",
            "/api/openapi.json",
            "/settings/credentials",
            "/api/audit-events",
            "/api/providers",
        ):
            result = self.client.get(url, headers=self.headers)
            surfaces[url] = result.text + json.dumps(dict(result.headers))
        return surfaces

    def test_no_readable_surface_returns_the_stored_secret(self):
        created = self.connect()
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn(CANARY, created.text)
        for url, body in self.readable_surfaces().items():
            with self.subTest(url=url):
                self.assertNotIn(CANARY, body)

    def test_nothing_under_the_workspace_holds_the_secret_after_a_connection(self):
        self.assertEqual(self.connect().status_code, 201)
        self.readable_surfaces()
        self.assertEqual(files_holding_the_canary(self.root), [])

    def test_disconnect_removes_the_value_and_reports_no_secret(self):
        reference = self.connect().json()["id"]
        removed = self.client.delete(
            "/api/credential-connections/" + reference, headers=self.headers
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["status"], "revoked")
        self.assertFalse(self.store.values)
        self.assertNotIn(CANARY, removed.text)
        self.assertEqual(files_holding_the_canary(self.root), [])

    def test_a_store_failure_carrying_the_secret_is_not_echoed_or_written(self):
        with patch.object(self.store, "put", side_effect=RuntimeError(CANARY)):
            failed = self.connect()
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn(CANARY, failed.text)
        self.assertEqual(files_holding_the_canary(self.root), [])

    def test_a_rejected_request_body_is_never_persisted(self):
        rejected = self.client.post(
            "/api/credential-connections",
            headers=self.headers,
            content=json.dumps({"provider": "openai", "secret": CANARY, "confirmed": "yes"}),
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn(CANARY, rejected.text)
        self.assertEqual(files_holding_the_canary(self.root), [])


if __name__ == "__main__":
    unittest.main()
