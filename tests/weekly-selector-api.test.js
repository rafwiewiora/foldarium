import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import {
  createWeeklySelectorHandler,
  parseWeeklySelectorRoute,
  resolveSelectorConfig,
  verifySelectorToken,
} from '../api/weekly-selector.js';
import {
  BLINDNESS_ATTESTATION_SCHEMA_VERSION,
  EMPTY_NETWORK_ALLOWLIST_SHA256,
  SUBMISSION_SCHEMA_VERSION,
  canonicalJson,
  sha256Hex,
} from '../lib/weekly-selector-contract.js';
import {
  SELECTOR_BENCHMARK_SCHEMA_VERSION,
} from '../lib/weekly-selector-benchmark.js';
import {
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
} from '../lib/weekly-selector-prompt.js';

const BLIND_SHA = 'b'.repeat(64);
const KIT_SHA = 'a'.repeat(64);

const productionEnv = {
  FOLDARIUM_ENV: 'production',
  FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co',
  FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_production',
};

const previewEnv = {
  FOLDARIUM_ENV: 'preview',
  FOLDARIUM_PREVIEW_SUPABASE_URL: 'https://preview.supabase.co',
  FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_preview',
};

const blindManifest = {
  schema_version: 1,
  round_id: 'weekly-2026-08-08',
  items: [
    {
      id: 'item-a',
      choices: [{ id: 'choice-1', cluster_id: 'cluster-x', is_rep: true }],
    },
  ],
};

const round = {
  environment: 'production',
  round_id: 'weekly-2026-08-08',
  public_status: 'open',
  opens_at: '2026-08-08T16:00:00.000Z',
  closes_at: '2026-08-12T16:00:00.000Z',
  item_count: 1,
  blind_manifest: blindManifest,
  blind_manifest_sha256: BLIND_SHA,
};

const kitRow = {
  round_id: 'weekly-2026-08-08',
  blind_manifest_sha256: BLIND_SHA,
  kit_sha256: KIT_SHA,
  item_count: 1,
  byte_size: 2048,
  storage_path: 'selector-kits/weekly-2026-08-08/kit.zip',
  created_at: '2026-08-08T16:00:00.000Z',
};

function validSubmission(overrides = {}) {
  return {
    schema_version: SUBMISSION_SCHEMA_VERSION,
    submission_id: '00000000-0000-4000-8000-000000000099',
    environment: 'production',
    round_id: 'weekly-2026-08-08',
    blind_manifest_sha256: BLIND_SHA,
    kit_sha256: KIT_SHA,
    items: [{
      item_id: 'item-a',
      clustered: { selection_kind: 'cluster', cluster_id: 'cluster-x' },
      unclustered: { selection_kind: 'exact', choice_id: 'choice-1' },
    }],
    ...overrides,
  };
}

function validTokenRequest(overrides = {}) {
  return {
    environment: 'production',
    round_id: 'weekly-2026-08-08',
    display_name: '  Ada   Lovelace ',
    method_name: 'demo-method',
    method_version: '1.0.0',
    provider: 'example-provider',
    model_name: 'example-model',
    model_version: '2026-08-01',
    prompt_profile_id: SELECTOR_PROMPT_PROFILE_ID,
    prompt_sha256: SELECTOR_PROMPT_SHA256,
    tools_sha256: 'd'.repeat(64),
    config_sha256: 'e'.repeat(64),
    blindness_attestation: validBlindnessAttestation(),
    ...overrides,
  };
}

function validBlindnessAttestation(overrides = {}) {
  return {
    schema_version: BLINDNESS_ATTESTATION_SCHEMA_VERSION,
    workspace_policy: 'verified-kit-only',
    network_policy: 'none',
    network_allowlist_sha256: EMPTY_NETWORK_ALLOWLIST_SHA256,
    browser_enabled: false,
    web_search_enabled: false,
    external_retrieval_enabled: false,
    shared_cache_enabled: false,
    ...overrides,
  };
}

