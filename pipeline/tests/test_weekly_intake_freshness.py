"""Mutable live inputs need a release window and a changed validated baseline."""
from copy import deepcopy
from datetime import date, datetime, timezone
import os
import json
import unittest
from unittest.mock import Mock, patch

from foldarium_pipeline.intake import WWPDB_NONPOLYMER_URL, WeeklyPolicy
from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublicationError
from foldarium_pipeline.weekly import build_public_weekly_plan, collect_wwpdb_inputs, deployment_weekly_hook
from foldarium_pipeline.weekly_intake_recovery import live_intake_window_block
from foldarium_pipeline.weekly_reconciliation import Gates, plan_reconciliation
from foldarium_pipeline.weekly_reconciliation_store import reconcile_weekly
from test_weekly import sources
from test_weekly_reconciliation import MemoryStore
from test_supabase import FakeResponse

RELEASE = date(2026, 8, 8)
OPEN = datetime(2026, 8, 8, 3, tzinfo=timezone.utc)
CLOSE = datetime(2026, 8, 12, tzinfo=timezone.utc)


class FreshIntakeTests(unittest.TestCase):
    def setUp(self):
        self.data = sources()
        self.inputs = collect_wwpdb_inputs(RELEASE, fetcher=self.data.__getitem__)
        self.coordinator = Mock()
        self.coordinator.weekly_campaign_exists.return_value = False
        self.coordinator.latest_prior_prerelease_snapshot.return_value = {
            'release_date': '2026-08-01', 'sequence_sha256': 'a' * 64,
            'nonpolymer_sha256': 'b' * 64,
        }
        self.coordinator.register_weekly_plan.return_value = {'status': 'registered'}

    def hook(self, now=OPEN, register=True, finish=None):
        with patch.dict(os.environ, {'FOLDARIUM_RELEASE_DATE': RELEASE.isoformat(),
                'FOLDARIUM_WEEKLY_REGISTER': str(int(register)), 'FOLDARIUM_WEEKLY_MAX_TARGETS': '1'}), \
                patch('foldarium_pipeline.weekly.datetime') as clock, \
                patch('foldarium_pipeline.weekly.SupabaseCoordinator.from_env', return_value=self.coordinator), \
                patch('foldarium_pipeline.weekly.collect_wwpdb_inputs', return_value=self.inputs) as fetch:
            clock.now.side_effect = [now, finish or now]
            result = deployment_weekly_hook()
            self.assertIn(clock.now.call_count, (1, 2))
            return result, fetch.call_count

    def test_exact_window_boundaries_and_planner(self):
        for now, reason in ((datetime(2026, 8, 8, 2, 59, 59, tzinfo=timezone.utc), 'intake-before-prerelease-window'),
                            (OPEN, None), (datetime(2026, 8, 11, 23, 59, 59, tzinfo=timezone.utc), None),
                            (CLOSE, 'intake-prerelease-window-closed')):
            self.assertEqual(live_intake_window_block(RELEASE, now=now), reason)
            plan = plan_reconciliation({'rounds': [], 'campaigns': []}, now=now, gates=Gates(intake=True),
                preview_version='v4', production_suffix='beta-v2')
            self.assertEqual(any(a['kind'] == 'intake' for a in plan['actions']), reason is None)
            if reason:
                self.assertIn(reason, [b['reason'] for b in plan['blocked']])

    def test_stale_manual_or_outbox_intent_cannot_bypass_live_window(self):
        for now in (datetime(2026, 8, 8, tzinfo=timezone.utc), CLOSE,
                    datetime(2026, 9, 1, tzinfo=timezone.utc)):
            result, fetched = self.hook(now)
            self.assertEqual(result['status'], 'waiting-for-inputs')
            self.assertEqual(fetched, 0)
        self.coordinator.latest_prior_prerelease_snapshot.assert_not_called()
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_registered_campaign_fastpath_survives_late_apply_and_dry_run(self):
        self.coordinator.weekly_campaign_exists.return_value = True
        for register in (False, True):
            result, fetched = self.hook(CLOSE, register)
            self.assertEqual(result['status'], 'already-registered')
            self.assertEqual(fetched, 0)
        self.coordinator.latest_prior_prerelease_snapshot.assert_not_called()
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_missing_prior_baseline_blocks_without_fetch(self):
        self.coordinator.latest_prior_prerelease_snapshot.return_value = None
        result, fetched = self.hook()
        self.assertEqual(result['reason'], 'prior-prerelease-baseline-unavailable')
        self.assertEqual(fetched, 0)
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_invalid_prior_baseline_does_not_fall_back_or_fetch(self):
        self.coordinator.latest_prior_prerelease_snapshot.side_effect = SupabasePublicationError('inconsistent prior SHA')
        with self.assertRaisesRegex(SupabasePublicationError, 'inconsistent prior SHA'):
            self.hook()
        self.coordinator.latest_prior_prerelease_snapshot.assert_called_once()
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_older_baseline_cannot_attest_a_missing_immediately_previous_week(self):
        self.coordinator.latest_prior_prerelease_snapshot.return_value['release_date'] = '2026-07-25'
        result, fetched = self.hook()
        self.assertEqual(result['reason'], 'immediate-prior-prerelease-baseline-unavailable')
        self.assertEqual(fetched, 0)
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_either_unchanged_input_blocks_stale_and_mixed_rollovers(self):
        for unchanged in (('sequence',), ('nonpolymer',), ('sequence', 'nonpolymer')):
            prior = {'release_date': '2026-08-01', 'sequence_sha256': 'a' * 64, 'nonpolymer_sha256': 'b' * 64}
            for name in unchanged:
                prior[f'{name}_sha256'] = self.inputs['snapshot'][f'{name}_sha256']
            self.coordinator.latest_prior_prerelease_snapshot.return_value = prior
            result, fetched = self.hook()
            self.assertEqual(result['reason'], 'prior-prerelease-source-unchanged')
            self.assertEqual(result['availability']['unchanged_sources'], list(unchanged))
            self.assertEqual(fetched, 1)
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_changed_pair_registers_and_sunday_catchup_uses_explicit_saturday(self):
        result, fetched = self.hook(datetime(2026, 8, 9, tzinfo=timezone.utc))
        self.assertEqual(len(result['tasks']), 2)
        self.assertEqual(fetched, 1)
        self.coordinator.register_weekly_plan.assert_called_once()
        plan = self.coordinator.register_weekly_plan.call_args.args[0]
        self.assertEqual(plan['campaign']['release_date'], RELEASE.isoformat())
        self.assertEqual(len(plan['tasks']), 2)

    def test_dry_run_validates_sources_without_registration(self):
        result, fetched = self.hook(register=False)
        self.assertEqual(len(result['tasks']), 2)
        self.assertEqual(fetched, 1)
        self.coordinator.latest_prior_prerelease_snapshot.assert_called_once()
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_acquisition_crossing_close_waits_unless_already_registered(self):
        result, fetched = self.hook(OPEN, finish=CLOSE)
        self.assertEqual(result['reason'], 'intake-prerelease-window-closed')
        self.assertEqual(fetched, 1)
        self.coordinator.register_weekly_plan.assert_not_called()
        self.coordinator.weekly_campaign_exists.side_effect = [False, True]
        result, _ = self.hook(OPEN, finish=CLOSE)
        self.assertEqual(result['status'], 'already-registered')
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_authoritative_prior_helper_checks_date_identity_hashes_and_counts(self):
        row = {'snapshot_id': 'fixture-snapshot', 'campaign_id': 'wwpdb-2026-08-01',
            'release_date': '2026-08-01', 'created_at': '2026-08-01T03:15:00Z',
            'files': {'wwpdb_sequence': {'sha256': 'a' * 64}, 'wwpdb_nonpolymer': {'sha256': 'b' * 64}},
            'metadata': {'sequence_sha256': 'a' * 64, 'nonpolymer_sha256': 'b' * 64,
                'sequence_rows': 1, 'nonpolymer_rows': 1}}
        def coordinator(value):
            def read(request, *, timeout):
                self.assertEqual(request.get_method(), 'GET')
                self.assertIn('release_date=lt.2026-08-08', request.full_url)
                return FakeResponse(json.dumps([value]).encode())
            return SupabaseCoordinator('https://fixture.supabase.test', 'synthetic', 'fixture', opener=read)
        self.assertEqual(coordinator(row).latest_prior_prerelease_snapshot('2026-08-08')['sequence_sha256'], 'a' * 64)
        for mutate in (lambda r:r.update(campaign_id='manual-unrelated'),
                       lambda r:r.update(campaign_id='wwpdb-2026-08-02', release_date='2026-08-02'),
                       lambda r:r['metadata'].update(sequence_sha256='c' * 64),
                       lambda r:r['metadata'].update(nonpolymer_rows=True),
                       lambda r:r.update(release_date='2026-08-08')):
            invalid = deepcopy(row); mutate(invalid)
            with self.assertRaises(SupabasePublicationError):
                coordinator(invalid).latest_prior_prerelease_snapshot('2026-08-08')

    def test_zero_eligible_targets_wait_without_spending_outbox_failure_budget(self):
        data = deepcopy(self.data)
        data[WWPDB_NONPOLYMER_URL] = data[WWPDB_NONPOLYMER_URL].replace(b'CCCCCCCCCCCCCCCC', b'CC')
        self.inputs = collect_wwpdb_inputs(RELEASE, fetcher=data.__getitem__)
        state = {'rounds': [], 'campaigns': []}
        store = MemoryStore(state)
        for _ in range(6):
            result = reconcile_weekly(store, lambda _: self.hook()[0], gates=Gates(intake=True),
                preview_version='v4', production_suffix='beta-v2', apply=True, clock=lambda: OPEN)
            self.assertEqual(result['executed'][0]['outcome'], 'waiting')
        self.assertEqual(store.finished, [('waiting', None)] * 6)
        self.coordinator.register_weekly_plan.assert_not_called()

    def test_saved_byte_plan_replay_remains_independent_of_live_window(self):
        # Pure explicit source replay cannot acquire any hidden date authority.
        for release in (RELEASE, date(2026, 8, 15)):
            plan, inputs = build_public_weekly_plan(release, fetcher=self.data.__getitem__,
                policy=WeeklyPolicy(max_targets=1))
            self.assertEqual(plan['campaign']['release_date'], release.isoformat())
            self.assertEqual(inputs['snapshot'], self.inputs['snapshot'])
