import {
  ContractError,
  ID_RE,
  SHA256_RE,
  UUID_RE,
  canonicalJson,
  sha256Hex,
  validateBlindnessAttestation,
  validateCompleteSubmission,
} from './weekly-selector-contract.js';
import {
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
} from './weekly-selector-prompt.js';

export const SELECTOR_BENCHMARK_SCHEMA_VERSION =
  'foldarium.selector-post-close-benchmark/v1';
export const SELECTOR_BENCHMARK_RECEIPT_SCHEMA_VERSION =
  'foldarium.selector-post-close-benchmark-receipt/v1';

const EXECUTION_KEYS = new Set([
  'schema_version',
  'execution_id',
  'supersedes_execution_id',
  'run_class',
  'environment',
  'round_id',
  'blind_manifest_sha256',
  'kit_sha256',
  'display_name',
  'method_name',
  'method_version',
  'provider',
  'engine',
  'model',
  'provenance',
  'blindness_attestation',
  'blindness_attestation_sha256',
  'usage',
  'started_at',
  'finished_at',
  'reasoning_trace_retained',
  'output_sha256',
  'payload',
]);
const ENGINE_KEYS = new Set(['name', 'version', 'run_id', 'session_id']);
const MODEL_KEYS = new Set([
  'requested_id',
  'observed_ids',
  'requested_effort',
  'applied_effort',
  'effort_reporting',
]);
const PROVENANCE_KEYS = new Set([
  'prompt_profile_id',
  'prompt_sha256',
  'input_manifest_sha256',
  'tools_sha256',
  'config_sha256',
  'runtime_sha256',
]);
const USAGE_KEYS = new Set([
  'input_tokens',
  'output_tokens',
  'cache_read_tokens',
  'cache_creation_tokens',
  'reasoning_tokens',
  'cost_usd',
  'duration_ms',
]);
const ENVIRONMENTS = new Set(['production', 'preview', 'development']);
const EFFORTS = new Set(['default', 'low', 'medium', 'high', 'max']);
const EFFORT_REPORTING = new Set(['reported', 'not_exposed']);

export function validatePostCloseBenchmark(raw, context) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new ContractError('benchmark execution must be an object');
  }
  exactKeys(raw, EXECUTION_KEYS, 'benchmark execution');
  if (raw.schema_version !== SELECTOR_BENCHMARK_SCHEMA_VERSION) {
    throw new ContractError('benchmark schema_version is invalid');
  }
  if (!UUID_RE.test(raw.execution_id || '')) {
    throw new ContractError('benchmark execution_id must be a canonical UUID');
  }
  if (raw.run_class !== 'post_close_benchmark') {
    throw new ContractError('benchmark run_class must be post_close_benchmark');
  }
  if (!ENVIRONMENTS.has(raw.environment) || raw.environment !== context?.environment) {
    throw new ContractError('benchmark environment does not match deployment');
  }
  if (!ID_RE.test(raw.round_id || '') || raw.round_id !== context?.roundId) {
    throw new ContractError('benchmark round_id does not match round');
  }
  requireBoundDigest(
    raw.blind_manifest_sha256,
    context?.blindManifestSha256,
    'blind_manifest_sha256',
  );
  requireBoundDigest(raw.kit_sha256, context?.kitSha256, 'kit_sha256');

  const normalized = {
    schema_version: SELECTOR_BENCHMARK_SCHEMA_VERSION,
    execution_id: raw.execution_id,
    supersedes_execution_id: raw.supersedes_execution_id == null
      ? null
      : canonicalUuid(raw.supersedes_execution_id, 'supersedes_execution_id'),
    run_class: 'post_close_benchmark',
    environment: raw.environment,
    round_id: raw.round_id,
    blind_manifest_sha256: raw.blind_manifest_sha256,
    kit_sha256: raw.kit_sha256,
    display_name: normalizedText(raw.display_name, 'display_name'),
    method_name: normalizedText(raw.method_name, 'method_name'),
    method_version: normalizedText(raw.method_version, 'method_version'),
    provider: normalizedText(raw.provider, 'provider'),
    engine: normalizeEngine(raw.engine),
    model: normalizeModel(raw.model),
    provenance: normalizeProvenance(raw.provenance),
    blindness_attestation: validateBlindnessAttestation(raw.blindness_attestation),
    blindness_attestation_sha256: requiredDigest(
      raw.blindness_attestation_sha256,
      'blindness_attestation_sha256',
    ),
    usage: normalizeUsage(raw.usage),
    started_at: normalizedTimestamp(raw.started_at, 'started_at'),
    finished_at: normalizedTimestamp(raw.finished_at, 'finished_at'),
    reasoning_trace_retained: raw.reasoning_trace_retained,
    output_sha256: requiredDigest(raw.output_sha256, 'output_sha256'),
    payload: validateCompleteSubmission(raw.payload, {
      environment: context.environment,
      roundId: context.roundId,
      blindManifestSha256: context.blindManifestSha256,
      kitSha256: context.kitSha256,
      blindManifest: context.blindManifest,
    }),
  };
  if (normalized.reasoning_trace_retained !== false) {
    throw new ContractError('benchmark reasoning_trace_retained must be false');
  }
  if (
    normalized.blindness_attestation_sha256
    !== sha256Hex(canonicalJson(normalized.blindness_attestation))
  ) {
    throw new ContractError('benchmark blindness attestation digest is inconsistent');
  }
  if (Date.parse(normalized.finished_at) < Date.parse(normalized.started_at)) {
    throw new ContractError('benchmark finished_at precedes started_at');
  }
  if (normalized.payload.submission_id !== normalized.execution_id) {
    throw new ContractError('benchmark payload submission_id must equal execution_id');
  }
  if (normalized.supersedes_execution_id === normalized.execution_id) {
    throw new ContractError('benchmark execution cannot supersede itself');
  }
  return normalized;
}