function validBenchmark(overrides = {}) {
  const attestation = validBlindnessAttestation();
  const executionId = '00000000-0000-4000-8000-000000000123';
  return {
    schema_version: SELECTOR_BENCHMARK_SCHEMA_VERSION,
    execution_id: executionId,
    supersedes_execution_id: null,
    run_class: 'post_close_benchmark',
    environment: 'production',
    round_id: round.round_id,
    blind_manifest_sha256: BLIND_SHA,
    kit_sha256: KIT_SHA,
    display_name: 'Claude Opus',
    method_name: 'blind-pose-selector',
    method_version: SELECTOR_PROMPT_PROFILE_ID,
    provider: 'anthropic',
    engine: {
      name: 'claude-cli',
      version: '1.2.3',
      run_id: null,
      session_id: 'session-1',
    },
    model: {
      requested_id: 'opus',
      observed_ids: ['claude-opus-exact'],
      requested_effort: 'default',
      applied_effort: null,
      effort_reporting: 'not_exposed',
    },
    provenance: {
      prompt_profile_id: SELECTOR_PROMPT_PROFILE_ID,
      prompt_sha256: SELECTOR_PROMPT_SHA256,
      input_manifest_sha256: '1'.repeat(64),
      tools_sha256: '2'.repeat(64),
      config_sha256: '3'.repeat(64),
      runtime_sha256: '4'.repeat(64),
    },
    blindness_attestation: attestation,
    blindness_attestation_sha256: sha256Hex(canonicalJson(attestation)),
    usage: {
      input_tokens: 10,
      output_tokens: 10,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      reasoning_tokens: null,
      cost_usd: 0,
      duration_ms: 1000,
    },
    started_at: '2026-08-26T12:00:00.000Z',
    finished_at: '2026-08-26T12:00:01.000Z',
    reasoning_trace_retained: false,
    output_sha256: '5'.repeat(64),
    payload: validSubmission({ submission_id: executionId }),
    ...overrides,
  };
}

function invoke(handler, request) {
  const headers = {};
  let statusCode;
  let body;
  const response = {
    setHeader(name, value) {
      headers[name] = value;
    },
    status(value) {
      statusCode = value;
      return this;
    },
    json(value) {
      body = value;
      return this;
    },
  };
  return handler(request, response).then(() => ({
    statusCode,
    headers,
    body,
    text: JSON.stringify(body),
  }));
}

function recordingFetch(routes) {
  async function fetchImpl(url, options = {}) {
    fetchImpl.calls.push({
      url,
      method: options.method,
      headers: options.headers,
      body: options.body,
    });
    for (const route of routes) {
      if (route.match(url, options)) {
        const payload = await route.respond(url, options);
        return {
          ok: route.ok ?? true,
          status: route.status ?? (route.ok === false ? 500 : 200),
          text: async () => JSON.stringify(payload),
        };
      }
    }
    return {
      ok: false,
      status: 404,
      text: async () => JSON.stringify({ message: 'not found' }),
    };
  }
  fetchImpl.calls = [];
  return fetchImpl;
}

function makeHandler({ env = productionEnv, fetchImpl } = {}) {
  return createWeeklySelectorHandler({
    env,
    fetchImpl: fetchImpl ?? (() => { throw new Error('fetch must be mocked'); }),
  });
}

test('Preview config never falls back to production credentials', () => {
  const config = resolveSelectorConfig({
    ...productionEnv,
    FOLDARIUM_ENV: 'preview',
    SUPABASE_URL: 'https://server-only.supabase.co',
    SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_never',
  });
  assert.equal(config.configured, false);
  assert.equal(config.url, '');
  assert.doesNotMatch(JSON.stringify(config), /production|server-only|sb_secret_never/);
});

test('routes current round, kit, token, submission, and receipt actions', () => {
  assert.deepEqual(
    parseWeeklySelectorRoute({ url: 'https://foldarium.test/api/weekly-selector/docs' }),
    { name: 'docs' },
  );
  assert.deepEqual(
    parseWeeklySelectorRoute({ url: 'https://foldarium.test/api/weekly-selector/rounds/current' }),
    { name: 'current-round' },
  );
  assert.deepEqual(
    parseWeeklySelectorRoute({ url: 'https://foldarium.test/api/weekly-selector/kits/weekly-2026-08-08' }),
    { name: 'kit', roundId: 'weekly-2026-08-08' },
  );
  assert.deepEqual(
    parseWeeklySelectorRoute({ query: { action: 'submissions/00000000-0000-4000-8000-000000000001' } }),
    { name: 'receipt', submissionId: '00000000-0000-4000-8000-000000000001' },
  );
  assert.deepEqual(
    parseWeeklySelectorRoute({ url: 'https://foldarium.test/api/weekly-selector/tokens/22222222-2222-4222-8222-222222222222' }),
    { name: 'revoke-token', tokenId: '22222222-2222-4222-8222-222222222222' },
  );
  assert.deepEqual(
    parseWeeklySelectorRoute({ url: 'https://foldarium.test/api/weekly-selector/benchmarks' }),
    { name: 'submit-benchmark' },
  );
});

