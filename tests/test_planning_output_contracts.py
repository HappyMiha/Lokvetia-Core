import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from agent_factory.autonomous_planning_pipeline import RuntimePlanningInvoker, PlanningArtifactSchemas
from agent_factory.planning_output_contracts import OUTPUT_SHAPES, EVIDENCE_SHAPES, guidance
from agent_factory.software_roles import AUTONOMOUS_PLANNING_ROLE_IDS


class PlanningPromptTests(unittest.TestCase):
    def test_measured_game_behavior_is_an_observable_acceptance_criterion(self):
        PlanningArtifactSchemas._measurable(
            'After pressing the right arrow key, the character moves 10 pixels to the right.', 'movement')
        PlanningArtifactSchemas._measurable(
            'The game loads and runs without errors on Windows, macOS, and Linux.', 'runtime')
        PlanningArtifactSchemas._measurable(
            'After collecting five coins, the game displays a win screen.', 'win screen')
        PlanningArtifactSchemas._measurable(
            'The game launches successfully on a Windows operating system without any errors.', 'runtime')
        PlanningArtifactSchemas._measurable('The game does not use any paid assets.', 'asset policy')
        PlanningArtifactSchemas._measurable(
            'After collecting five coins, the counter displays the number 5.', 'counter')
        PlanningArtifactSchemas._measurable(
            'The win screen displays after collecting five coins.', 'win screen')
        PlanningArtifactSchemas._measurable(
            "After pressing the up arrow key, the player's vertical position changes upwards", 'position')
        for vague in ('The player moves smoothly.', 'The game should feel good.', 'The player collects coins.',
                      'The game shows beautiful graphics.'):
            with self.assertRaises(ValueError):
                PlanningArtifactSchemas._measurable(vague, 'vague')

    def test_runtime_prompt_includes_the_nested_contract_before_the_first_call(self):
        runtime = Mock()
        request = SimpleNamespace(
            assignment=SimpleNamespace(logical_agent_id='planner', role_id='mission_analyst',
                provider_id='ollama', model='local:qwen2.5-coder:7b', permissions=(), limits={}),
            context=SimpleNamespace(id=1, document={'source': 'Exact supplied idea'}),
            mission_id=1, validation_feedback=(), authorization=object())
        RuntimePlanningInvoker(runtime).invoke(request)
        agent, item, context, authority = runtime.run.call_args.args
        for field in ('summary', 'outcomes', 'constraints', 'ambiguities', 'source_references'):
            self.assertIn('"'+field+'"', agent.instructions)
        self.assertEqual(context, request.context.document)
        self.assertIs(authority, request.authorization)
        self.assertFalse(runtime.run.call_args.kwargs['allow_fallback'])

    def test_every_planning_role_has_a_nested_contract(self):
        self.assertEqual(set(OUTPUT_SHAPES), set(AUTONOMOUS_PLANNING_ROLE_IDS))
        self.assertEqual(set(EVIDENCE_SHAPES), set(AUTONOMOUS_PLANNING_ROLE_IDS))
        for role in AUTONOMOUS_PLANNING_ROLE_IDS:
            self.assertIn('separate', guidance(role))

    def test_evidence_preserves_the_nested_types_required_by_the_validator(self):
        self.assertIsInstance(EVIDENCE_SHAPES['mission_analyst'], dict)
        self.assertTrue(all(isinstance(value, str)
                            for value in EVIDENCE_SHAPES['mission_analyst']['source_trace']))
        self.assertIn('"output":{', guidance('mission_analyst'))
        self.assertIn('"evidence":{"source_trace":[', guidance('mission_analyst'))
        self.assertIn('host computes evidence digest', guidance('mission_analyst'))
        item = OUTPUT_SHAPES['backlog_planner']['backlog_proposal']['items'][0]
        for field in ('validation_method', 'required_components', 'required_infrastructure',
                      'expected_artifacts', 'definition_of_done'):
            self.assertTrue(item[field], field)


if __name__ == '__main__': unittest.main()
