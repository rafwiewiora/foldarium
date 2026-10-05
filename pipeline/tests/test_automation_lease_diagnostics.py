"""Lease diagnostics observe control state without granting retries."""
import unittest
from copy import deepcopy
from test_weekly_reconciliation import ALL, NOW, MemoryStore, snapshot, round_row
from foldarium_pipeline.weekly_reconciliation_store import reconcile_weekly


class LeaseDiagnosticsTests(unittest.TestCase):
    def run_pass(self, store, execute, *, apply):
        return reconcile_weekly(store, execute, gates=ALL, preview_version='v4',
            production_suffix='beta-v2', apply=apply, max_actions=1, clock=lambda: NOW)

    def test_dry_run_exposes_expired_leases_without_mutation(self):
        state = snapshot(round_row())
        state.update(expired_running_actions=[{'action_key': 'a'*64, 'diagnostic': 'expired-lease-unresolved'}],
                     expired_running_actions_count=1, expired_running_actions_truncated=False)
        before = deepcopy(state)
        store = MemoryStore(state)
        result = self.run_pass(store, lambda _: self.fail('executed'), apply=False)
        self.assertEqual(state, before)
        self.assertEqual(result['expired_running_actions'], state['expired_running_actions'])
        self.assertEqual(store.finished, [])

    def test_final_failure_health_is_refreshed_after_execution(self):
        store = MemoryStore(snapshot(round_row()))
        original_finish = store.finish
        def finish(claim, outcome, error=None):
            result = original_finish(claim, outcome, error)
            store.state['failed_actions'] = [{'action_key': claim['action_key'], 'kind': 'fixture'}]
            return result
        store.finish = finish
        def execute(_):
            raise RuntimeError('sensitive detail must not appear')
        result = self.run_pass(store, execute, apply=True)
        self.assertEqual(result['failed_actions'], store.state['failed_actions'])
        self.assertNotIn('sensitive', str(result))
        self.assertEqual(len(store.finished), 1)

    def test_optional_final_observation_error_does_not_retry_completed_work(self):
        store = MemoryStore(snapshot(round_row()))
        original_finish = store.finish
        def unavailable():
            raise RuntimeError('secret observation detail')
        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            store.snapshot = unavailable
            return result
        store.finish = finish
        called = []
        result = self.run_pass(store, called.append, apply=True)
        self.assertEqual(result['health_refresh_error_type'], 'RuntimeError')
        self.assertNotIn('secret', str(result))
        self.assertEqual(len(called), 1)
        self.assertEqual(len(store.finished), 1)