test('serves static API documentation without database credentials', async () => {
  const response = await invoke(makeHandler({ env: {} }), {
    method: 'GET',
    url: 'https://foldarium.test/api/weekly-selector/docs',
  });
  assert.equal(response.statusCode, 200);
  assert.equal(response.body.complete_only, true);
  assert.equal(response.body.schema_version, 'foldarium.weekly-selector-api/v2');
  assert.equal(response.body.canonical_json_required, true);
  assert.equal(response.body.prompt_profile.prompt_profile_id, SELECTOR_PROMPT_PROFILE_ID);
  assert.equal(response.body.prompt_profile.prompt_sha256, SELECTOR_PROMPT_SHA256);
  assert.equal(
    response.body.blindness_attestation_schema_version,
    BLINDNESS_ATTESTATION_SCHEMA_VERSION,
  );
  assert.equal(
    response.body.token_request.blindness_attestation.empty_network_allowlist_sha256,
    EMPTY_NETWORK_ALLOWLIST_SHA256,
  );
  assert.deepEqual(
    response.body.token_request.exact_keys.at(-1),
    'blindness_attestation',
  );
  assert.equal(response.body.endpoints.submit.path, '/api/weekly-selector/submissions');
  assert.equal(
    response.body.endpoints.submit_post_close_benchmark.publication_class,
    'post_close_benchmark',
  );
  assert.equal(response.body.decision_modes.clustered[0].selection_kind, 'cluster');
  assert.equal(response.body.decision_modes.unclustered[0].selection_kind, 'exact');
  assert.equal(response.body.legacy_v1.accepted_by_v2_endpoints, false);
});

test('returns current round and kit descriptor without secrets', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_current_weekly_quiz_round'),
      respond: () => [round],
    },
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: () => [kitRow],
    },
  ]);

  const response = await invoke(makeHandler({
    fetchImpl,
    env: productionEnv,
  }), {
    method: 'GET',
    url: 'https://foldarium.test/api/weekly-selector/rounds/current',
  });

  assert.equal(response.statusCode, 200);
  assert.equal(response.headers['Cache-Control'], 'no-store');
  assert.equal(response.body.round_id, 'weekly-2026-08-08');
  assert.equal(response.body.environment, 'production');
  assert.equal(response.body.blind_manifest_sha256, BLIND_SHA);
  assert.equal(response.body.kit.kit_sha256, KIT_SHA);
  assert.equal(response.body.prompt_profile.prompt_profile_id, SELECTOR_PROMPT_PROFILE_ID);
  assert.doesNotMatch(response.text, /sb_secret_production|"blind_manifest":/);
  assert.equal(fetchImpl.calls[0].headers.Authorization, undefined);
  assert.equal(fetchImpl.calls[0].headers.apikey, 'sb_publishable_production');
  assert.deepEqual(JSON.parse(fetchImpl.calls[1].body), {
    p_round_id: round.round_id,
    p_environment: 'production',
  });
});

test('returns a verified kit redirect descriptor', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: () => [kitRow],
    },
  ]);

  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'GET',
    url: 'https://foldarium.test/api/weekly-selector/kits/weekly-2026-08-08',
  });

  assert.equal(response.statusCode, 200);
  assert.equal(response.body.descriptor_verified, true);
  assert.match(response.body.download_url, /selector-kits\/weekly-2026-08-08\/kit\.zip$/);
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].body), {
    p_round_id: round.round_id,
    p_environment: 'production',
  });
});

