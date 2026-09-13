"""Native CLI integration and approval-before-secret access."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from agent_factory.models import Agent, ExecutionApproval, WorkItem
from agent_factory.providers import CLIProvider


class ConnectedProviderTests(unittest.TestCase):
    def test_key_access_requires_approval_and_output_is_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            provider=CLIProvider('gemini',sys.executable,
                ['-c','import os;print(os.environ["GEMINI_API_KEY"])'],
                allow_execution=True,workspace=Path(folder))
            agent=Agent('worker','Worker','Implementation Worker',True,'gemini','Return evidence',[])
            agent.model='google:gemini'
            item=WorkItem('Task','Description',1,id=7)
            with patch('agent_factory.ai_setup.connected_environment',return_value={'GEMINI_API_KEY':'synthetic-private-key'}) as resolve:
                self.assertFalse(provider.execute(agent,item,{}).ok)
                resolve.assert_not_called()
                result=provider.execute(agent,item,{},ExecutionApproval(1,'gemini','worker',7,approved_by='owner'))
                self.assertTrue(result.ok,result.error)
                self.assertNotIn('synthetic-private-key',str(result))
                self.assertIn('[REDACTED]',result.content)
                resolve.assert_called_once_with(Path(folder).resolve(),'owner')
                self.assertIsNone(result.metadata['effective_model'])
            with patch('agent_factory.ai_setup.connected_environment',side_effect=RuntimeError('private-store-detail')):
                result=provider.execute(agent,item,{},ExecutionApproval(2,'gemini','worker',7,approved_by='owner'))
                self.assertFalse(result.ok);self.assertTrue(result.metadata['blocked'])
                self.assertNotIn('private-store-detail',str(result))

    def test_official_node_entrypoint_used_for_health_and_preserves_custom_native(self):
        with tempfile.TemporaryDirectory() as folder:
            provider=CLIProvider('gemini','gemini',[],workspace=Path(folder))
            selected=[sys.executable,str(Path(folder)/'official-gemini.js')]
            with patch('agent_factory.providers.shutil.which',return_value=None),patch('agent_factory.ai_setup.launcher',return_value=selected):
                self.assertEqual(provider._executable_path(),sys.executable)
                self.assertEqual(provider._launch_command(sys.executable,['--version']),selected+['--version'])
                custom=CLIProvider('gemini',sys.executable,[],workspace=Path(folder))
                self.assertEqual(custom._launch_command(sys.executable,['--version']),[sys.executable,'--version'])


if __name__=='__main__':unittest.main()
