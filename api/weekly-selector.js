import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import {
  BLINDNESS_ATTESTATION_SCHEMA_VERSION,
  ContractError,
  EMPTY_NETWORK_ALLOWLIST_SHA256,
  ID_RE,
  KIT_SCHEMA_VERSION,
  MAX_REQUEST_BODY_BYTES,
  SHA256_RE,
  SUBMISSION_SCHEMA_VERSION,
  UUID_RE,
  canonicalJson,
  sha256Hex,
  validateCompleteSubmission,
  validateKitDescriptor,
  validateTokenRequest,
} from '../lib/weekly-selector-contract.js';
import {
  SELECTOR_BENCHMARK_SCHEMA_VERSION,
  digestPostCloseBenchmark,
  sanitizePostCloseBenchmarkReceipt,
  validatePostCloseBenchmark,
} from '../lib/weekly-selector-benchmark.js';
import { SELECTOR_PROMPT_PROFILE } from '../lib/weekly-selector-prompt.js';

const MAX_BENCHMARK_BODY_BYTES = 262_144;

export function resolveSelectorConfig(env = process.env) {
  const deploymentEnvironment = normalizeDeploymentEnvironment(env.FOLDARIUM_ENV);
  if (deploymentEnvironment === 'preview') {
    return configFromNames(env, {
      url: 'FOLDARIUM_PREVIEW_SUPABASE_URL',
      publishableKey: 'FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY',
      anonKey: 'FOLDARIUM_PREVIEW_SUPABASE_ANON_KEY',
      serviceRoleKey: 'FOLDARIUM_PREVIEW_SUPABASE_SERVICE_ROLE_KEY',
      benchmarkIngestToken: 'FOLDARIUM_PREVIEW_SELECTOR_BENCHMARK_INGEST_TOKEN',
      deploymentEnvironment,
    });
  }
  if (deploymentEnvironment === 'production') {
    return configFromNames(env, {
      url: 'FOLDARIUM_PRODUCTION_SUPABASE_URL',
      publishableKey: 'FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY',
      anonKey: 'FOLDARIUM_PRODUCTION_SUPABASE_ANON_KEY',
      serviceRoleKey: 'FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY',
      benchmarkIngestToken: 'FOLDARIUM_PRODUCTION_SELECTOR_BENCHMARK_INGEST_TOKEN',
      deploymentEnvironment,
    });
  }
  return configFromNames(env, {
    url: 'FOLDARIUM_DEVELOPMENT_SUPABASE_URL',
    publishableKey: 'FOLDARIUM_DEVELOPMENT_SUPABASE_PUBLISHABLE_KEY',
    anonKey: 'FOLDARIUM_DEVELOPMENT_SUPABASE_ANON_KEY',
    fallbackUrl: env.SUPABASE_URL,
    fallbackPublishableKey: env.SUPABASE_ANON_KEY,
    serviceRoleKey: 'SUPABASE_SERVICE_ROLE_KEY',
    benchmarkIngestToken: 'FOLDARIUM_SELECTOR_BENCHMARK_INGEST_TOKEN',
    deploymentEnvironment,
  });
}

function configFromNames(env, names) {
  const url = normalizedHttpsUrl(env[names.url] || names.fallbackUrl);
  const publishableKey = trimmedString(env[names.publishableKey] || env[names.anonKey]
    || names.fallbackPublishableKey);
  const serviceRoleKey = trimmedString(env[names.serviceRoleKey]);
  const benchmarkIngestToken = trimmedString(env[names.benchmarkIngestToken]);
  return {
    url,
    publishableKey,
    serviceRoleKey,
    benchmarkIngestToken,
    deploymentEnvironment: names.deploymentEnvironment,
    configured: Boolean(url && publishableKey),
  };
}

