import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SELECTOR_BENCHMARK_SCHEMA_VERSION,
  digestPostCloseBenchmark,
  sanitizePostCloseBenchmarkReceipt,
  validatePostCloseBenchmark,
} from '../lib/weekly-selector-benchmark.js';
import {
  BLINDNESS_ATTESTATION_SCHEMA_VERSION,
  EMPTY_NETWORK_ALLOWLIST_SHA256,
  canonicalJson,
  sha256Hex,
} from '../lib/weekly-selector-contract.js';
import {
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
} from '../lib/weekly-selector-prompt.js';

const EXECUTION_ID = '00000000-0000-4000-8000-000000000123';
const BLIND_SHA = 'a'.repeat(64);
const KIT_SHA = 'b'.repeat(64);
const blindManifest = {
  schema_version: 1,
  round_id: 'weekly-test',
  items: [{
    id: 'ITEM01',
    choices: [{ id: 'choice-a', cluster_id: 'cluster-a', is_rep: true }],
  }],
};
const context = {
  environment: 'preview',
  roundId: 'weekly-test',
  blindManifestSha256: BLIND_SHA,
  kitSha256: KIT_SHA,
  blindManifest,
};

function blindnessAttestation() {
  return {
    schema_version: BLINDNESS_ATTESTATION_SCHEMA_VERSION,
    workspace_policy: 'verified-kit-only',
    network_policy: 'none',
    network_allowlist_sha256: EMPTY_NETWORK_ALLOWLIST_SHA256,
    browser_enabled: false,
    web_search_enabled: false,
    external_retrieval_enabled: false,
    shared_cache_enabled: false,
  };
}

function execution(overrides = {}) {
  const attestation = blindnessAttestation();
  return {
    schema_version: SELECTOR_BENCHMARK_SCHEMA_VERSION,
    execution_id: EXECUTION_ID,
    supersedes_execution_id: null,
    run_class: 'post_close_benchmark',
    environment: 'preview',
    round_id: 'weekly-test',
    blind_manifest_sha256: BLIND_SHA,
    kit_sha256: KIT_SHA,
    display_name: 'Claude Opus',
    method_name: 'blind-pose-selector',
    method_version: 'weekly-pose-selector-v1',
    provider: 'anthropic',
    engine: {
      name: 'claude-cli',
      version: '1.2.3',
      run_id: null,
      session_id: 'session-1',
    },
    model: {
      requested_id: 'opus',
      observed_ids: ['claude-opus-4-1-20260805'],
      requested_effort: 'default',
      applied_effort: null,
      effort_reporting: 'not_exposed',
    },
    provenance: {
      prompt_profile_id: SELECTOR_PROMPT_PROFILE_ID,
      prompt_sha256: SELECTOR_PROMPT_SHA256,
      input_manifest_sha256: 'c'.repeat(64),
      tools_sha256: 'd'.repeat(64),
      config_sha256: 'e'.repeat(64),
      runtime_sha256: 'f'.repeat(64),
    },
    blindness_attestation: attestation,
    blindness_attestation_sha256: sha256Hex(canonicalJson(attestation)),
    usage: {
      input_tokens: 1200,
      output_tokens: 80,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      reasoning_tokens: null,
      cost_usd: 0,
      duration_ms: 9000,
    },
    started_at: '2026-08-26T12:00:00.000Z',
    finished_at: '2026-08-26T12:00:09.000Z',
    reasoning_trace_retained: false,
    output_sha256: '9'.repeat(64),
    payload: {
      schema_version: 'foldarium.selector-submission/v2',
      submission_id: EXECUTION_ID,
      environment: 'preview',
      round_id: 'weekly-test',
      blind_manifest_sha256: BLIND_SHA,
      kit_sha256: KIT_SHA,
      items: [{
        item_id: 'ITEM01',
        clustered: { selection_kind: 'cluster', cluster_id: 'cluster-a' },
        unclustered: { selection_kind: 'exact', choice_id: 'choice-a' },
      }],
    },
    ...overrides,
  };
}

test('normalizes a complete Claude default-effort post-close benchmark', () => {
  const normalized = validatePostCloseBenchmark(execution(), context);
  assert.equal(normalized.run_class, 'post_close_benchmark');
  assert.equal(normalized.model.requested_effort, 'default');
  assert.equal(normalized.model.applied_effort, null);
  assert.equal(normalized.model.effort_reporting, 'not_exposed');
  assert.equal(normalized.reasoning_trace_retained, false);
  assert.match(digestPostCloseBenchmark(normalized, context), /^[0-9a-f]{64}$/);
});

test('accepts a single observed Sol model with reported high effort', () => {
  const normalized = validatePostCloseBenchmark(execution({
    display_name: 'GPT-5.6 Sol',
    provider: 'cursor',
    engine: {
      name: 'cursor-sdk',
      version: '0.1.0',
      run_id: 'run-1',
      session_id: null,
    },
    model: {
      requested_id: 'gpt-5.6-sol-high',
      observed_ids: ['gpt-5.6-sol-2026-08-20'],
      requested_effort: 'high',
      applied_effort: 'high',
      effort_reporting: 'reported',
    },
  }), context);
  assert.equal(normalized.model.observed_ids.length, 1);
  assert.equal(normalized.model.applied_effort, 'high');
});

test('rejects fallback models, prompt drift, reasoning retention, and noncanonical time', () => {
  for (const [mutate, pattern] of [
    [value => value.model.observed_ids.push('fallback-model'), /exactly one model/],
    [value => { value.provenance.prompt_sha256 = '0'.repeat(64); }, /prompt_sha256/],
    [value => { value.reasoning_trace_retained = true; }, /reasoning_trace_retained/],
    [value => { value.started_at = '2026-08-26T12:00:00Z'; }, /canonical UTC/],
    [value => { value.blindness_attestation_sha256 = '0'.repeat(64); }, /attestation digest/],
  ]) {
    const candidate = execution();
    mutate(candidate);
    assert.throws(() => validatePostCloseBenchmark(candidate, context), pattern);
  }
});

test('requires execution and payload identities to match and supports explicit supersession', () => {
  assert.throws(
    () => validatePostCloseBenchmark(execution({
      payload: { ...execution().payload, submission_id: '00000000-0000-4000-8000-000000000999' },
    }), context),
    /submission_id must equal execution_id/,
  );
  assert.throws(
    () => validatePostCloseBenchmark(execution({
      supersedes_execution_id: EXECUTION_ID,
    }), context),
    /cannot supersede itself/,
  );
});

test('receipt exposes only immutable benchmark classification and digests', () => {
  const receipt = sanitizePostCloseBenchmarkReceipt({
    execution_id: EXECUTION_ID,
    environment: 'preview',
    round_id: 'weekly-test',
    execution_sha256: '1'.repeat(64),
    payload_digest: '2'.repeat(64),
    accepted_at: '2026-08-26T12:01:00.000Z',
    idempotent: true,
    execution: { secret: true },
  });
  assert.equal(receipt.run_class, 'post_close_benchmark');
  assert.equal(receipt.idempotent, true);
  assert.doesNotMatch(JSON.stringify(receipt), /secret|"payload":|usage|session/);
});