test('issues a round-scoped expiring v2 token without exposing persisted hashes', async () => {
  const fetchImpl = recordingFetch([
    {
      match: (url, options) => url.includes('/auth/v1/user')
        && options.headers?.Authorization === 'Bearer browser-jwt',
      respond: () => ({ id: '11111111-1111-4111-8111-111111111111' }),
    },
    {
      match: url => url.includes('/rpc/issue_weekly_selector_token_v2'),
      respond: (_url, options) => {
        assert.equal(options.headers.Authorization, 'Bearer browser-jwt');
        assert.equal(options.headers.apikey, 'sb_publishable_production');
        const body = JSON.parse(options.body);
        assert.equal(body.p_user_id, undefined);
        assert.match(body.p_token_hash, /^[0-9a-f]{64}$/);
        assert.equal(body.p_display_name, 'Ada Lovelace');
        assert.equal(body.p_environment, 'production');
        assert.equal(body.p_round_id, round.round_id);
        assert.equal(body.p_provider, 'example-provider');
        assert.equal(body.p_model_name, 'example-model');
        assert.equal(body.p_model_version, '2026-08-01');
        assert.equal(body.p_prompt_profile_id, SELECTOR_PROMPT_PROFILE_ID);
        assert.equal(body.p_prompt_sha256, SELECTOR_PROMPT_SHA256);
        assert.equal(body.p_tools_sha256, 'd'.repeat(64));
        assert.equal(body.p_config_sha256, 'e'.repeat(64));
        assert.deepEqual(body.p_blindness_attestation, validBlindnessAttestation());
        assert.equal(
          body.p_blindness_attestation_sha256,
          createHash('sha256')
            .update(canonicalJson(validBlindnessAttestation()))
            .digest('hex'),
        );
        return [{
          token_id: '22222222-2222-4222-8222-222222222222',
          expires_at: round.closes_at,
        }];
      },
    },
  ]);

  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/tokens',
    headers: {
      authorization: 'Bearer browser-jwt',
      'content-type': 'application/json',
    },
    body: JSON.stringify(validTokenRequest()),
  });

  assert.equal(response.statusCode, 201);
  assert.match(response.body.token, /^[-A-Za-z0-9_]{40,}$/);
  assert.equal(response.body.round_id, round.round_id);
  assert.equal(response.body.expires_at, round.closes_at);
  assert.equal(response.body.provider, 'example-provider');
  assert.doesNotMatch(response.text, /sb_secret_production|browser-jwt|[0-9a-f]{64}/);
});

test('token issuance fails closed before RPC for invalid blindness capabilities', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/auth/v1/user'),
      respond: () => { throw new Error('authentication must not be called'); },
    },
    {
      match: url => url.includes('/rpc/issue_weekly_selector_token_v2'),
      respond: () => { throw new Error('issuance RPC must not be called'); },
    },
  ]);
  const invalidAttestations = [
    validBlindnessAttestation({ browser_enabled: true }),
    validBlindnessAttestation({ unknown_capability: false }),
    validBlindnessAttestation({ network_policy: 'open' }),
    validBlindnessAttestation({ network_allowlist_sha256: 'f'.repeat(64) }),
  ];
  for (const blindnessAttestation of invalidAttestations) {
    const response = await invoke(makeHandler({ fetchImpl }), {
      method: 'POST',
      url: 'https://foldarium.test/api/weekly-selector/tokens',
      headers: {
        authorization: 'Bearer browser-jwt',
        'content-type': 'application/json',
      },
      body: JSON.stringify(validTokenRequest({
        blindness_attestation: blindnessAttestation,
      })),
    });
    assert.equal(response.statusCode, 400);
    assert.deepEqual(response.body, { error: 'Invalid token request' });
  }
  assert.equal(fetchImpl.calls.length, 0);
});

test('revokes only an authenticated owner token in the deployment environment', async () => {
  const tokenId = '22222222-2222-4222-8222-222222222222';
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/auth/v1/user'),
      respond: () => ({ id: '11111111-1111-4111-8111-111111111111' }),
    },
    {
      match: url => url.includes('/rpc/revoke_weekly_selector_token_v2'),
      respond: (_url, options) => {
        assert.equal(options.headers.Authorization, 'Bearer browser-jwt');
        assert.deepEqual(JSON.parse(options.body), {
          p_token_id: tokenId,
          p_environment: 'production',
        });
        return [{ token_id: tokenId, revoked_at: '2026-08-09T01:00:00.000Z' }];
      },
    },
  ]);
  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'DELETE',
    url: `https://foldarium.test/api/weekly-selector/tokens/${tokenId}`,
    headers: { authorization: 'Bearer browser-jwt' },
  });
  assert.equal(response.statusCode, 200);
  assert.equal(response.body.token_id, tokenId);
});