export function parseWeeklySelectorRoute(request) {
  const url = request.url
    ? new URL(request.url, 'http://foldarium.local')
    : new URL('http://foldarium.local/api/weekly-selector');
  const queryAction = request.query?.action || url.searchParams.get('action');
  const segments = url.pathname.replace(/\/+$/, '').split('/').filter(Boolean);
  const rest = segments[0] === 'api' && segments[1] === 'weekly-selector'
    ? segments.slice(2)
    : segments.slice(segments.indexOf('weekly-selector') + 1);

  if (queryAction) {
    const querySegments = String(queryAction).split('/').filter(Boolean);
    return routeFromSegments(querySegments, url.searchParams);
  }
  return routeFromSegments(rest, url.searchParams);
}

function routeFromSegments(segments, searchParams) {
  if (segments.length === 0) return { name: 'unknown' };
  if (segments[0] === 'docs') return { name: 'docs' };
  if (segments[0] === 'rounds' && segments[1] === 'current') return { name: 'current-round' };
  if (segments[0] === 'kits' && segments[1]) {
    return { name: 'kit', roundId: decodeURIComponent(segments[1]) };
  }
  if (segments[0] === 'tokens' && segments[1]) {
    return { name: 'revoke-token', tokenId: decodeURIComponent(segments[1]) };
  }
  if (segments[0] === 'tokens') return { name: 'issue-token' };
  if (segments[0] === 'submissions' && segments[1]) {
    return { name: 'receipt', submissionId: decodeURIComponent(segments[1]) };
  }
  if (segments[0] === 'submissions') return { name: 'submit' };
  if (segments[0] === 'benchmarks') return { name: 'submit-benchmark' };
  if (searchParams.get('submission_id') && segments[0] === 'receipt') {
    return { name: 'receipt', submissionId: searchParams.get('submission_id') };
  }
  return { name: 'unknown' };
}

export function createWeeklySelectorHandler({ env = process.env, fetchImpl = fetch } = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');

    const route = parseWeeklySelectorRoute(request);
    if (route.name === 'docs') {
      if (request.method !== 'GET') return methodNotAllowed(response, 'GET');
      return sendJson(response, 200, selectorApiDocumentation());
    }
    const config = resolveSelectorConfig(env);
    if (!config.configured) {
      return sendJson(response, 500, { error: 'Weekly selector service is not configured' });
    }

    try {
      if (route.name === 'current-round') {
        if (request.method !== 'GET') return methodNotAllowed(response, 'GET');
        return await handleCurrentRound({ config, fetchImpl, response });
      }
      if (route.name === 'kit') {
        if (request.method !== 'GET') return methodNotAllowed(response, 'GET');
        return await handleKitRedirect({ config, fetchImpl, response, roundId: route.roundId });
      }
      if (route.name === 'issue-token') {
        if (request.method !== 'POST') return methodNotAllowed(response, 'POST');
        return await handleIssueToken({ config, env, fetchImpl, request, response });
      }
      if (route.name === 'revoke-token') {
        if (request.method !== 'DELETE') return methodNotAllowed(response, 'DELETE');
        return await handleRevokeToken({
          config,
          fetchImpl,
          request,
          response,
          tokenId: route.tokenId,
        });
      }
      if (route.name === 'submit') {
        if (request.method !== 'POST') return methodNotAllowed(response, 'POST');
        return await handleSubmit({ config, fetchImpl, request, response });
      }
      if (route.name === 'submit-benchmark') {
        if (request.method !== 'POST') return methodNotAllowed(response, 'POST');
        return await handleSubmitBenchmark({
          config,
          fetchImpl,
          request,
          response,
        });
      }
      if (route.name === 'receipt') {
        if (request.method !== 'GET') return methodNotAllowed(response, 'GET');
        return await handleReceipt({
          config,
          fetchImpl,
          request,
          response,
          submissionId: route.submissionId,
        });
      }
      return sendJson(response, 404, { error: 'Not found' });
    } catch (error) {
      if (error?.code === 'UNAUTHORIZED') {
        return sendJson(response, 401, { error: 'Authentication required' });
      }
      if (error?.code === 'CONFLICT') {
        return sendJson(response, 409, { error: 'Conflict' });
      }
      if (error?.code === 'VALIDATION') {
        return sendJson(response, 400, { error: 'Invalid request' });
      }
      return sendJson(response, 502, { error: 'Weekly selector service unavailable' });
    }
  };
}

