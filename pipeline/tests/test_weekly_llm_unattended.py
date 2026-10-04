"""No paid calls: exercise crash accounting and exact production runner contract."""
import copy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from foldarium_pipeline.weekly_llm_providers.anthropic_api import (
    ApiBudgetError, ApiConfig, AnthropicApiProvider, MessagesTransport, config_sha256, wire_response_schema, provider_config, initialize_budget_ledger, validate_budget_ledger,
)
from foldarium_pipeline.weekly_llm_unattended import execute_benchmark_job, run_isolated_inference
from foldarium_pipeline.weekly_llm_contract import sha256_hex
from test_weekly_llm_score import _build_kit_zip, _fake_fixture

CONFIG = ApiConfig(model_id="claude-test-20260101", model_input_token_limit=200000,
    max_output_tokens=1000, input_usd_per_million_upper="5", output_usd_per_million_upper="25",
    max_cost_usd="3", timeout_seconds=60, rate_card_valid_until="2099-01-01")
EXECUTION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CAPABILITIES = {"image_input": {"supported": True}, "structured_outputs": {"supported": True}}


class Transport:
    def __init__(self, responses=None):
        self.responses = responses or {"item-1": {"item_id": "item-1"}}
        self.posts = []
        self.metadata = {"id": CONFIG.model_id, "max_input_tokens": 200000, "max_tokens": 1000, "capabilities": CAPABILITIES}
        self.error = None
        self.edit = lambda x: x
        self.before_post = lambda: None

    def __call__(self, method, path, payload=None):
        if method == "GET":
            return self.metadata
        self.before_post()
        self.posts.append(copy.deepcopy(payload))
        if self.error:
            raise self.error
        prompt = payload["messages"][0]["content"][0]["text"]
        item_id = next(key for key in self.responses if key in prompt)
        return self.edit({"id": "message-id", "model": CONFIG.model_id, "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "content": [{"type": "text", "text": json.dumps(self.responses[item_id])}]})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = Transport()
        self.commits = 0
        initialize_budget_ledger(self.root / "budget.json", CONFIG, EXECUTION)

    def provider(self, config=CONFIG, commit=None):
        def persisted():
            self.commits += 1
        return AnthropicApiProvider(config=config, ledger_path=self.root / "budget.json",
            execution_id=EXECUTION, transport=self.transport, durable_commit=commit or persisted)

    def score(self, provider, item="item-1", prompt=None):
        return provider.score_item(item_id=item, prompt_text=prompt or item, image_paths=[], workspace_dir=str(self.root))

    def test_configuration_requires_pinned_model_positive_explicit_rates_and_sufficient_budget(self):
        for values in ({"model_id": "claude-opus-latest"}, {"model_id": "claude-sonnet-4-5"}, {"model_id": "claude-future-99"}, {"max_cost_usd": "0.01"},
                       {"input_usd_per_million_upper": "NaN"}, {"max_output_tokens": True}, {"rate_card_valid_until": "invalid-date"}, {"rate_card_valid_until": "20990101"}):
            with self.subTest(values=values), self.assertRaises(ApiBudgetError):
                replace(CONFIG, **values)
        with self.assertRaises(ApiBudgetError):
            ApiConfig.from_mapping({"model_id": CONFIG.model_id})

    def test_reviewed_dateless_snapshot_is_supported_without_alias_fallback(self):
        config = replace(CONFIG, model_id="claude-sonnet-4-6", model_input_token_limit=1000000, max_cost_usd="10")
        self.assertEqual(config.reserve_micro_usd, 5025000)
        self.transport.metadata = {"id": config.model_id, "max_input_tokens": 1000000, "max_tokens": 128000, "capabilities": CAPABILITIES}
        (self.root / "budget.json").unlink()
        initialize_budget_ledger(self.root / "budget.json", config, EXECUTION)
        self.provider(config).preflight()
        self.assertEqual(self.transport.posts, [])

    def test_reservation_is_durable_before_paid_call_and_settles_conservatively(self):
        def verify():
            ledger = json.loads((self.root / "budget.json").read_text())
            self.assertEqual(ledger["entries"]["item-1"]["cost_micro_usd"], 1025000)
            self.assertEqual(self.commits, 1)
        self.transport.before_post = verify
        provider = self.provider()
        provider.preflight()
        result = self.score(provider)
        self.assertEqual(result.usage.cost_usd, .00175)
        self.assertEqual(self.commits, 2)
        self.assertEqual(result.observed_ids, (CONFIG.model_id,))
        payload = self.transport.posts[0]
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_failed_commit_prevents_paid_call(self):
        def failed():
            raise OSError("commit failed")
        provider = self.provider(commit=failed)
        provider.preflight()
        with self.assertRaises(OSError):
            self.score(provider)
        self.assertEqual(self.transport.posts, [])

    def test_unknown_outcome_retains_full_cost_and_never_retries(self):
        self.transport.error = TimeoutError("unknown server outcome")
        provider = self.provider()
        provider.preflight()
        with self.assertRaises(TimeoutError):
            self.score(provider)
        resumed = self.provider()
        resumed.preflight()
        with self.assertRaisesRegex(ApiBudgetError, "uncertain"):
            self.score(resumed)
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(json.loads((self.root / "budget.json").read_text())["entries"]["item-1"]["cost_micro_usd"], CONFIG.reserve_micro_usd)

    def test_completed_response_replays_but_changed_prompt_is_rejected(self):
        provider = self.provider()
        provider.preflight()
        expected = self.score(provider)
        resumed = self.provider()
        resumed.preflight()
        self.assertEqual(self.score(resumed), expected)
        with self.assertRaisesRegex(ApiBudgetError, "inputs changed"):
            self.score(resumed, prompt="item-1 altered")
        self.assertEqual(len(self.transport.posts), 1)

    def test_expired_rate_card_allows_exact_replay_but_no_new_reservation(self):
        provider = self.provider()
        provider.preflight()
        expected = self.score(provider)
        ledger_before = (self.root / "budget.json").read_bytes()
        with patch("foldarium_pipeline.weekly_llm_providers.anthropic_api.datetime") as clock:
            clock.now.return_value = datetime(2100, 1, 1, tzinfo=timezone.utc)
            restored = ApiConfig.from_mapping(CONFIG.__dict__)
            self.assertEqual(config_sha256(restored), config_sha256(CONFIG))
            resumed = self.provider(restored)
            resumed.preflight()
            self.assertEqual(self.score(resumed), expected)
            with self.assertRaisesRegex(ApiBudgetError, "inputs changed"):
                self.score(resumed, prompt="item-1 changed")
            with self.assertRaisesRegex(ApiBudgetError, "expired"):
                self.score(resumed, item="item-2")
        self.assertEqual((self.root / "budget.json").read_bytes(), ledger_before)
        self.assertEqual(len(self.transport.posts), 1)

    def test_remaining_budget_must_cover_full_next_request(self):
        config = replace(CONFIG, max_cost_usd="1.025")
        (self.root / "budget.json").unlink()
        initialize_budget_ledger(self.root / "budget.json", config, EXECUTION)
        provider = self.provider(config)
        provider.preflight()
        self.score(provider)
        with self.assertRaisesRegex(ApiBudgetError, "budget"):
            self.score(provider, item="item-2")
        self.assertEqual(len(self.transport.posts), 1)

    def test_model_alias_or_missing_context_limit_fails_before_spend(self):
        for metadata in ({"id": "different"}, {"id": CONFIG.model_id},
                         {"id": CONFIG.model_id, "max_input_tokens": 1000000, "max_tokens": 1000}):
            self.transport.metadata = metadata
            with self.subTest(metadata=metadata), self.assertRaises(ApiBudgetError):
                self.provider().preflight()
        self.assertEqual(self.transport.posts, [])

    def test_unexpected_model_or_usage_never_releases_reservation(self):
        for edits in ({"model": "other"}, {"usage": {"input_tokens": 200001, "output_tokens": 2}},
                      {"usage": {"input_tokens": 10, "output_tokens": 1001}}, {"stop_reason": "max_tokens"}):
            with self.subTest(edits=edits):
                (self.root / "budget.json").unlink(missing_ok=True)
                initialize_budget_ledger(self.root / "budget.json", CONFIG, EXECUTION)
                self.transport.edit = lambda response: {**response, **edits}
                provider = self.provider()
                provider.preflight()
                with self.assertRaises(ApiBudgetError):
                    self.score(provider)
                ledger = json.loads((self.root / "budget.json").read_text())
                self.assertEqual(ledger["entries"]["item-1"]["status"], "reserved")

    def test_config_change_cannot_reset_execution_budget(self):
        provider = self.provider()
        provider.preflight()
        self.score(provider)
        with self.assertRaisesRegex(ApiBudgetError, "another execution/config"):
            self.provider(replace(CONFIG, max_cost_usd="4")).preflight()

    def test_transport_rejects_routes_and_redacts_errors(self):
        transport = MessagesTransport("TEST-SECRET-DO-NOT-LOG", 30)
        with self.assertRaisesRegex(ApiBudgetError, "unapproved"):
            transport("POST", "https://evil.example/")
        with patch.object(transport._opener, "open", side_effect=RuntimeError("TEST-SECRET-DO-NOT-LOG")):
            with self.assertRaises(ApiBudgetError) as raised:
                transport("GET", "/v1/models/" + CONFIG.model_id)
            self.assertNotIn("TEST-SECRET", str(raised.exception))

    def test_native_json_projection_is_explicit_and_original_schema_is_unchanged(self):
        from foldarium_pipeline.weekly_selector_prompt import SELECTOR_MODEL_RESPONSE_SCHEMA
        original = copy.deepcopy(SELECTOR_MODEL_RESPONSE_SCHEMA)
        provider = self.provider(); provider.preflight(); self.score(provider)
        wire = self.transport.posts[0]['output_config']['format']
        self.assertEqual(wire['type'], 'json_schema')
        self.assertEqual(wire['schema'], wire_response_schema())
        self.assertNotIn('oneOf', wire['schema']['properties']['clustered'])
        confidence = wire['schema']['properties']['clustered']['anyOf'][0]['properties']['confidence']
        self.assertNotIn('minimum', confidence)
        self.assertIn('"minimum":0', confidence['description'])
        self.assertEqual(SELECTOR_MODEL_RESPONSE_SCHEMA, original)
        config = provider_config(CONFIG)
        self.assertEqual(config['wire_response_schema_sha256'], sha256_hex(wire['schema']))
        self.assertEqual(config['response_schema_sha256'], sha256_hex(original))
        self.assertEqual(config['schema_version'], 'foldarium.anthropic-api-config/v2')

    def test_projection_refuses_overlapping_or_unknown_schema_rules(self):
        from foldarium_pipeline.weekly_selector_prompt import SELECTOR_MODEL_RESPONSE_SCHEMA
        schema = copy.deepcopy(SELECTOR_MODEL_RESPONSE_SCHEMA)
        schema['properties']['clustered']['oneOf'][1]['properties']['selection_kind']['const'] = 'cluster'
        with self.assertRaisesRegex(ApiBudgetError, 'disjoint'):
            wire_response_schema(schema)
        with self.assertRaisesRegex(ApiBudgetError, 'reviewed wire projection'):
            wire_response_schema({'not': {}})

    def test_missing_required_capabilities_fails_before_paid_call(self):
        for capability in ('structured_outputs', 'image_input'):
            self.transport.metadata['capabilities'] = copy.deepcopy(CAPABILITIES)
            self.transport.metadata['capabilities'][capability]['supported'] = False
            with self.assertRaisesRegex(ApiBudgetError, 'capabilities'):
                self.provider().preflight()
        self.assertEqual(self.transport.posts, [])

    def test_missing_or_deleted_ledger_never_creates_fresh_budget(self):
        path = self.root / 'budget.json'
        path.unlink()
        with self.assertRaisesRegex(ApiBudgetError, 'ledger is missing'):
            self.provider().preflight()
        self.assertFalse(path.exists())
        initialize_budget_ledger(path, CONFIG, EXECUTION)
        provider = self.provider(); provider.preflight()
        path.unlink()
        with self.assertRaisesRegex(ApiBudgetError, 'ledger is missing'):
            self.score(provider)
        self.assertEqual(self.transport.posts, [])

    def test_initializer_cannot_overwrite_existing_budget(self):
        path = self.root / 'budget.json'
        original = path.read_bytes()
        with self.assertRaisesRegex(ApiBudgetError, 'initialization refused'):
            initialize_budget_ledger(path, CONFIG, EXECUTION)
        self.assertEqual(path.read_bytes(), original)

    def test_corrupt_reserved_and_settled_amounts_fail_closed(self):
        path = self.root / 'budget.json'
        provider = self.provider(); provider.preflight(); self.score(provider)
        original = json.loads(path.read_text())
        for delta in (-1, 1):
            ledger = copy.deepcopy(original)
            ledger['entries']['item-1']['cost_micro_usd'] += delta
            path.write_text(json.dumps(ledger))
            with self.assertRaisesRegex(ApiBudgetError, 'budget ledger is invalid'):
                validate_budget_ledger(path, CONFIG, EXECUTION)
        ledger = copy.deepcopy(original)
        ledger['entries']['item-1'] = {'status': 'reserved', 'request_sha256': 'a' * 64, 'cost_micro_usd': 1}
        path.write_text(json.dumps(ledger))
        with self.assertRaisesRegex(ApiBudgetError, 'budget ledger is invalid'):
            validate_budget_ledger(path, CONFIG, EXECUTION)
        self.assertEqual(len(self.transport.posts), 1)

    def test_ledger_total_above_frozen_cap_fails_before_network(self):
        path = self.root / 'budget.json'
        ledger = json.loads(path.read_text())
        ledger['entries'] = {str(i): {'status': 'reserved', 'request_sha256': 'a' * 64, 'cost_micro_usd': CONFIG.reserve_micro_usd} for i in range(4)}
        path.write_text(json.dumps(ledger))
        with self.assertRaisesRegex(ApiBudgetError, 'budget ledger is invalid'):
            self.provider().preflight()
        self.assertEqual(self.transport.posts, [])


class UnattendedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kit_bytes, self.kit = _build_kit_zip()
        self.kit_path = self.root / "kit.zip"
        self.kit_path.write_bytes(self.kit_bytes)
        self.job = {"round_id": self.kit["round_id"], "environment": self.kit["environment"],
            "blind_manifest_sha256": self.kit["blind_manifest_sha256"], "execution_id": EXECUTION,
            "driver": "anthropic-api", "model_id": CONFIG.model_id,
            "config_sha256": config_sha256(CONFIG), "max_cost_usd": CONFIG.max_cost_usd}
        self.transport = Transport({key: value["response"] for key, value in _fake_fixture(self.kit)["items"].items()})
        initialize_budget_ledger(self.root / "state" / "budget.json", CONFIG, EXECUTION)

    def run_inference(self, **overrides):
        params = dict(kit_path=self.kit_path, job=self.job, config=CONFIG.__dict__, state_dir=self.root / "state",
            egress_enforced=True, durable_commit=lambda: None, transport=self.transport)
        return run_isolated_inference(**{**params, **overrides})

    def test_real_runner_exact_provenance_and_completed_replay_without_paid_calls(self):
        result = self.run_inference()
        execution = json.loads(result.read_text())
        self.assertEqual(execution["provenance"]["config_sha256"], self.job["config_sha256"])
        self.assertEqual(execution["provider"], "anthropic-api")
        self.assertEqual(len(self.transport.posts), 2)
        self.assertEqual(self.run_inference(), result)
        self.assertEqual(len(self.transport.posts), 2)
        # Simulate loss after item completion but before final artifact publication.
        result.unlink()
        self.run_inference()
        self.assertEqual(len(self.transport.posts), 2)

    def test_network_binding_and_config_fail_before_paid_calls(self):
        for overrides in ({"egress_enforced": False}, {"job": {**self.job, "blind_manifest_sha256": "0" * 64}},
                          {"job": {**self.job, "config_sha256": "0" * 64}}):
            with self.subTest(overrides=overrides), self.assertRaises(ApiBudgetError):
                self.run_inference(**overrides)
        self.assertEqual(self.transport.posts, [])

    def test_completed_artifact_and_item_checkpoints_recover_after_rate_expiry(self):
        result = self.run_inference()
        completed = result.read_bytes()
        with patch("foldarium_pipeline.weekly_llm_providers.anthropic_api.datetime") as clock:
            clock.now.return_value = datetime(2100, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(self.run_inference().read_bytes(), completed)
            # Crash after all item responses, before the final artifact write.
            result.unlink()
            recovered = json.loads(self.run_inference().read_bytes())
        self.assertEqual(recovered["execution_id"], EXECUTION)
        self.assertEqual(recovered["provenance"]["config_sha256"], self.job["config_sha256"])
        self.assertEqual(len(self.transport.posts), 2)

    def test_outer_adapter_passes_only_blind_inputs_and_registers_validated_artifact(self):
        test = self
        class Private:
            def __init__(self):
                self.calls = []
                self.claimed = False
                self.fail_publication = True
            def _rpc(self, name, payload):
                self.calls.append((name, payload))
                if name == "claim_weekly_automation_inference_start_v1":
                    first = not self.claimed
                    self.claimed = True
                    return {**test.job, "kit_sha256": test.kit["kit_sha256"], "first_claim": first}
                if name == "register_weekly_automation_artifact_v1" and self.fail_publication:
                    self.fail_publication = False
                    raise ConnectionError("registration failed after completed inference")
                if name == "weekly_automation_snapshot_v1":
                    return {"rounds": [{**test.job, "benchmark_jobs": [test.job],
                        "kit": {"storage_path": "public/sha256/aa/hash", "kit_sha256": test.kit["kit_sha256"]},
                        "secret_answer_data": "NEVER PASS TO INFERENCE"}]}
            def store_bytes(self, content, media_type):
                return {"object_uri": "supabase://private/object", "sha256": sha256_hex(content)}
        class Public:
            def download_content_object(self, uri):
                return test.kit_bytes
        def launch(**args):
            self.assertEqual(set(args), {"kit_path", "job", "config", "state_dir"})
            self.assertNotIn("secret_answer_data", json.dumps(args["job"]))
            return run_isolated_inference(**args, egress_enforced=True, durable_commit=lambda: None, transport=self.transport)
        private = Private()
        with self.assertRaisesRegex(ConnectionError, "registration failed"):
            execute_benchmark_job(private, Public(), self.job, self.root / "outer", config=CONFIG.__dict__, launch_inference=launch)
        completed = (self.root / "outer" / EXECUTION / "benchmark.execution.json").read_bytes()
        with patch("foldarium_pipeline.weekly_llm_providers.anthropic_api.datetime") as clock:
            clock.now.return_value = datetime(2100, 1, 1, tzinfo=timezone.utc)
            result = execute_benchmark_job(private, Public(), self.job, self.root / "outer", config=CONFIG.__dict__, launch_inference=launch)
        self.assertEqual((self.root / "outer" / EXECUTION / "benchmark.execution.json").read_bytes(), completed)
        self.assertEqual(result["status"], "registered")
        self.assertEqual(private.calls[-1][0], "register_weekly_automation_artifact_v1")
        self.assertEqual(private.calls[-1][1]["p_execution_id"], EXECUTION)
        self.assertEqual(len(self.transport.posts), 2)


if __name__ == "__main__":
    unittest.main()