test('accepts complete submissions with bearer tokens and returns immutable receipts', async () => {
  const submission = validSubmission();
  const bearer = 'selector-bearer-token';

  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_round_v2'),
      respond: () => [round],
    },
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: () => [kitRow],
    },
    {
      match: url => url.includes('/rpc/submit_weekly_selector_complete_v2'),
      respond: (_url, options) => {
        const body = JSON.parse(options.body);
        assert.equal(body.p_token_hash, createHash('sha256').update(bearer).digest('hex'));
        assert.equal(body.p_environment, 'production');
        assert.equal(body.p_round_id, submission.round_id);
        assert.equal(body.p_blind_manifest_sha256, BLIND_SHA);
        assert.equal(body.p_kit_sha256, KIT_SHA);
        return [{
          submission_id: submission.submission_id,
          revision_number: 1,
          environment: submission.environment,
          round_id: submission.round_id,
          blind_manifest_sha256: submission.blind_manifest_sha256,
          kit_sha256: submission.kit_sha256,
          payload_digest: body.p_payload_digest,
          submitted_at: '2026-08-09T01:00:00.000Z',
          idempotent: false,
        }];
      },
    },
  ]);

  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/submissions',
    headers: {
      authorization: `Bearer ${bearer}`,
      'content-type': 'application/json',
    },
    body: canonicalJson(submission),
  });

  assert.equal(response.statusCode, 201);
  assert.equal(response.body.submission_id, submission.submission_id);
  assert.equal(response.body.environment, 'production');
  assert.equal(response.body.blind_manifest_sha256, BLIND_SHA);
  assert.match(response.body.payload_digest, /^[0-9a-f]{64}$/);
  assert.doesNotMatch(response.text, /user_id|token_hash|selector-bearer-token/);
});

test('registers post-close benchmarks through a separate service-only endpoint', async () => {
  const benchmark = validBenchmark();
  const serviceRoleKey = 'service-role-secret';
  const ingestToken = 'benchmark-ingest-secret';
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_round_v2'),
      respond: (_url, options) => {
        assert.equal(options.headers.Authorization, `Bearer ${serviceRoleKey}`);
        return [{ ...round, public_status: 'closed' }];
      },
    },
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: (_url, options) => {
        assert.equal(options.headers.Authorization, `Bearer ${serviceRoleKey}`);
        return [kitRow];
      },
    },
    {
      match: url => url.includes('/rpc/register_weekly_selector_benchmark_v1'),
      respond: (_url, options) => {
        assert.equal(options.headers.Authorization, `Bearer ${serviceRoleKey}`);
        assert.equal(options.headers.apikey, serviceRoleKey);
        const body = JSON.parse(options.body);
        assert.deepEqual(body.p_execution, benchmark);
        assert.match(body.p_execution_sha256, /^[0-9a-f]{64}$/);
        assert.match(body.p_payload_digest, /^[0-9a-f]{64}$/);
        return [{
          execution_id: benchmark.execution_id,
          environment: benchmark.environment,
          round_id: benchmark.round_id,
          execution_sha256: body.p_execution_sha256,
          payload_digest: body.p_payload_digest,
          accepted_at: '2026-08-26T12:00:02.000Z',
          idempotent: false,
        }];
      },
    },
  ]);
  const response = await invoke(makeHandler({
    fetchImpl,
    env: {
      ...productionEnv,
      FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: serviceRoleKey,
      FOLDARIUM_PRODUCTION_SELECTOR_BENCHMARK_INGEST_TOKEN: ingestToken,
    },
  }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/benchmarks',
    headers: {
      authorization: `Bearer ${ingestToken}`,
      'content-type': 'application/json',
    },
    body: canonicalJson(benchmark),
  });

  assert.equal(response.statusCode, 201);
  assert.equal(response.body.run_class, 'post_close_benchmark');
  assert.equal(response.body.execution_id, benchmark.execution_id);
  assert.doesNotMatch(response.text, /service-role|ingest-secret|session-1|usage/);
});

