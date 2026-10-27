"""Preference integration preserves old assignments and full scientific inputs."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.druglike_selection import DRUGLIKE_PREFERENCE_POLICY
from foldarium_pipeline.quiz import manifest_sha256
from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublicationError
from foldarium_pipeline.weekly_question_selection import freeze_weekly_featured_questions, WeeklyQuestionSelectionError
import test_weekly_question_selection as fixture


class DruglikeFeaturedFreezeTests(unittest.TestCase):
    def coordinator(self):
        coordinator, source = fixture.FeaturedFreezeAdapterTests().coordinator()
        source['environment'] = 'production'
        coordinator.weekly_featured_question_selection.return_value = None
        return coordinator, source

    def assessment(self, source, ids, status='preferred'):
        return {'policy': DRUGLIKE_PREFERENCE_POLICY, 'rdkit_version': '2025.03.6',
                'status': status, 'eligible_item_ids': ids, 'preferred_count': len(ids) if status == 'preferred' else 0,
                'source_blind_manifest_sha256': source['blind_manifest_sha256'],
                'source_private_index_sha256': source['metadata']['private_index']['sha256'],
                'descriptor_rows': [{'item_id': item['id'], 'preferred': item['id'] in ids}
                                    for item in source['blind_manifest']['items']],
                'fallback_reason': None if status == 'preferred' else 'no-preferred-ligands'}

    def freeze(self, coordinator, source, **kwargs):
        return freeze_weekly_featured_questions(coordinator, source['round_id'],
            preference_policy=DRUGLIKE_PREFERENCE_POLICY,
            expected_blind_manifest_sha256=source['blind_manifest_sha256'],
            expected_environment='production', **kwargs)

    def test_new_preferred_draw_has_fewer_questions_instead_of_junk_padding(self):
        coordinator, source = self.coordinator()
        before = deepcopy(source)
        ids = [item['id'] for item in source['blind_manifest']['items'][:2]]
        assessment = self.assessment(source, ids)
        with patch('foldarium_pipeline.druglike_selection.assess_druglike_candidates', return_value=assessment):
            preview = self.freeze(coordinator, source, dry_run=True)
            result = self.freeze(coordinator, source)
        marker = result['featured_questions']
        self.assertEqual(marker['source_item_count'], 12)
        self.assertEqual(marker['candidate_count'], 2)
        self.assertEqual(marker['selected_question_count'], 2)
        self.assertEqual(marker['item_ids'], ids)
        self.assertEqual(result['ligand_preference_status'], 'preferred')
        self.assertEqual(result['preferred_candidate_count'], 2)
        self.assertEqual(source, before)
        self.assertEqual(preview['audit']['ligand_preference'], assessment)
        content, _ = coordinator.store_bytes.call_args.args
        self.assertEqual(json.loads(content)['ligand_preference'], assessment)
        self.assertEqual(hashlib.sha256(content).hexdigest(), marker['selection_sha256'])
        legacy = freeze_weekly_featured_questions(coordinator, source['round_id'], dry_run=True)
        self.assertNotEqual(legacy['featured_questions']['selection_sha256'], marker['selection_sha256'])

    def test_zero_preferred_uses_explicit_audited_legacy_fallback(self):
        coordinator, source = self.coordinator()
        ids = [item['id'] for item in source['blind_manifest']['items']]
        assessment = self.assessment(source, ids, 'legacy-fallback-no-preferred')
        with patch('foldarium_pipeline.druglike_selection.assess_druglike_candidates', return_value=assessment):
            result = self.freeze(coordinator, source, dry_run=True)
        self.assertEqual(result['featured_questions']['selected_question_count'], 5)
        self.assertEqual(result['featured_questions']['candidate_count'], 12)
        self.assertEqual(result['audit']['ligand_preference']['status'], 'legacy-fallback-no-preferred')
        coordinator.store_bytes.assert_not_called()
        coordinator.register_weekly_featured_questions.assert_not_called()

    def test_lost_ack_or_policy_upgrade_preserves_old_frozen_marker_before_private_reads(self):
        coordinator, source = self.coordinator()
        old = freeze_weekly_featured_questions(coordinator, source['round_id'])['featured_questions']
        coordinator.weekly_featured_question_selection.return_value = {
            'round_id': source['round_id'], 'environment': 'production',
            'blind_manifest_sha256': source['blind_manifest_sha256'],
            'item_count': 12, 'blind_manifest': source['blind_manifest'], 'featured_questions': old}
        coordinator.weekly_quiz_reveal_inputs.reset_mock()
        coordinator.weekly_quiz_reveal_inputs.side_effect = AssertionError('old assignment must not reopen private science')
        coordinator.store_bytes.reset_mock(); coordinator.register_weekly_featured_questions.reset_mock()
        with patch('foldarium_pipeline.druglike_selection.assess_druglike_candidates') as assess:
            result = self.freeze(coordinator, source)
        self.assertEqual(result['status'], 'already-registered')
        self.assertTrue(result['preserved_existing'])
        self.assertEqual(result['featured_questions'], old)
        assess.assert_not_called();coordinator.weekly_quiz_reveal_inputs.assert_not_called()
        coordinator.store_bytes.assert_not_called();coordinator.register_weekly_featured_questions.assert_not_called()

    def test_new_or_preserved_draw_rejects_stale_remote_action_bindings(self):
        for existing in (False, True):
            for mutation in ('manifest', 'environment'):
                coordinator, source = self.coordinator()
                if existing:
                    marker = freeze_weekly_featured_questions(coordinator, source['round_id'])['featured_questions']
                    coordinator.weekly_featured_question_selection.return_value = {
                        **source, 'item_count': 12, 'featured_questions': marker}
                coordinator.store_bytes.reset_mock();coordinator.register_weekly_featured_questions.reset_mock()
                digest = 'f'*64 if mutation == 'manifest' else source['blind_manifest_sha256']
                environment = 'preview' if mutation == 'environment' else 'production'
                with self.subTest(existing=existing, mutation=mutation), self.assertRaisesRegex(WeeklyQuestionSelectionError, 'source binding changed'):
                    freeze_weekly_featured_questions(coordinator, source['round_id'], preference_policy=DRUGLIKE_PREFERENCE_POLICY,
                        expected_blind_manifest_sha256=digest, expected_environment=environment)
                coordinator.store_bytes.assert_not_called();coordinator.register_weekly_featured_questions.assert_not_called()

    def test_chemistry_assessment_must_bind_exact_private_and_blind_source(self):
        for field in ('source_blind_manifest_sha256', 'source_private_index_sha256', 'policy'):
            coordinator, source = self.coordinator()
            assessment = self.assessment(source, [source['blind_manifest']['items'][0]['id']])
            assessment[field] = 'wrong'
            with patch('foldarium_pipeline.druglike_selection.assess_druglike_candidates', return_value=assessment), self.assertRaisesRegex(WeeklyQuestionSelectionError, 'preference differs'):
                self.freeze(coordinator, source)
            coordinator.store_bytes.assert_not_called();coordinator.register_weekly_featured_questions.assert_not_called()

    def test_registered_marker_must_match_its_original_population(self):
        coordinator, source = self.coordinator()
        marker = freeze_weekly_featured_questions(coordinator, source['round_id'])['featured_questions']
        row = {**source, 'item_count': 12, 'featured_questions': marker}
        for mutate in (lambda r:r['featured_questions']['item_ids'].__setitem__(0, 'unknown'),
                       lambda r:r['featured_questions'].__setitem__('selection_sha256', 'bad'),
                       lambda r:r['featured_questions'].__setitem__('candidate_count', True)):
            bad=deepcopy(row);mutate(bad);coordinator.weekly_featured_question_selection.return_value=bad
            with self.assertRaises(WeeklyQuestionSelectionError):self.freeze(coordinator, source)

    def test_public_marker_lookup_is_exact_and_never_requests_answer_fields(self):
        coordinator = SupabaseCoordinator('https://fixture.supabase.co', 'fixture-key', 'private')
        row = {'round_id':'weekly-exact','featured_questions':{'selection_sha256':'a'*64}}
        with patch.object(SupabaseCoordinator, '_get_json_rows', return_value=[row]) as query:
            self.assertEqual(coordinator.weekly_featured_question_selection('weekly-exact'), row)
        endpoint, operation = query.call_args.args
        self.assertIn('/public_weekly_quiz_rounds?', endpoint)
        self.assertIn('round_id=eq.weekly-exact', endpoint)
        self.assertNotIn('reveal_manifest', endpoint);self.assertNotIn('private_index', endpoint)
        with patch.object(SupabaseCoordinator, '_get_json_rows', return_value=[]):
            self.assertIsNone(coordinator.weekly_featured_question_selection('weekly-exact'))
        for rows in ([row,row], [{'round_id':'wrong','featured_questions':{}}], [{'round_id':'weekly-exact','featured_questions':'bad'}]):
            with patch.object(SupabaseCoordinator, '_get_json_rows', return_value=rows), self.assertRaises(SupabasePublicationError):
                coordinator.weekly_featured_question_selection('weekly-exact')


if __name__ == '__main__': unittest.main()