export function digestPostCloseBenchmark(execution, context) {
  return sha256Hex(canonicalJson(validatePostCloseBenchmark(execution, context)));
}

export function sanitizePostCloseBenchmarkReceipt(row) {
  if (!row || typeof row !== 'object') {
    throw new ContractError('benchmark receipt is missing');
  }
  return {
    schema_version: SELECTOR_BENCHMARK_RECEIPT_SCHEMA_VERSION,
    execution_id: row.execution_id,
    run_class: 'post_close_benchmark',
    environment: row.environment,
    round_id: row.round_id,
    execution_sha256: row.execution_sha256,
    payload_digest: row.payload_digest,
    accepted_at: row.accepted_at,
    idempotent: row.idempotent === true,
  };
}

function normalizeEngine(raw) {
  object(raw, 'engine');
  exactKeys(raw, ENGINE_KEYS, 'engine');
  return {
    name: normalizedText(raw.name, 'engine.name'),
    version: normalizedText(raw.version, 'engine.version'),
    run_id: optionalText(raw.run_id, 'engine.run_id'),
    session_id: optionalText(raw.session_id, 'engine.session_id'),
  };
}

function normalizeModel(raw) {
  object(raw, 'model');
  exactKeys(raw, MODEL_KEYS, 'model');
  const requestedEffort = normalizedText(raw.requested_effort, 'model.requested_effort');
  const effortReporting = normalizedText(raw.effort_reporting, 'model.effort_reporting');
  if (!EFFORTS.has(requestedEffort) || !EFFORT_REPORTING.has(effortReporting)) {
    throw new ContractError('benchmark model effort provenance is invalid');
  }
  const appliedEffort = raw.applied_effort == null
    ? null
    : normalizedText(raw.applied_effort, 'model.applied_effort');
  if (
    appliedEffort != null && !EFFORTS.has(appliedEffort)
    || effortReporting === 'reported' && appliedEffort == null
    || effortReporting === 'not_exposed' && appliedEffort != null
  ) {
    throw new ContractError('benchmark applied effort provenance is inconsistent');
  }
  if (!Array.isArray(raw.observed_ids) || raw.observed_ids.length !== 1) {
    throw new ContractError('benchmark model.observed_ids must contain exactly one model');
  }
  const observedIds = raw.observed_ids.map((value, index) => (
    normalizedText(value, `model.observed_ids[${index}]`)
  ));
  if (
    new Set(observedIds).size !== observedIds.length
    || [...observedIds].sort().some((value, index) => value !== observedIds[index])
  ) {
    throw new ContractError('benchmark model.observed_ids must be sorted and unique');
  }
  return {
    requested_id: normalizedText(raw.requested_id, 'model.requested_id'),
    observed_ids: observedIds,
    requested_effort: requestedEffort,
    applied_effort: appliedEffort,
    effort_reporting: effortReporting,
  };
}

