"""Boundary tests use fake tools; live EXE evidence is recorded separately."""
from contextlib import ExitStack, closing
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_factory import godot_prototype as cycle
from agent_factory.godot_engine import BuildArtifact, EngineRun
from agent_factory.playable_versions import PlayableVersions
from agent_factory.storage import SQLiteStorage


SPEC = {'title': 'Sky Steps', 'max_jumps': 1, 'speed': 260, 'jump_velocity': 520, 'unsupported': []}


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'game'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cycle, 'require_a_build_machine',
            return_value=SimpleNamespace(is_the_users_computer=True)))
        # Patch just the platform check, not pathlib's host OS selection.
        self.stack.enter_context(patch.object(cycle, 'os', SimpleNamespace(name='nt')))
        self.infer = self.stack.enter_context(patch.object(cycle, 'infer', return_value=(dict(SPEC), {'fake': True})))
        self.stack.enter_context(patch.object(cycle, 'commit_source', return_value='a' * 40))
        self.build = self.stack.enter_context(patch.object(cycle.GodotAdapter, 'build', side_effect=self.fake_build))
        self.verify = self.stack.enter_context(patch.object(cycle, 'verify_exe', side_effect=self.fake_verify))

    def fake_build(self, source, **args):
        args['output'].write_bytes((source / 'scripts/player.gd').read_bytes())
        run = EngineRun('headless_smoke', 'succeeded', 0, 1, (), 'd' * 64, 'test-only', '', '', '')
        return BuildArtifact(args['project_digest'], args['source_commit'], '4.7.2',
            args['template_id'], args['template_version'], args['preset'], str(args['output']),
            cycle.checksum(args['output']), args['output'].stat().st_size, (run,), '2026-01-01')

    def fake_verify(self, exe, evidence, expected):
        evidence.mkdir()
        result = {'checks': {key: True for key in cycle.CHECKS}}
        cycle.save(evidence / 'runtime.json', result)
        (evidence / 'game.png').write_bytes(b'test-only')
        return result

    def run_cycle(self, command='one', base=None, text='platformer', **kwargs):
        return cycle.run(self.root, request_text=text, base_version=base, command_id=command,
            model='qwen2.5-coder:7b', godot='godot.exe', execute_local=True, **kwargs)

    def current(self):
        with closing(SQLiteStorage(self.root / 'versions.db')) as storage:
            return PlayableVersions(storage).current('prototype')

    def test_opt_in_denied_before_any_effect(self):
        with self.assertRaises(PermissionError):
            cycle.run(self.root, request_text='platformer', base_version=None, command_id='one',
                model='qwen2.5-coder:7b', godot='godot.exe')
        self.assertFalse(self.root.exists())
        self.infer.assert_not_called()

    def test_two_versions_and_exact_replay(self):
        first = self.run_cycle()
        self.infer.return_value = ({**SPEC, 'max_jumps': 2}, {'fake': True})
        second = self.run_cycle('two', first['version'], 'double jump')
        self.assertNotEqual(first['artifact_sha256'], second['artifact_sha256'])
        self.assertTrue(Path(first['artifact']).is_file())
        self.assertEqual(self.current().version_digest, second['version'])
        self.assertEqual(self.run_cycle('two', first['version'], 'double jump'), second)
        self.assertEqual(self.infer.call_count, 2)
        self.assertIn('graphical_playtest', second['evidence_gap'])

    def test_stale_base_and_command_collision_do_not_infer(self):
        self.run_cycle()
        for command, base, text in [('two', 'b' * 64, 'change'), ('one', None, 'changed request')]:
            with self.assertRaises(ValueError):
                self.run_cycle(command, base, text)
        self.assertEqual(self.infer.call_count, 1)

    def test_failed_edit_keeps_original_playable_and_no_retry(self):
        first = self.run_cycle()
        self.infer.return_value = ({**SPEC, 'max_jumps': 2}, {})
        self.verify.side_effect = TimeoutError('EXE timeout')
        with self.assertRaises(TimeoutError):
            self.run_cycle('two', first['version'], 'double jump')
        self.assertEqual(self.current().version_digest, first['version'])
        with self.assertRaisesRegex(ValueError, 'failed/interrupted'):
            self.run_cycle('two', first['version'], 'double jump')
        self.assertEqual(self.infer.call_count, 2)

    def test_model_timeout_and_noop_do_not_build(self):
        first = self.run_cycle()
        with self.assertRaisesRegex(ValueError, 'no supported change'):
            self.run_cycle('noop', first['version'], 'same')
        self.infer.side_effect = TimeoutError('model timeout')
        with self.assertRaises(TimeoutError):
            self.run_cycle('timeout', first['version'], 'change')
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(self.current().version_digest, first['version'])

    def test_prepared_receipt_recovers_without_inference(self):
        first = self.run_cycle()
        record = self.root / 'revisions' / 'one' / 'result.json'
        cycle.save(record, {**first, 'status': 'prepared'})
        self.assertEqual(self.run_cycle(), first)
        self.assertEqual(self.infer.call_count, 1)

    def test_tampered_exe_denies_replay_and_revision(self):
        first = self.run_cycle()
        Path(first['artifact']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_cycle()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_cycle('two', first['version'])
        self.assertEqual(self.infer.call_count, 1)

    def test_interrupted_attempt_never_reexecutes(self):
        self.infer.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_cycle()
        with self.assertRaisesRegex(ValueError, 'failed/interrupted'):
            self.run_cycle()
        self.assertEqual(self.infer.call_count, 1)

    def test_build_failure_does_not_run_exe(self):
        self.build.side_effect = lambda *a, **kw: replace(self.fake_build(*a, **kw), artifact_checksum=None)
        with self.assertRaisesRegex(ValueError, 'Godot build failed'):
            self.run_cycle()
        self.verify.assert_not_called()
        self.assertIsNone(self.current())

    def test_schema_cannot_inject_code_or_widen_scope(self):
        for bad in [{**SPEC, 'max_jumps': True}, {**SPEC, 'speed': 999},
                    {**SPEC, 'script': 'OS.execute(...)'}, {**SPEC, 'unsupported': ['multiplayer']},
                    {**SPEC, 'jump_velocity': '520; OS.execute(...)'}]:
            with self.assertRaises(ValueError):
                cycle.compile_template(bad)
        title = 'Hello "world" \\ test'
        template = cycle.compile_template({**SPEC, 'title': title})
        line = next(line for line in template.file('project.godot').content.splitlines() if line.startswith('config/name='))
        self.assertEqual(json.loads(line.split('=', 1)[1]), title)


class LocalInferenceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.inventory = self.stack.enter_context(patch.object(cycle, 'model_inventory', return_value='a' * 64))
        self.opener = self.stack.enter_context(patch.object(cycle, 'build_opener')).return_value
        self.response = self.opener.open.return_value.__enter__.return_value
        self.good = {'model': 'qwen2.5-coder:7b', 'done': True, 'eval_count': 44, 'response': json.dumps(SPEC)}

    def infer(self, payload):
        self.response.read.return_value = json.dumps(payload).encode()
        return cycle.infer('platformer', None, 'qwen2.5-coder:7b')

    def test_exact_local_model_and_bounded_request(self):
        spec, record = self.infer(self.good)
        self.assertEqual(spec, SPEC)
        self.assertEqual(record['model_digest'], 'a' * 64)
        args, kwargs = self.opener.open.call_args
        self.assertEqual(args[0].full_url, 'http://127.0.0.1:11434/api/generate')
        self.assertEqual(kwargs['timeout'], 180)
        self.assertEqual(json.loads(args[0].data)['options']['num_predict'], 384)
        self.response.read.assert_called_once_with(32769)

    def test_malformed_truncated_wrong_model_and_identity_drift(self):
        for bad in [[], {**self.good, 'done': False}, {**self.good, 'model': 'remote'},
                    {**self.good, 'eval_count': 384}, {**self.good, 'eval_count': True},
                    {**self.good, 'response': 'not JSON'}, {**self.good, 'response': '{}'}]:
            with self.assertRaises(ValueError):
                self.infer(bad)
        self.inventory.side_effect = ['a' * 64, 'b' * 64]
        with self.assertRaisesRegex(ValueError, 'changed during inference'):
            self.infer(self.good)

    def test_timeout_overflow_and_unsupported_model_never_fallback(self):
        self.opener.open.side_effect = TimeoutError('timeout')
        with self.assertRaises(TimeoutError):
            self.infer(self.good)
        self.assertEqual(self.opener.open.call_count, 1)
        self.opener.open.side_effect = None
        self.response.read.return_value = b'x' * 32769
        with self.assertRaisesRegex(ValueError, 'exceeds bound'):
            cycle.infer('platformer', None, 'qwen2.5-coder:7b')
        with self.assertRaisesRegex(ValueError, 'supported installed'):
            cycle.infer('platformer', None, 'remote:model')


if __name__ == '__main__':
    unittest.main()