export function selectorApiDocumentation() {
  return {
    schema_version: 'foldarium.weekly-selector-api/v2',
    submission_schema_version: SUBMISSION_SCHEMA_VERSION,
    blindness_attestation_schema_version: BLINDNESS_ATTESTATION_SCHEMA_VERSION,
    prompt_profile: SELECTOR_PROMPT_PROFILE,
    post_close_benchmark_schema_version: SELECTOR_BENCHMARK_SCHEMA_VERSION,
    complete_only: true,
    canonical_json_required: true,
    token_request: {
      exact_keys: [
        'environment',
        'round_id',
        'display_name',
        'method_name',
        'method_version',
        'provider',
        'model_name',
        'model_version',
        'prompt_profile_id',
        'prompt_sha256',
        'tools_sha256',
        'config_sha256',
        'blindness_attestation',
      ],
      blindness_attestation: {
        exact_keys: [
          'schema_version',
          'workspace_policy',
          'network_policy',
          'network_allowlist_sha256',
          'browser_enabled',
          'web_search_enabled',
          'external_retrieval_enabled',
          'shared_cache_enabled',
        ],
        schema_version: BLINDNESS_ATTESTATION_SCHEMA_VERSION,
        workspace_policy: 'verified-kit-only',
        network_policy: ['none', 'provider-api-only'],
        empty_network_allowlist_sha256: EMPTY_NETWORK_ALLOWLIST_SHA256,
        required_disabled_capabilities: [
          'browser_enabled',
          'web_search_enabled',
          'external_retrieval_enabled',
          'shared_cache_enabled',
        ],
      },
    },
    decision_modes: {
      clustered: [
        { selection_kind: 'cluster', required_identity: 'cluster_id' },
        { selection_kind: 'none' },
      ],
      unclustered: [
        { selection_kind: 'exact', required_identity: 'choice_id' },
        { selection_kind: 'none' },
      ],
    },
    legacy_v1: {
      accepted_by_v2_endpoints: false,
      compatibility: 'Use the recovered v1 database RPCs explicitly; v2 never infers v1 decisions.',
    },
    endpoints: {
      current_round: { method: 'GET', path: '/api/weekly-selector/rounds/current' },
      kit: { method: 'GET', path: '/api/weekly-selector/kits/{round_id}' },
      issue_token: {
        method: 'POST',
        path: '/api/weekly-selector/tokens',
        authentication: 'Supabase access token',
      },
      revoke_token: {
        method: 'DELETE',
        path: '/api/weekly-selector/tokens/{token_id}',
        authentication: 'Supabase access token',
      },
      submit: {
        method: 'POST',
        path: '/api/weekly-selector/submissions',
        authentication: 'Foldarium selector bearer token',
      },
      submit_post_close_benchmark: {
        method: 'POST',
        path: '/api/weekly-selector/benchmarks',
        authentication: 'Dedicated server-side benchmark ingest token',
        publication_class: 'post_close_benchmark',
      },
      receipt: {
        method: 'GET',
        path: '/api/weekly-selector/submissions/{submission_id}',
        authentication: 'Foldarium selector bearer token',
      },
    },
  };
}

async function fetchKitDescriptor(fetchImpl, config, roundId, bearerToken = null) {
  const kitRows = await supabaseFetch(
    fetchImpl,
    config,
    '/rest/v1/rpc/get_weekly_selector_kit_descriptor',
    {
      method: 'POST',
      body: {
        p_round_id: roundId,
        p_environment: config.deploymentEnvironment,
      },
      bearerToken,
    },
  );
  return Array.isArray(kitRows) ? kitRows[0] : null;
}

