from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from foldarium_pipeline.modal_dispatch import (
    dispatch_prediction, observe_prediction_dispatch,
    PredictionDispatchObservationUncertain, PredictionDispatchTerminalError,
)


class ModalTimeout(Exception):
    pass


class FunctionTimeout(ModalTimeout):
    pass


class OutputExpired(ModalTimeout):
    pass


def node(call_id='fc-old', status='PENDING', children=None):
    return SimpleNamespace(function_call_id=call_id, task_id='ta-fixture', status=SimpleNamespace(name=status), children=children or [])


class DispatchObservationTests(unittest.TestCase):
    def setUp(self):
        self.action={'action_key':'action','parameters':{'campaign_id':'campaign','run_id':'run'}}
        self.row={'run_id':'run','status':'queued','attempt_count':0,'task_payload':{'task_id':'run'}}
        self.private=Mock();self.private.campaign_prediction_run_statuses.return_value=[self.row]
        self.receipt={'provider':'modal','call_id':'fc-old'}
        self.snapshot={'action_history':{'action':{'dispatch_receipt':self.receipt}}}
        self.store=Mock();self.store.snapshot.return_value=self.snapshot
        self.call=Mock();self.call.get.side_effect=TimeoutError('observation deadline')
        self.call.get_call_graph.return_value=[node()]
        self.api=SimpleNamespace(FunctionCall=SimpleNamespace(from_id=Mock(return_value=self.call)),exception=SimpleNamespace(TimeoutError=ModalTimeout,FunctionTimeoutError=FunctionTimeout,OutputExpiredError=OutputExpired))
        self.spawn=Mock(return_value='fc-new')

    def execute(self):
        before=deepcopy(self.snapshot)
        try:
            return dispatch_prediction(self.private,self.store,self.action,modal_api=self.api,spawn=self.spawn)
        finally:
            self.assertEqual(self.snapshot,before,'observation must never erase/replace an acknowledged receipt')

    def test_builtin_poll_timeout_with_pending_exact_node_waits_without_spawn(self):
        self.call.get_call_graph.return_value=[node('fc-ancestor', 'SUCCESS', [node()])]
        self.assertEqual(self.execute(),{'status':'accepted-dispatch-still-pending'})
        self.call.get.assert_called_once_with(timeout=0);self.spawn.assert_not_called()

    def test_expired_output_is_not_pending_or_replacement_authorization(self):
        self.call.get.side_effect=OutputExpired()
        with self.assertRaises(OutputExpired):self.execute()
        self.call.get_call_graph.assert_not_called();self.spawn.assert_not_called()

    def test_terminal_worker_builtin_timeout_is_visible_and_never_respawned(self):
        self.call.get.side_effect=TimeoutError('worker HTTP deadline')
        for status in ('FAILURE','INIT_FAILURE','TERMINATED','SUCCESS','TIMEOUT'):
            with self.subTest(status=status):
                self.call.get_call_graph.return_value=[node(status=status)]
                with self.assertRaises(PredictionDispatchTerminalError):self.execute()
        self.spawn.assert_not_called()

    def test_missing_ambiguous_or_unknown_graph_is_explicit_uncertainty(self):
        for graph in ([],[node('fc-other')],[node(),node()],[node(status='NEW_UNKNOWN')]):
            with self.subTest(graph=graph):
                self.call.get_call_graph.return_value=graph
                with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.spawn.assert_not_called()

    def test_transient_graph_failure_preserves_receipt_without_spawn(self):
        self.call.get_call_graph.side_effect=ConnectionError('transport unavailable')
        with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.spawn.assert_not_called()

    def test_actual_worker_and_other_modal_or_transport_errors_propagate(self):
        for error in (ValueError('worker failed'),RuntimeError('worker failed'),ModalTimeout('other timeout'),ConnectionError('control plane unavailable')):
            with self.subTest(error=type(error)):
                self.call.get.side_effect=error
                with self.assertRaises(type(error)):self.execute()
        self.spawn.assert_not_called();self.call.get_call_graph.assert_not_called()

    def test_finished_call_without_run_claim_is_terminal_error(self):
        self.call.get.side_effect=None;self.call.get.return_value={'status':'done'}
        with self.assertRaises(PredictionDispatchTerminalError):self.execute()
        self.spawn.assert_not_called()

    def test_confirmed_function_timeout_rechecks_run_before_single_replacement(self):
        self.call.get.side_effect=FunctionTimeout()
        self.assertEqual(self.execute(),{'dispatch_receipt':{'provider':'modal','call_id':'fc-new'}})
        self.assertEqual(self.private.campaign_prediction_run_statuses.call_count,2)
        self.spawn.assert_called_once_with(self.row['task_payload']);self.call.get_call_graph.assert_not_called()

    def test_claim_race_after_terminal_timeout_prevents_replacement(self):
        self.call.get.side_effect=FunctionTimeout()
        for state in ({'status':'running','attempt_count':1},{'status':'failed','attempt_count':1},{'status':'queued','attempt_count':1}):
            with self.subTest(state=state):
                self.private.campaign_prediction_run_statuses.side_effect=[[self.row],[{**self.row,**state}]]
                self.assertEqual(self.execute(),{'status':'already-claimed'})
        self.spawn.assert_not_called()

    def test_first_dispatch_and_already_claimed_guard(self):
        self.store.snapshot.return_value={}
        self.assertEqual(self.execute()['dispatch_receipt']['call_id'],'fc-new')
        self.spawn.assert_called_once()
        self.api.FunctionCall.from_id.assert_not_called()
        self.spawn.reset_mock();self.private.campaign_prediction_run_statuses.return_value=[{**self.row,'status':'running','attempt_count':1}]
        self.assertEqual(self.execute(),{'status':'already-claimed'});self.spawn.assert_not_called()

    def test_wrong_provider_is_rejected_without_observing_or_spawning(self):
        self.receipt['provider']='another-provider'
        with self.assertRaisesRegex(ValueError,'provider changed'):self.execute()
        self.api.FunctionCall.from_id.assert_not_called();self.spawn.assert_not_called()
