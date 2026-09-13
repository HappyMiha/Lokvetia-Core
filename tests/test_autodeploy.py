import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('deploy_controller', ROOT / 'scripts' / 'autodeploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentSafetyTests(unittest.TestCase):
    def test_release_tags_are_distinct_per_service_and_attempt(self):
        core = {'id': 'core', 'service': 'lokvetia'}
        identity = {'id': 'identity', 'service': 'identity'}
        self.assertNotEqual(deploy.release_image(core, 'revision-first'), deploy.release_image(core, 'revision-second'))
        self.assertNotEqual(deploy.release_image(core, 'revision-first'), deploy.release_image(identity, 'revision-first'))

    def test_missing_manifest_can_be_archived_without_pausing_immutable_container(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'image.tar'
            calls = []
            def execute(args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    raise deploy.DeployError('No such image: old-manifest')
                if args[1] == 'commit':
                    return 'sha256:recovery'
                archive.with_suffix('.partial').write_bytes(b'recovery-image')
                return ''
            with patch.object(deploy, 'command', side_effect=execute):
                deploy.archive_image('old-container', {'Image': 'old-manifest', 'HostConfig': {'ReadonlyRootfs': True}}, archive)
            self.assertEqual(archive.read_bytes(), b'recovery-image')
            self.assertIn('--pause=false', calls[1])
            self.assertEqual(deploy.read_json(archive.with_suffix('.json'))['source_container'], 'old-container')

    def test_mutable_container_is_not_snapshotted_during_live_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(deploy, 'command', side_effect=deploy.DeployError('No such image')) as execute:
                with self.assertRaises(deploy.DeployError):
                    deploy.archive_image('mutable', {'Image': 'missing', 'HostConfig': {'ReadonlyRootfs': False}}, Path(folder) / 'image.tar')
            self.assertEqual(execute.call_count, 1)

    def test_schema_removal_or_change_is_blocked(self):
        self.assertFalse(deploy.compatible({'state.db': 'old'}, {}))
        self.assertFalse(deploy.compatible({'state.db': 'old'}, {'state.db': 'new'}))
        self.assertTrue(deploy.compatible({'state.db': 'old'}, {'state.db': 'old', 'new.db': 'new'}))

    def test_a_release_that_only_adds_tables_can_roll_out(self):
        before = {'state.db': {'runs': 'runs-signature'}}
        after = {'state.db': {'runs': 'runs-signature', 'setting_overrides': 'settings-signature'}}
        self.assertTrue(deploy.compatible(before, after))

    def test_a_rewritten_table_is_still_blocked(self):
        self.assertFalse(deploy.compatible({'state.db': {'runs': 'before'}}, {'state.db': {'runs': 'after'}}))

    def test_a_dropped_table_is_still_blocked(self):
        self.assertFalse(deploy.compatible({'state.db': {'runs': 'a', 'leases': 'b'}}, {'state.db': {'runs': 'a'}}))

    def test_a_missing_database_is_still_blocked(self):
        self.assertFalse(deploy.compatible({'state.db': {'runs': 'a'}}, {'other.db': {'runs': 'a'}}))

    def test_adding_a_table_leaves_existing_signatures_untouched(self):
        snapshot = self.snapshot_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / 'state.db')) as db, db:
                db.execute('CREATE TABLE runs(value TEXT)')
            before = snapshot.schemas(root)
            with closing(sqlite3.connect(root / 'state.db')) as db, db:
                db.execute('CREATE TABLE setting_overrides(key TEXT PRIMARY KEY)')
            after = snapshot.schemas(root)
            self.assertEqual(before['state.db']['runs'], after['state.db']['runs'])
            self.assertIn('setting_overrides', after['state.db'])
            self.assertTrue(deploy.compatible(before, after))

    def test_rewriting_a_table_changes_its_signature(self):
        snapshot = self.snapshot_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / 'state.db')) as db, db:
                db.execute('CREATE TABLE runs(value TEXT)')
            before = snapshot.schemas(root)
            with closing(sqlite3.connect(root / 'state.db')) as db, db:
                db.execute('ALTER TABLE runs ADD COLUMN added TEXT')
            self.assertFalse(deploy.compatible(before, snapshot.schemas(root)))

    def snapshot_module(self):
        snapshot_file = ROOT / 'ops' / 'test-deploy' / 'snapshot.py'
        if not snapshot_file.exists():
            self.skipTest('Snapshot implementation is owned by Core')
        module_spec = importlib.util.spec_from_file_location('snapshot', snapshot_file)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        return module

    def test_atomic_publication_does_not_touch_client_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = root / 'client.db'
            with closing(sqlite3.connect(live)) as db, db:
                db.execute('CREATE TABLE writes(value TEXT)')
                db.execute("INSERT INTO writes VALUES ('saved during release')")
            routes = root / 'routes.json'
            deploy.atomic_json(routes, {'test.lokvetia.com': {'container': 'new'}})
            controller = object.__new__(deploy.Controller)
            controller.routes_path = routes
            controller.rollback_route({'host': 'test.lokvetia.com'}, {'container': 'old'})
            self.assertEqual(deploy.read_json(routes)['test.lokvetia.com']['container'], 'old')
            with closing(sqlite3.connect(live)) as db:
                self.assertEqual(db.execute('SELECT value FROM writes').fetchone()[0], 'saved during release')

    def test_rollback_preserves_other_project_routing(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = object.__new__(deploy.Controller)
            controller.routes_path = Path(folder) / 'routes.json'
            deploy.atomic_json(controller.routes_path, {'core': {'container': 'new'}, 'cloud': {'container': 'cloud'}})
            controller.rollback_route({'host': 'core'}, {'container': 'old'})
            self.assertEqual(deploy.read_json(controller.routes_path)['cloud'], {'container': 'cloud'})

    def test_corrupt_state_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text('{broken')
            with self.assertRaises(json.JSONDecodeError):
                deploy.read_json(path, {})

    def test_failed_ci_cannot_be_deployed(self):
        controller = object.__new__(deploy.Controller)
        with patch.object(deploy, 'command', return_value='[{"status":"completed","conclusion":"failure"}]'):
            self.assertFalse(controller.checks_passed({'repository': 'HappyMiha/Lokvetia-Core'}, 'a' * 40))

    def test_missing_ci_cannot_be_deployed(self):
        controller = object.__new__(deploy.Controller)
        with patch.object(deploy, 'command', return_value='[]'):
            self.assertFalse(controller.checks_passed({'repository': 'HappyMiha/Lokvetia-Core'}, 'a' * 40))

    def test_invalid_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(deploy.DeployError):
                deploy.Controller({'state_root': folder, 'runtime_bundle': folder, 'projects': [{'repository': 'attacker/repo'}]})

    def test_online_sqlite_snapshot_includes_committed_wal_writes(self):
        snapshot_file = ROOT / 'ops' / 'test-deploy' / 'snapshot.py'
        if not snapshot_file.exists():
            self.skipTest('Snapshot implementation is owned by Core')
        module_spec = importlib.util.spec_from_file_location('snapshot', snapshot_file)
        snapshot = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(snapshot)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'live'; source.mkdir()
            db = sqlite3.connect(source / 'state.db')
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE records(value TEXT)')
                db.execute("INSERT INTO records VALUES ('committed')"); db.commit()
                snapshot.backup(source, root / 'backup')
                with closing(sqlite3.connect(root / 'backup' / 'state.db')) as backup:
                    self.assertEqual(backup.execute('SELECT value FROM records').fetchone()[0], 'committed')
                self.assertFalse((root / 'backup' / 'state.db-wal').exists())
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()


class ReleaseHistoryTests(unittest.TestCase):
    """Finished attempts stay visible after the next one starts."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.project = {'id': 'core', 'name': 'Lokvetia Core', 'service': 'lokvetia',
                        'repository': 'HappyMiha/Lokvetia-Core', 'host': 'test.lokvetia.com'}

    def controller(self):
        return deploy.Controller({'state_root': str(self.root), 'runtime_bundle': str(self.root),
                                  'projects': [self.project]})

    def history(self, controller):
        return controller.status['history'].get('core', [])

    def test_only_a_finished_attempt_is_remembered(self):
        controller = self.controller()
        controller.report(self.project, 'build')
        controller.report(self.project, 'activate')
        self.assertEqual(self.history(controller), [])
        controller.report(self.project, 'success', 'success', commit='a' * 40,
                          finished_at='2026-09-11T10:00:00+00:00')
        self.assertEqual(len(self.history(controller)), 1)
        self.assertEqual(self.history(controller)[0]['state'], 'success')

    def test_the_newest_attempt_is_listed_first(self):
        controller = self.controller()
        controller.report(self.project, 'failure', 'failure', commit='a' * 40,
                          finished_at='2026-09-11T10:00:00+00:00', error='first')
        controller.report(self.project, 'success', 'success', commit='b' * 40,
                          finished_at='2026-09-11T11:00:00+00:00')
        self.assertEqual([entry['commit'][:1] for entry in self.history(controller)], ['b', 'a'])

    def test_repeating_one_attempt_does_not_duplicate_its_entry(self):
        controller = self.controller()
        for _ in range(3):
            controller.report(self.project, 'failure', 'failure', commit='a' * 40,
                              finished_at='2026-09-11T10:00:00+00:00', error='same attempt')
        self.assertEqual(len(self.history(controller)), 1)

    def test_the_list_stays_bounded(self):
        controller = self.controller()
        for index in range(deploy.HISTORY_PER_PROJECT + 7):
            controller.report(self.project, 'success', 'success', commit=f'{index:040d}',
                              finished_at=f'2026-09-11T10:{index:02d}:00+00:00')
        self.assertEqual(len(self.history(controller)), deploy.HISTORY_PER_PROJECT)
        self.assertTrue(self.history(controller)[0]['commit'].endswith(str(
            deploy.HISTORY_PER_PROJECT + 6)))

    def test_a_long_error_is_truncated_before_publication(self):
        controller = self.controller()
        controller.report(self.project, 'failure', 'failure', commit='a' * 40,
                          finished_at='2026-09-11T10:00:00+00:00', error='x' * 9000)
        self.assertEqual(len(self.history(controller)[0]['error']), deploy.HISTORY_ERROR_LIMIT)

    def test_history_survives_a_controller_restart(self):
        controller = self.controller()
        controller.report(self.project, 'success', 'success', commit='a' * 40,
                          finished_at='2026-09-11T10:00:00+00:00')
        self.assertEqual(len(self.history(self.controller())), 1)

    def test_published_state_without_history_upgrades_in_place(self):
        public = self.root / 'public'
        public.mkdir(parents=True, exist_ok=True)
        deploy.atomic_json(public / 'status.json',
                           {'projects': {'core': {'phase': 'success'}}, 'updated_at': '2026-09-11T09:00:00+00:00'})
        controller = self.controller()
        self.assertEqual(controller.status['history'], {})
        controller.report(self.project, 'failure', 'failure', commit='a' * 40,
                          finished_at='2026-09-11T10:00:00+00:00', error='boom')
        published = deploy.read_json(public / 'status.json')
        self.assertEqual(published['history']['core'][0]['error'], 'boom')

    def test_a_rollback_result_is_carried_into_the_entry(self):
        controller = self.controller()
        controller.report(self.project, 'failure', 'failure', commit='a' * 40, rollback='success',
                          finished_at='2026-09-11T10:00:00+00:00', error='health check failed')
        self.assertEqual(self.history(controller)[0]['rollback'], 'success')


    def test_repeated_terminal_report_without_timestamp_keeps_one_entry(self):
        controller = self.controller()
        with patch.object(deploy, 'now', side_effect=[f'time-{i}' for i in range(20)]):
            for _ in range(3):
                controller.report(self.project, 'failure', 'failure', commit='a' * 40)
        self.assertEqual(len(self.history(controller)), 1)

    def test_two_attempts_of_one_revision_are_retained_even_in_the_same_second(self):
        controller = self.controller()
        for attempt in ('first', 'second'):
            controller.report(self.project, 'failure', 'failure', commit='a' * 40,
                              attempt_id=attempt, finished_at='same-second')
        self.assertEqual(len(self.history(controller)), 2)


class ReleaseRetirementTests(unittest.TestCase):
    """Superseded releases are reclaimed, and only after their work has finished."""

    LISTING = ('lokvetia-release-core-new\trunning\t2026-09-12 15:02:21 +0200 CEST\n'
               'lokvetia-release-core-busy\trunning\t2026-09-11 09:00:00 +0200 CEST\n'
               'lokvetia-release-core-quiet\trunning\t2026-09-10 09:00:00 +0200 CEST\n'
               'lokvetia-release-core-stopped\texited\t2026-09-09 09:00:00 +0200 CEST\n'
               'lokvetia-release-core-previous\trunning\t2026-09-08 09:00:00 +0200 CEST\n')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.project = {'id': 'core', 'name': 'Lokvetia Core', 'service': 'lokvetia',
                        'repository': 'HappyMiha/Lokvetia-Core', 'host': 'test.lokvetia.com'}
        self.controller = deploy.Controller({'state_root': str(self.root), 'runtime_bundle': str(self.root),
                                             'projects': [self.project], 'keep_releases': 2})
        deploy.atomic_json(self.controller.routes_path,
                           {'test.lokvetia.com': {'container': 'lokvetia-release-core-new'}})
        self.controller.status['projects']['core'] = {
            'previous_route': {'container': 'lokvetia-release-core-previous'}}
        self.connections = {'lokvetia-release-core-busy': '4', 'lokvetia-release-core-quiet': '0'}
        self.removed = []

    def docker(self, args, **kwargs):
        if args[:3] == ['docker', 'ps', '-a']:
            return self.LISTING
        if args[:2] == ['docker', 'exec']:
            return self.connections[args[2]]
        if args[:3] == ['docker', 'rm', '-f']:
            self.removed.append(args[3])
            return ''
        raise AssertionError(args)

    def retire(self):
        with patch.object(deploy, 'command', side_effect=self.docker):
            return self.controller.retire(self.project, self.controller.protected(self.project))

    def test_the_newest_releases_and_the_routed_one_are_kept(self):
        self.retire()
        self.assertNotIn('lokvetia-release-core-new', self.removed)
        self.assertNotIn('lokvetia-release-core-busy', self.removed)

    def test_a_release_still_serving_a_call_is_not_cut_off(self):
        self.connections['lokvetia-release-core-quiet'] = '2'
        self.assertNotIn('lokvetia-release-core-quiet', self.retire())

    def test_a_drained_release_is_reclaimed(self):
        self.assertIn('lokvetia-release-core-quiet', self.retire())

    def test_a_stopped_release_needs_no_probe(self):
        self.assertIn('lokvetia-release-core-stopped', self.retire())

    def test_the_rollback_target_is_never_reclaimed(self):
        self.assertNotIn('lokvetia-release-core-previous', self.retire())

    def test_housekeeping_never_fails_a_finished_rollout(self):
        with patch.object(deploy, 'command', side_effect=deploy.DeployError('docker daemon is busy')):
            self.assertEqual(self.controller.retire(self.project, set()), [])

    def test_an_unreachable_container_is_treated_as_busy(self):
        with patch.object(deploy, 'command', side_effect=deploy.DeployError('container is gone')):
            self.assertFalse(self.controller.idle('lokvetia-release-core-quiet'))

    def test_a_removal_that_fails_does_not_stop_the_rest(self):
        def docker(args, **kwargs):
            if args[:3] == ['docker', 'rm', '-f'] and args[3] == 'lokvetia-release-core-quiet':
                raise deploy.DeployError('removal in progress')
            return self.docker(args, **kwargs)
        with patch.object(deploy, 'command', side_effect=docker):
            retired = self.controller.retire(self.project, self.controller.protected(self.project))
        self.assertEqual(retired, ['lokvetia-release-core-stopped'])


class StaleControllerTests(unittest.TestCase):
    """A machine running older controller files says so, instead of blaming the release.

    Both failures a person sees on the deployment page - a schema that "would
    change" and a retained-release limit - are produced by the controller itself.
    When the controller predates the revision it is rolling out, neither message
    is about the release, and nothing on the page used to say that.
    """

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / 'state'
        self.checkout = Path(self.folder.name) / 'checkout'
        for source in deploy.INSTALLED_FROM:
            path = self.checkout / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('current ' + source)
        self.install('current ')

    def install(self, prefix):
        for source, installed in deploy.INSTALLED_FROM.items():
            path = self.root / installed
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prefix + source)

    def controller(self):
        return deploy.Controller({'state_root': str(self.root), 'runtime_bundle': str(self.root),
                                  'projects': [], 'keep_releases': 2})

    def test_an_installation_that_matches_the_revision_is_not_behind(self):
        self.assertEqual(deploy.behind_checkout(self.root, self.checkout), [])

    def test_every_file_the_revision_moved_past_is_named(self):
        (self.root / 'runtime' / 'snapshot.py').write_text('the version before per-object signatures')
        self.assertEqual(deploy.behind_checkout(self.root, self.checkout),
                         ['ops/test-deploy/snapshot.py'])

    def test_a_file_that_was_never_installed_counts_as_behind(self):
        (self.root / 'controller.py').unlink()
        self.assertIn('scripts/autodeploy.py', deploy.behind_checkout(self.root, self.checkout))

    def test_a_file_the_revision_does_not_have_is_not_a_difference(self):
        # A checkout older than this controller is not the controller's problem to
        # report: only files the revision actually carries are compared.
        (self.checkout / 'ops/test-deploy/gateway.py').unlink()
        (self.root / 'runtime' / 'gateway.py').write_text('anything else')
        self.assertEqual(deploy.behind_checkout(self.root, self.checkout), [])

    def test_windows_line_endings_are_not_a_difference(self):
        (self.root / 'runtime' / 'serve.py').write_text(
            'current ops/test-deploy/serve.py'.replace('\n', '\r\n'))
        (self.checkout / 'ops/test-deploy/serve.py').write_bytes(b'current ops/test-deploy/serve.py')
        self.assertNotIn('ops/test-deploy/serve.py', deploy.behind_checkout(self.root, self.checkout))

    def test_the_installed_revision_is_published_for_the_page(self):
        self.root.mkdir(parents=True, exist_ok=True)
        deploy.atomic_json(self.root / 'installed.json',
                           {'revision': 'a' * 40, 'installed_at': '2026-09-12T19:00:00Z'})
        self.assertEqual(self.controller().status['controller']['revision'], 'a' * 40)

    def test_an_installation_that_recorded_nothing_still_publishes_a_record(self):
        self.assertEqual(self.controller().status['controller'],
                         {'revision': '', 'installed_at': '', 'behind': []})

    def test_the_difference_reaches_the_published_state(self):
        controller = self.controller()
        self.install('older ')
        controller.note_controller(self.checkout)
        published = deploy.read_json(controller.status_path)
        self.assertEqual(published['controller']['behind'], sorted(deploy.INSTALLED_FROM))

    def test_a_failure_on_a_stale_controller_says_which_files_are_old(self):
        controller = self.controller()
        controller.status['controller']['behind'] = ['ops/test-deploy/snapshot.py']
        self.assertIn('older than the revision it is rolling out', controller.stale_note())
        self.assertIn('ops/test-deploy/snapshot.py', controller.stale_note())
        self.assertIn('Install.ps1', controller.stale_note())

    def test_a_failure_on_a_current_controller_adds_nothing(self):
        self.assertEqual(self.controller().stale_note(), '')


class InstallerTests(unittest.TestCase):
    """The installer has to be able to update a machine, not only set one up."""

    SCRIPT = (ROOT / 'ops' / 'test-deploy' / 'Install.ps1').read_text(encoding='utf-8')

    def test_an_existing_configuration_is_reconciled_rather_than_skipped(self):
        # The bug this fixes: a re-install used to leave an existing config.json
        # untouched, so a key a newer release needs never arrived on a machine
        # that had already been installed once.
        self.assertIn('$existing = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json', self.SCRIPT)
        self.assertIn('keep_releases', self.SCRIPT)
        self.assertIn("Copy-Item -LiteralPath $configPath -Destination \"$configPath.previous\"", self.SCRIPT)

    def test_a_value_the_operator_set_is_never_overwritten(self):
        self.assertIn('if ($null -eq $existing.PSObject.Properties[$name])', self.SCRIPT)
        self.assertIn("if ($null -eq $project.environment.PSObject.Properties[$key])", self.SCRIPT)

    def test_the_running_controller_is_stopped_before_its_files_are_replaced(self):
        stop = self.SCRIPT.index('Stop-ScheduledTask')
        self.assertLess(stop, self.SCRIPT.index('Copy-Item -LiteralPath "$source/scripts/autodeploy.py"'))
        self.assertIn('did not stop', self.SCRIPT)

    def test_the_controller_is_started_again_even_when_only_configuring(self):
        configure = self.SCRIPT.index('if ($ConfigureOnly)')
        self.assertIn('Start-ScheduledTask', self.SCRIPT[configure:configure + 300])

    def test_the_installed_revision_is_recorded_for_the_controller_to_publish(self):
        self.assertIn('installed.json', self.SCRIPT)
        self.assertIn('rev-parse HEAD', self.SCRIPT)

    def test_every_copied_file_is_one_the_controller_compares(self):
        # If the installer starts copying a file the drift check does not know
        # about, a stale copy of it would go unreported.
        for source in deploy.INSTALLED_FROM:
            self.assertIn(Path(source).name, self.SCRIPT, source)