async function handleCurrentRound({ config, fetchImpl, response }) {
  const roundRows = await supabaseFetch(
    fetchImpl,
    config,
    '/rest/v1/rpc/get_current_weekly_quiz_round',
    {
      method: 'POST',
      body: { p_environment: config.deploymentEnvironment },
    },
  );
  const round = Array.isArray(roundRows) ? roundRows[0] : null;
  if (!round) return sendJson(response, 404, { error: 'No active weekly round' });

  const kitRow = await fetchKitDescriptor(fetchImpl, config, round.round_id);
  if (!kitRow) return sendJson(response, 404, { error: 'Selector kit is unavailable' });

  const kit = validateKitDescriptor({
    schema_version: KIT_SCHEMA_VERSION,
    environment: config.deploymentEnvironment,
    round_id: kitRow.round_id,
    blind_manifest_sha256: kitRow.blind_manifest_sha256,
    kit_sha256: kitRow.kit_sha256,
    item_count: kitRow.item_count,
    byte_size: Number(kitRow.byte_size),
    storage_path: kitRow.storage_path,
    created_at: kitRow.created_at,
  });

  return sendJson(response, 200, {
    environment: config.deploymentEnvironment,
    round_id: round.round_id,
    public_status: round.public_status,
    opens_at: round.opens_at,
    closes_at: round.closes_at,
    item_count: round.item_count,
    blind_manifest_sha256: round.blind_manifest_sha256,
    prompt_profile: SELECTOR_PROMPT_PROFILE,
    kit,
  });
}

async function handleKitRedirect({ config, fetchImpl, response, roundId }) {
  if (!isSafeRoundId(roundId)) return sendJson(response, 400, { error: 'Invalid round id' });

  const kitRow = await fetchKitDescriptor(fetchImpl, config, roundId);
  if (!kitRow) return sendJson(response, 404, { error: 'Selector kit is unavailable' });

  const kit = validateKitDescriptor({
    schema_version: KIT_SCHEMA_VERSION,
    environment: config.deploymentEnvironment,
    round_id: kitRow.round_id,
    blind_manifest_sha256: kitRow.blind_manifest_sha256,
    kit_sha256: kitRow.kit_sha256,
    item_count: kitRow.item_count,
    byte_size: Number(kitRow.byte_size),
    storage_path: kitRow.storage_path,
    created_at: kitRow.created_at,
  });

  const encodedPath = kit.storage_path.split('/').map(encodeURIComponent).join('/');
  const downloadUrl = `${config.url}/storage/v1/object/public/${encodedPath}`;
  return sendJson(response, 200, {
    environment: kit.environment,
    round_id: kit.round_id,
    blind_manifest_sha256: kit.blind_manifest_sha256,
    kit_sha256: kit.kit_sha256,
    prompt_profile: SELECTOR_PROMPT_PROFILE,
    descriptor_verified: true,
    download_url: downloadUrl,
  });
}