test('benchmark ingest fails closed without dedicated credentials or after reveal', async () => {
  const disabled = await invoke(makeHandler({ env: productionEnv }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/benchmarks',
    headers: {
      authorization: 'Bearer anything',
      'content-type': 'application/json',
    },
    body: canonicalJson(validBenchmark()),
  });
  assert.equal(disabled.statusCode, 503);

  const env = {
    ...productionEnv,
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: 'service-role',
    FOLDARIUM_PRODUCTION_SELECTOR_BENCHMARK_INGEST_TOKEN: 'ingest-token',
  };
  const unauthorized = await invoke(makeHandler({ env }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/benchmarks',
    headers: {
      authorization: 'Bearer wrong',
      'content-type': 'application/json',
    },
    body: canonicalJson(validBenchmark()),
  });
  assert.equal(unauthorized.statusCode, 401);

  const fetchImpl = recordingFetch([{
    match: url => url.includes('/rpc/get_weekly_selector_round_v2'),
    respond: () => [{ ...round, public_status: 'open' }],
  }]);
  const openRound = await invoke(makeHandler({ env, fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/benchmarks',
    headers: {
      authorization: 'Bearer ingest-token',
      'content-type': 'application/json',
    },
    body: canonicalJson(validBenchmark()),
  });
  assert.equal(openRound.statusCode, 409);
});

test('uses timing-safe token hash verification helper', () => {
  const token = 'secret-token';
  const hash = createHash('sha256').update(token).digest('hex');
  assert.equal(verifySelectorToken(token, hash), true);
  assert.equal(verifySelectorToken('wrong-token', hash), false);
});

test('returns idempotent success for the same submission id and digest', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_round_v2'),
      respond: () => [round],
    },
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: () => [kitRow],
    },
    {
      match: url => url.includes('/rpc/submit_weekly_selector_complete_v2'),
      respond: () => [{
        submission_id: '00000000-0000-4000-8000-000000000099',
        revision_number: 1,
        environment: 'production',
        round_id: 'weekly-2026-08-08',
        blind_manifest_sha256: BLIND_SHA,
        kit_sha256: KIT_SHA,
        payload_digest: 'b'.repeat(64),
        submitted_at: '2026-08-09T01:00:00.000Z',
        idempotent: true,
      }],
    },
  ]);

  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/submissions',
    headers: {
      authorization: 'Bearer token',
      'content-type': 'application/json',
    },
    body: canonicalJson(validSubmission()),
  });

  assert.equal(response.statusCode, 200);
});

test('conflicts when the same submission id is rebound to a different payload', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_round_v2'),
      respond: () => [round],
    },
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: () => [kitRow],
    },
    {
      match: url => url.includes('/rpc/submit_weekly_selector_complete_v2'),
      ok: false,
      status: 409,
      respond: () => ({ message: 'selector submission id is already bound to a different payload' }),
    },
  ]);

  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/submissions',
    headers: {
      authorization: 'Bearer token',
      'content-type': 'application/json',
    },
    body: canonicalJson(validSubmission()),
  });

  assert.equal(response.statusCode, 409);
  assert.equal(response.body.error, 'Submission id is already bound to a different payload');
});

test('returns immutable receipts and rejects invalid methods or media types', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_receipt_v2'),
      respond: () => [{
        submission_id: '00000000-0000-4000-8000-000000000099',
        revision_number: 2,
        environment: 'production',
        round_id: 'weekly-2026-08-08',
        blind_manifest_sha256: BLIND_SHA,
        kit_sha256: KIT_SHA,
        payload_digest: 'c'.repeat(64),
        submitted_at: '2026-08-09T02:00:00.000Z',
      }],
    },
  ]);

  const missingBearer = await invoke(makeHandler({ fetchImpl }), {
    method: 'GET',
    url: 'https://foldarium.test/api/weekly-selector/submissions/00000000-0000-4000-8000-000000000099',
  });
  assert.equal(missingBearer.statusCode, 401);

  const receipt = await invoke(makeHandler({ fetchImpl }), {
    method: 'GET',
    url: 'https://foldarium.test/api/weekly-selector/submissions/00000000-0000-4000-8000-000000000099',
    headers: { authorization: 'Bearer selector-token' },
  });
  assert.equal(receipt.statusCode, 200);
  assert.equal(receipt.body.revision_number, 2);
  assert.equal(receipt.body.blind_manifest_sha256, BLIND_SHA);
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].body), {
    p_submission_id: '00000000-0000-4000-8000-000000000099',
    p_token_hash: createHash('sha256').update('selector-token').digest('hex'),
    p_environment: 'production',
  });

  const wrongMethod = await invoke(makeHandler({ fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/rounds/current',
  });
  assert.equal(wrongMethod.statusCode, 405);
  assert.equal(wrongMethod.headers.Allow, 'GET');

  const badMedia = await invoke(makeHandler({ fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/tokens',
    headers: { 'content-type': 'text/plain', authorization: 'Bearer browser-jwt' },
    body: '{}',
  });
  assert.equal(badMedia.statusCode, 415);
});