function normalizeProvenance(raw) {
  object(raw, 'provenance');
  exactKeys(raw, PROVENANCE_KEYS, 'provenance');
  if (raw.prompt_profile_id !== SELECTOR_PROMPT_PROFILE_ID) {
    throw new ContractError('benchmark prompt_profile_id is invalid');
  }
  if (raw.prompt_sha256 !== SELECTOR_PROMPT_SHA256) {
    throw new ContractError('benchmark prompt_sha256 does not match prompt profile');
  }
  return {
    prompt_profile_id: SELECTOR_PROMPT_PROFILE_ID,
    prompt_sha256: SELECTOR_PROMPT_SHA256,
    input_manifest_sha256: requiredDigest(
      raw.input_manifest_sha256,
      'provenance.input_manifest_sha256',
    ),
    tools_sha256: requiredDigest(raw.tools_sha256, 'provenance.tools_sha256'),
    config_sha256: requiredDigest(raw.config_sha256, 'provenance.config_sha256'),
    runtime_sha256: requiredDigest(raw.runtime_sha256, 'provenance.runtime_sha256'),
  };
}

function normalizeUsage(raw) {
  object(raw, 'usage');
  exactKeys(raw, USAGE_KEYS, 'usage');
  return Object.fromEntries([...USAGE_KEYS].map(key => {
    const value = raw[key];
    if (value == null) return [key, null];
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
      throw new ContractError(`benchmark usage.${key} must be null or non-negative`);
    }
    if (key !== 'cost_usd' && !Number.isInteger(value)) {
      throw new ContractError(`benchmark usage.${key} must be an integer`);
    }
    return [key, value];
  }));
}

function exactKeys(value, allowed, label) {
  object(value, label);
  const keys = Object.keys(value);
  const unknown = keys.filter(key => !allowed.has(key));
  const missing = [...allowed].filter(key => !Object.hasOwn(value, key));
  if (unknown.length || missing.length) {
    throw new ContractError(`${label} keys are not exact`);
  }
}

function object(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ContractError(`${label} must be an object`);
  }
}

function normalizedText(value, label) {
  if (typeof value !== 'string') throw new ContractError(`${label} must be text`);
  const normalized = value.trim().replace(/\s+/g, ' ');
  if (!normalized || normalized.length > 160 || /[\u0000-\u001f\u007f]/.test(normalized)) {
    throw new ContractError(`${label} is invalid`);
  }
  return normalized;
}

function optionalText(value, label) {
  return value == null ? null : normalizedText(value, label);
}

function requiredDigest(value, label) {
  if (typeof value !== 'string' || !SHA256_RE.test(value)) {
    throw new ContractError(`${label} must be a lowercase SHA-256`);
  }
  return value;
}

function requireBoundDigest(value, expected, label) {
  requiredDigest(value, label);
  if (value !== expected) throw new ContractError(`benchmark ${label} does not match`);
}

function normalizedTimestamp(value, label) {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) {
    throw new ContractError(`benchmark ${label} is invalid`);
  }
  const canonical = new Date(value).toISOString();
  if (value !== canonical) {
    throw new ContractError(`benchmark ${label} must be canonical UTC`);
  }
  return canonical;
}

function canonicalUuid(value, label) {
  if (typeof value !== 'string' || !UUID_RE.test(value)) {
    throw new ContractError(`benchmark ${label} must be a canonical UUID`);
  }
  return value;
}