async function handleIssueToken({ config, fetchImpl, request, response }) {
  const contentType = headerValue(request.headers, 'content-type');
  if (!contentType.startsWith('application/json')) {
    return sendJson(response, 415, { error: 'Content-Type must be application/json' });
  }

  const rawBody = await readBody(request);
  if (rawBody.byteLength > MAX_REQUEST_BODY_BYTES) {
    return sendJson(response, 413, { error: 'Request body is too large' });
  }

  const body = parseJsonObject(rawBody);
  if (!body) return sendJson(response, 400, { error: 'Invalid request' });

  const accessToken = bearerToken(request.headers?.authorization);
  if (!accessToken) return sendJson(response, 401, { error: 'Authentication required' });

  let tokenRequest;
  try {
    tokenRequest = validateTokenRequest(body, {
      environment: config.deploymentEnvironment,
    });
  } catch (error) {
    if (!(error instanceof ContractError)) throw error;
    return sendJson(response, 400, { error: 'Invalid token request' });
  }

  const user = await supabaseFetch(fetchImpl, config, '/auth/v1/user', {
    method: 'GET',
    bearerToken: accessToken,
    publishableKey: config.publishableKey,
  });
  if (!user?.id) return sendJson(response, 401, { error: 'Authentication required' });

  const rawToken = randomBytes(32).toString('base64url');
  const tokenHash = sha256Hex(rawToken);
  const blindnessAttestationSha256 = sha256Hex(tokenRequest.blindness_attestation);

  const tokenRows = await supabaseFetch(
    fetchImpl,
    config,
    '/rest/v1/rpc/issue_weekly_selector_token_v2',
    {
      method: 'POST',
      body: {
        p_environment: tokenRequest.environment,
        p_round_id: tokenRequest.round_id,
        p_display_name: tokenRequest.display_name,
        p_method_name: tokenRequest.method_name,
        p_method_version: tokenRequest.method_version,
        p_provider: tokenRequest.provider,
        p_model_name: tokenRequest.model_name,
        p_model_version: tokenRequest.model_version,
        p_prompt_profile_id: tokenRequest.prompt_profile_id,
        p_prompt_sha256: tokenRequest.prompt_sha256,
        p_tools_sha256: tokenRequest.tools_sha256,
        p_config_sha256: tokenRequest.config_sha256,
        p_blindness_attestation: tokenRequest.blindness_attestation,
        p_blindness_attestation_sha256: blindnessAttestationSha256,
        p_token_hash: tokenHash,
      },
      bearerToken: accessToken,
    },
  );
  const tokenRecord = Array.isArray(tokenRows) ? tokenRows[0] : tokenRows;

  return sendJson(response, 201, {
    token: rawToken,
    token_type: 'Bearer',
    token_id: tokenRecord?.token_id,
    environment: tokenRequest.environment,
    round_id: tokenRequest.round_id,
    expires_at: tokenRecord?.expires_at,
    display_name: tokenRequest.display_name,
    method_name: tokenRequest.method_name,
    method_version: tokenRequest.method_version,
    provider: tokenRequest.provider,
    model_name: tokenRequest.model_name,
    model_version: tokenRequest.model_version,
    prompt_profile_id: tokenRequest.prompt_profile_id,
  });
}

async function handleRevokeToken({ config, fetchImpl, request, response, tokenId }) {
  if (!UUID_RE.test(tokenId || '')) {
    return sendJson(response, 400, { error: 'Invalid token id' });
  }
  const accessToken = bearerToken(request.headers?.authorization);
  if (!accessToken) return sendJson(response, 401, { error: 'Authentication required' });

  const user = await supabaseFetch(fetchImpl, config, '/auth/v1/user', {
    method: 'GET',
    bearerToken: accessToken,
    publishableKey: config.publishableKey,
  });
  if (!user?.id) return sendJson(response, 401, { error: 'Authentication required' });

  const revokedRows = await supabaseFetch(
    fetchImpl,
    config,
    '/rest/v1/rpc/revoke_weekly_selector_token_v2',
    {
      method: 'POST',
      body: {
        p_token_id: tokenId,
        p_environment: config.deploymentEnvironment,
      },
      bearerToken: accessToken,
    },
  );
  const revoked = Array.isArray(revokedRows) ? revokedRows[0] : revokedRows;
  if (!revoked) return sendJson(response, 404, { error: 'Token not found' });
  return sendJson(response, 200, {
    token_id: revoked.token_id,
    revoked_at: revoked.revoked_at,
  });
}

async function fetchScopedRound(fetchImpl, config, environment, roundId, bearerToken = null) {
  const rows = await supabaseFetch(
    fetchImpl,
    config,
    '/rest/v1/rpc/get_weekly_selector_round_v2',
    {
      method: 'POST',
      body: {
        p_environment: environment,
        p_round_id: roundId,
      },
      bearerToken,
    },
  );
  return Array.isArray(rows) ? rows[0] : null;
}