test('maps database token expiry and revocation authorization failures to 401', async () => {
  const fetchImpl = recordingFetch([{
    match: url => url.includes('/rpc/get_weekly_selector_receipt_v2'),
    ok: false,
    status: 403,
    respond: () => ({ code: '42501', message: 'selector v2 bearer token is invalid or revoked' }),
  }]);
  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'GET',
    url: 'https://foldarium.test/api/weekly-selector/submissions/00000000-0000-4000-8000-000000000099',
    headers: { authorization: 'Bearer revoked-token' },
  });
  assert.equal(response.statusCode, 401);
});

test('Preview handler fails closed when preview credentials are absent', async () => {
  const response = await invoke(makeHandler({
    env: { FOLDARIUM_ENV: 'preview' },
  }), {
    method: 'GET',
    url: 'https://foldarium.test/api/weekly-selector/rounds/current',
  });
  assert.equal(response.statusCode, 500);
  assert.equal(response.body.error, 'Weekly selector service is not configured');
});

test('rejects malformed submission bodies with validation errors', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_round_v2'),
      respond: () => [round],
    },
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: () => [kitRow],
    },
    {
      match: url => url.includes('/rpc/submit_weekly_selector_complete_v2'),
      respond: () => { throw new Error('submit must not be called'); },
    },
  ]);
  const response = await invoke(makeHandler({ fetchImpl }), {
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/submissions',
    headers: {
      authorization: 'Bearer token',
      'content-type': 'application/json',
    },
    body: canonicalJson(validSubmission({
      items: [{
        item_id: 'item-a',
        clustered: { selection_kind: 'none' },
        unclustered: { selection_kind: 'exact', choice_id: 'missing' },
      }],
    })),
  });
  assert.equal(response.statusCode, 400);
  assert.equal(fetchImpl.calls.filter(call => call.url.includes('submit_weekly_selector_complete_v2')).length, 0);
});

test('rejects inferred scope, v1, noncanonical JSON, and oversized bodies', async () => {
  const fetchImpl = recordingFetch([
    {
      match: url => url.includes('/rpc/get_weekly_selector_round_v2'),
      respond: () => [round],
    },
    {
      match: url => url.includes('/rpc/get_weekly_selector_kit_descriptor'),
      respond: () => [kitRow],
    },
  ]);
  const handler = makeHandler({ fetchImpl });
  const request = body => ({
    method: 'POST',
    url: 'https://foldarium.test/api/weekly-selector/submissions',
    headers: {
      authorization: 'Bearer token',
      'content-type': 'application/json',
    },
    body,
  });

  const missingEnvironment = validSubmission();
  delete missingEnvironment.environment;
  assert.equal((await invoke(handler, request(canonicalJson(missingEnvironment)))).statusCode, 400);
  assert.equal((await invoke(handler, request(canonicalJson({
    ...validSubmission(),
    schema_version: 'foldarium.selector-submission/v1',
  })))).statusCode, 400);

  const noncanonical = JSON.stringify(validSubmission(), null, 2);
  assert.notEqual(noncanonical, canonicalJson(validSubmission()));
  assert.equal((await invoke(handler, request(noncanonical))).statusCode, 400);

  assert.equal((await invoke(handler, request(' '.repeat(131_073)))).statusCode, 413);
  assert.equal(
    fetchImpl.calls.filter(call => call.url.includes('submit_weekly_selector_complete_v2')).length,
    0,
  );
});