async function handleSubmit({ config, fetchImpl, request, response }) {
  const contentType = headerValue(request.headers, 'content-type');
  if (!contentType.startsWith('application/json')) {
    return sendJson(response, 415, { error: 'Content-Type must be application/json' });
  }

  const rawBody = await readBody(request);
  if (rawBody.byteLength > MAX_REQUEST_BODY_BYTES) {
    return sendJson(response, 413, { error: 'Request body is too large' });
  }

  const body = parseJsonObject(rawBody);
  if (!body) return sendJson(response, 400, { error: 'Invalid request' });

  const bearer = bearerToken(request.headers?.authorization);
  if (!bearer) return sendJson(response, 401, { error: 'Selector token required' });

  if (
    body.schema_version !== SUBMISSION_SCHEMA_VERSION
    || body.environment !== config.deploymentEnvironment
    || typeof body.round_id !== 'string'
    || !ID_RE.test(body.round_id)
    || typeof body.blind_manifest_sha256 !== 'string'
    || !SHA256_RE.test(body.blind_manifest_sha256)
    || typeof body.kit_sha256 !== 'string'
    || !SHA256_RE.test(body.kit_sha256)
  ) {
    return sendJson(response, 400, { error: 'Invalid submission' });
  }

  const round = await fetchScopedRound(
    fetchImpl,
    config,
    body.environment,
    body.round_id,
  );
  if (!round) return sendJson(response, 404, { error: 'Selector round is unavailable' });

  const kitRow = await fetchKitDescriptor(fetchImpl, config, body.round_id);
  if (!kitRow) return sendJson(response, 404, { error: 'Selector kit is unavailable' });

  let normalized;
  const validationContext = {
    environment: config.deploymentEnvironment,
    roundId: round.round_id,
    blindManifestSha256: round.blind_manifest_sha256,
    kitSha256: kitRow.kit_sha256,
    blindManifest: round.blind_manifest,
  };
  try {
    normalized = validateCompleteSubmission(body, validationContext);
    if (rawBody.toString('utf8') !== canonicalJson(normalized)) {
      throw new ContractError('submission body is not canonical JSON');
    }
  } catch (error) {
    if (error instanceof ContractError) {
      return sendJson(response, 400, { error: 'Invalid submission' });
    }
    throw error;
  }

  const payloadDigest = sha256Hex(canonicalJson(normalized));
  const tokenHash = sha256Hex(bearer);

  let receiptRows;
  try {
    receiptRows = await supabaseFetch(
      fetchImpl,
      config,
      '/rest/v1/rpc/submit_weekly_selector_complete_v2',
      {
        method: 'POST',
        body: {
          p_token_hash: tokenHash,
          p_environment: normalized.environment,
          p_round_id: normalized.round_id,
          p_submission_id: normalized.submission_id,
          p_blind_manifest_sha256: normalized.blind_manifest_sha256,
          p_kit_sha256: normalized.kit_sha256,
          p_payload: normalized,
          p_payload_digest: payloadDigest,
        },
        allowConflict: true,
      },
    );
  } catch (error) {
    if (error.code === 'CONFLICT') {
      return sendJson(response, 409, { error: 'Submission id is already bound to a different payload' });
    }
    if (error.code === 'UNAUTHORIZED') {
      return sendJson(response, 401, { error: 'Selector token required' });
    }
    if (error.code === 'VALIDATION') {
      return sendJson(response, 400, { error: 'Invalid submission' });
    }
    throw error;
  }

  const receipt = Array.isArray(receiptRows) ? receiptRows[0] : receiptRows;
  return sendJson(response, receipt?.idempotent ? 200 : 201, sanitizeReceipt(receipt));
}

async function handleSubmitBenchmark({ config, fetchImpl, request, response }) {
  if (!config.serviceRoleKey || !config.benchmarkIngestToken) {
    return sendJson(response, 503, { error: 'Benchmark ingest is disabled' });
  }
  if (!secureTokenEqual(bearerToken(request.headers?.authorization), config.benchmarkIngestToken)) {
    return sendJson(response, 401, { error: 'Benchmark ingest token required' });
  }
  const contentType = headerValue(request.headers, 'content-type');
  if (!contentType.startsWith('application/json')) {
    return sendJson(response, 415, { error: 'Content-Type must be application/json' });
  }
  const rawBody = await readBody(request);
  if (rawBody.byteLength > MAX_BENCHMARK_BODY_BYTES) {
    return sendJson(response, 413, { error: 'Request body is too large' });
  }
  const body = parseJsonObject(rawBody);
  if (
    !body
    || body.schema_version !== SELECTOR_BENCHMARK_SCHEMA_VERSION
    || body.environment !== config.deploymentEnvironment
    || !ID_RE.test(body.round_id || '')
  ) {
    return sendJson(response, 400, { error: 'Invalid benchmark execution' });
  }

  const serviceConfig = {
    ...config,
    publishableKey: config.serviceRoleKey,
  };
  const round = await fetchScopedRound(
    fetchImpl,
    serviceConfig,
    body.environment,
    body.round_id,
    config.serviceRoleKey,
  );
  if (!round || round.public_status !== 'closed') {
    return sendJson(response, 409, { error: 'Round is not closed for benchmarking' });
  }
  const kitRow = await fetchKitDescriptor(
    fetchImpl,
    serviceConfig,
    body.round_id,
    config.serviceRoleKey,
  );
  if (!kitRow) return sendJson(response, 404, { error: 'Selector kit is unavailable' });

  const context = {
    environment: config.deploymentEnvironment,
    roundId: round.round_id,
    blindManifestSha256: round.blind_manifest_sha256,
    kitSha256: kitRow.kit_sha256,
    blindManifest: round.blind_manifest,
  };
  let normalized;
  try {
    normalized = validatePostCloseBenchmark(body, context);
    if (rawBody.toString('utf8') !== canonicalJson(normalized)) {
      throw new ContractError('benchmark body is not canonical JSON');
    }
  } catch (error) {
    if (error instanceof ContractError) {
      return sendJson(response, 400, { error: 'Invalid benchmark execution' });
    }
    throw error;
  }
  const executionSha256 = digestPostCloseBenchmark(normalized, context);
  const payloadDigest = sha256Hex(canonicalJson(normalized.payload));
  const receiptRows = await supabaseFetch(
    fetchImpl,
    serviceConfig,
    '/rest/v1/rpc/register_weekly_selector_benchmark_v1',
    {
      method: 'POST',
      body: {
        p_execution: normalized,
        p_execution_sha256: executionSha256,
        p_payload_digest: payloadDigest,
      },
      bearerToken: config.serviceRoleKey,
      publishableKey: config.serviceRoleKey,
      allowConflict: true,
    },
  );
  const receipt = Array.isArray(receiptRows) ? receiptRows[0] : receiptRows;
  return sendJson(
    response,
    receipt?.idempotent ? 200 : 201,
    sanitizePostCloseBenchmarkReceipt(receipt),
  );
}

async function handleReceipt({ config, fetchImpl, request, response, submissionId }) {
  if (!UUID_RE.test(submissionId || '')) {
    return sendJson(response, 400, { error: 'Invalid submission id' });
  }
  const bearer = bearerToken(request.headers?.authorization);
  if (!bearer) return sendJson(response, 401, { error: 'Selector token required' });

  const receiptRows = await supabaseFetch(
    fetchImpl,
    config,
    '/rest/v1/rpc/get_weekly_selector_receipt_v2',
    {
      method: 'POST',
      body: {
        p_submission_id: submissionId,
        p_token_hash: sha256Hex(bearer),
        p_environment: config.deploymentEnvironment,
      },
    },
  );
  const receipt = Array.isArray(receiptRows) ? receiptRows[0] : null;
  if (!receipt) return sendJson(response, 404, { error: 'Receipt not found' });
  return sendJson(response, 200, sanitizeReceipt(receipt));
}

function sanitizeReceipt(receipt) {
  return {
    submission_id: receipt.submission_id,
    revision_number: receipt.revision_number,
    environment: receipt.environment,
    round_id: receipt.round_id,
    blind_manifest_sha256: receipt.blind_manifest_sha256,
    kit_sha256: receipt.kit_sha256,
    payload_digest: receipt.payload_digest,
    submitted_at: receipt.submitted_at,
  };
}

async function supabaseFetch(fetchImpl, config, path, options = {}) {
  const headers = { apikey: options.publishableKey || config.publishableKey };
  if (options.bearerToken) headers.Authorization = `Bearer ${options.bearerToken}`;

  const upstream = await fetchImpl(`${config.url}${path}`, {
    method: options.method || 'GET',
    headers: {
      ...headers,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      Prefer: options.prefer || 'return=representation',
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  const text = await upstream.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (upstream.ok) return payload;

  const message = typeof payload?.message === 'string' ? payload.message : '';
  if (
    upstream.status === 401
    || upstream.status === 403
    || payload?.code === '42501'
    || message.includes('invalid') && message.includes('token')
    || message.includes('token is expired')
    || message.includes('token is invalid or revoked')
  ) {
    const error = new Error('unauthorized');
    error.code = 'UNAUTHORIZED';
    throw error;
  }
  if (
    upstream.status === 409
    || payload?.code === '23505'
    || message.includes('already bound to a different payload')
  ) {
    const error = new Error('conflict');
    error.code = 'CONFLICT';
    throw error;
  }
  if (
    upstream.status === 400
    || payload?.code === '22023'
    || payload?.code === '23514'
    || message.includes('invalid')
    || message.includes('must include')
  ) {
    const error = new Error('validation');
    error.code = 'VALIDATION';
    throw error;
  }
  throw new Error('upstream unavailable');
}

export function verifySelectorToken(providedToken, storedHash) {
  const providedHash = createHash('sha256').update(String(providedToken)).digest();
  const expectedHash = Buffer.from(String(storedHash), 'hex');
  if (providedHash.length !== expectedHash.length) return false;
  return timingSafeEqual(providedHash, expectedHash);
}

function secureTokenEqual(providedToken, expectedToken) {
  if (!providedToken || !expectedToken) return false;
  const providedHash = createHash('sha256').update(providedToken).digest();
  const expectedHash = createHash('sha256').update(expectedToken).digest();
  return timingSafeEqual(providedHash, expectedHash);
}

function bearerToken(value) {
  if (typeof value !== 'string') return '';
  const match = value.match(/^Bearer\s+(\S+)$/i);
  return match ? match[1] : '';
}

function headerValue(headers, name) {
  if (!headers) return '';
  const direct = headers[name];
  if (typeof direct === 'string') return direct.toLowerCase();
  const found = headers[Object.keys(headers).find(key => key.toLowerCase() === name)];
  return typeof found === 'string' ? found.toLowerCase() : '';
}

async function readBody(request) {
  if (typeof request.body === 'string') return Buffer.from(request.body, 'utf8');
  if (Buffer.isBuffer(request.body)) return request.body;
  if (request.body && typeof request.body === 'object') {
    return Buffer.from(JSON.stringify(request.body), 'utf8');
  }
  return Buffer.alloc(0);
}

function parseJsonObject(rawBody) {
  try {
    const value = JSON.parse(rawBody.toString('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    return value;
  } catch {
    return null;
  }
}

function isSafeRoundId(value) {
  return typeof value === 'string' && ID_RE.test(value);
}

function normalizeDeploymentEnvironment(value) {
  if (value === 'production' || value === 'preview') return value;
  return 'development';
}

function normalizedHttpsUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  try {
    const url = new URL(value.trim());
    const loopbackHttp = url.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !loopbackHttp) || url.username || url.password) return '';
    return url.toString().replace(/\/$/, '');
  } catch {
    return '';
  }
}

function trimmedString(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function methodNotAllowed(response, allow) {
  response.setHeader('Allow', allow);
  return sendJson(response, 405, { error: 'Method not allowed' });
}

function sendJson(response, status, value) {
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  return response.status(status).json(value);
}

export default createWeeklySelectorHandler();
