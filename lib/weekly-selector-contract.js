import { createHash } from 'node:crypto';
import {
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
} from './weekly-selector-prompt.js';

export const LEGACY_KIT_SCHEMA_VERSION = 'foldarium.weekly-selector-kit/v1';
export const LEGACY_SUBMISSION_SCHEMA_VERSION = 'foldarium.selector-submission/v1';
export const KIT_SCHEMA_VERSION = 'foldarium.weekly-selector-kit/v2';
export const SUBMISSION_SCHEMA_VERSION = 'foldarium.selector-submission/v2';
export const BLINDNESS_ATTESTATION_SCHEMA_VERSION = 'foldarium.selector-blindness-attestation/v1';
export const EMPTY_NETWORK_ALLOWLIST_SHA256 = '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945';

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const SHA256_RE = /^[0-9a-f]{64}$/;
export const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
export const STORAGE_PATH_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$/;
export const DEPLOYMENT_ENVIRONMENTS = Object.freeze(['production', 'preview', 'development']);

export const MAX_SUBMISSION_PAYLOAD_BYTES = 65_536;
export const MAX_REQUEST_BODY_BYTES = 131_072;

export const REVEAL_KEY_DENYLIST = Object.freeze([
  'correct',
  'accepted_correct',
  'rmsd',
  'answer',
  'answer_metadata',
  'score',
  'reference',
  'crystal',
  'run_id',
  'sample_id',
  'artifact_sha256',
  'private_index',
  'reveal_manifest',
  'coordinates',
  'user_id',
  'token_hash',
  'participant_hash',
  'display_name_hash',
  'representative_id',
  'representative_choice_id',
  'is_rep',
]);

const KIT_DESCRIPTOR_KEYS = Object.freeze([
  'schema_version',
  'environment',
  'round_id',
  'blind_manifest_sha256',
  'kit_sha256',
  'item_count',
  'byte_size',
  'storage_path',
  'created_at',
]);

const TOKEN_REQUEST_KEYS = Object.freeze([
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
]);

const BLINDNESS_ATTESTATION_KEYS = Object.freeze([
  'schema_version',
  'workspace_policy',
  'network_policy',
  'network_allowlist_sha256',
  'browser_enabled',
  'web_search_enabled',
  'external_retrieval_enabled',
  'shared_cache_enabled',
]);

const SUBMISSION_KEYS = Object.freeze([
  'schema_version',
  'submission_id',
  'environment',
  'round_id',
  'blind_manifest_sha256',
  'kit_sha256',
  'items',
]);

const SUBMISSION_ITEM_KEYS = Object.freeze(['item_id', 'clustered', 'unclustered']);
const NONE_DECISION_KEYS = Object.freeze(['selection_kind']);
const CLUSTER_DECISION_KEYS = Object.freeze(['selection_kind', 'cluster_id']);
const EXACT_DECISION_KEYS = Object.freeze(['selection_kind', 'choice_id']);

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === 'object') {
    return Object.keys(value).sort().reduce((accumulator, key) => {
      accumulator[key] = sortKeys(value[key]);
      return accumulator;
    }, {});
  }
  return value;
}

export function canonicalJson(value) {
  return JSON.stringify(sortKeys(value));
}

export function sha256Hex(value) {
  const bytes = typeof value === 'string' ? value : canonicalJson(value);
  return createHash('sha256').update(bytes, 'utf8').digest('hex');
}

export function normalizeDisplayName(value) {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().replace(/\s+/g, ' ');
  if (!normalized || normalized.length > 80 || Buffer.byteLength(normalized, 'utf8') > 320) return null;
  if (/[\u0000-\u001F\u007F]/.test(normalized)) return null;
  return normalized;
}

export function normalizeMethodField(value, { maxLength = 80 } = {}) {
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  if (!normalized || normalized.length > maxLength || /[\u0000-\u001F\u007F]/.test(normalized)) return null;
  return normalized;
}

function assertExactKeys(object, allowedKeys, label) {
  if (!object || typeof object !== 'object' || Array.isArray(object)) {
    throw new ContractError(`${label} must be an object`);
  }
  const actualKeys = Object.keys(object);
  for (const key of actualKeys) {
    if (!allowedKeys.includes(key)) {
      throw new ContractError(`${label} contains unknown key: ${key}`);
    }
  }
  for (const key of allowedKeys) {
    if (!Object.hasOwn(object, key)) {
      throw new ContractError(`${label} is missing required key: ${key}`);
    }
  }
}

function rejectForbiddenKeys(value, path = 'payload') {
  if (value === null || typeof value !== 'object') return;
  if (Array.isArray(value)) {
    value.forEach((entry, index) => rejectForbiddenKeys(entry, `${path}[${index}]`));
    return;
  }
  for (const [key, nested] of Object.entries(value)) {
    if (REVEAL_KEY_DENYLIST.includes(key)) {
      throw new ContractError(`${path} contains forbidden key: ${key}`);
    }
    rejectForbiddenKeys(nested, `${path}.${key}`);
  }
}

function requireId(value, label) {
  if (typeof value !== 'string' || !ID_RE.test(value)) {
    throw new ContractError(`${label} is invalid`);
  }
  return value;
}

function requireDigest(value, label) {
  if (typeof value !== 'string' || !SHA256_RE.test(value)) {
    throw new ContractError(`${label} must be a lowercase SHA-256 digest`);
  }
  return value;
}

function requireEnvironment(value, label = 'environment') {
  if (!DEPLOYMENT_ENVIRONMENTS.includes(value)) {
    throw new ContractError(`${label} is invalid`);
  }
  return value;
}

export class ContractError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ContractError';
  }
}

export function validateKitDescriptor(descriptor) {
  assertExactKeys(descriptor, KIT_DESCRIPTOR_KEYS, 'kit descriptor');
  rejectForbiddenKeys(descriptor);
  if (descriptor.schema_version !== KIT_SCHEMA_VERSION) {
    throw new ContractError(`unsupported kit schema_version; expected ${KIT_SCHEMA_VERSION}`);
  }
  requireEnvironment(descriptor.environment, 'kit descriptor environment');
  requireId(descriptor.round_id, 'kit descriptor round_id');
  requireDigest(descriptor.blind_manifest_sha256, 'kit descriptor blind_manifest_sha256');
  requireDigest(descriptor.kit_sha256, 'kit descriptor kit_sha256');
  if (!Number.isInteger(descriptor.item_count) || descriptor.item_count <= 0) {
    throw new ContractError('kit descriptor item_count must be a positive integer');
  }
  if (!Number.isInteger(descriptor.byte_size) || descriptor.byte_size <= 0) {
    throw new ContractError('kit descriptor byte_size must be a positive integer');
  }
  if (
    typeof descriptor.storage_path !== 'string'
    || !STORAGE_PATH_RE.test(descriptor.storage_path)
    || descriptor.storage_path.includes('..')
    || descriptor.storage_path.includes('//')
  ) {
    throw new ContractError('kit descriptor storage_path is invalid');
  }
  if (typeof descriptor.created_at !== 'string' || Number.isNaN(Date.parse(descriptor.created_at))) {
    throw new ContractError('kit descriptor created_at must be an ISO timestamp');
  }
  return {
    schema_version: KIT_SCHEMA_VERSION,
    environment: descriptor.environment,
    round_id: descriptor.round_id,
    blind_manifest_sha256: descriptor.blind_manifest_sha256,
    kit_sha256: descriptor.kit_sha256,
    item_count: descriptor.item_count,
    byte_size: descriptor.byte_size,
    storage_path: descriptor.storage_path,
    created_at: descriptor.created_at,
  };
}

export function validateTokenRequest(request, context = {}) {
  assertExactKeys(request, TOKEN_REQUEST_KEYS, 'token request');
  rejectForbiddenKeys(request, 'token request');

  const normalized = {
    environment: requireEnvironment(request.environment),
    round_id: requireId(request.round_id, 'token request round_id'),
    display_name: normalizeDisplayName(request.display_name),
    method_name: normalizeMethodField(request.method_name),
    method_version: normalizeMethodField(request.method_version),
    provider: normalizeMethodField(request.provider),
    model_name: normalizeMethodField(request.model_name),
    model_version: normalizeMethodField(request.model_version),
    prompt_profile_id: normalizeMethodField(request.prompt_profile_id),
    prompt_sha256: requireDigest(request.prompt_sha256, 'token request prompt_sha256'),
    tools_sha256: requireDigest(request.tools_sha256, 'token request tools_sha256'),
    config_sha256: requireDigest(request.config_sha256, 'token request config_sha256'),
    blindness_attestation: validateBlindnessAttestation(request.blindness_attestation),
  };
  if (
    !normalized.display_name
    || !normalized.method_name
    || !normalized.method_version
    || !normalized.provider
    || !normalized.model_name
    || !normalized.model_version
    || normalized.prompt_profile_id !== SELECTOR_PROMPT_PROFILE_ID
  ) {
    throw new ContractError('token request selector identity is invalid');
  }
  if (normalized.prompt_sha256 !== SELECTOR_PROMPT_SHA256) {
    throw new ContractError('token request prompt_sha256 does not match prompt_profile_id');
  }
  if (context.environment && normalized.environment !== context.environment) {
    throw new ContractError('token request environment does not match the deployment');
  }
  return normalized;
}

export function validateBlindnessAttestation(attestation) {
  assertExactKeys(attestation, BLINDNESS_ATTESTATION_KEYS, 'blindness_attestation');
  if (attestation.schema_version !== BLINDNESS_ATTESTATION_SCHEMA_VERSION) {
    throw new ContractError(
      `unsupported blindness_attestation schema_version; expected ${BLINDNESS_ATTESTATION_SCHEMA_VERSION}`,
    );
  }
  if (attestation.workspace_policy !== 'verified-kit-only') {
    throw new ContractError('blindness_attestation workspace_policy is invalid');
  }
  if (!['none', 'provider-api-only'].includes(attestation.network_policy)) {
    throw new ContractError('blindness_attestation network_policy is invalid');
  }
  const networkAllowlistSha256 = requireDigest(
    attestation.network_allowlist_sha256,
    'blindness_attestation network_allowlist_sha256',
  );
  if (
    attestation.network_policy === 'none'
    && networkAllowlistSha256 !== EMPTY_NETWORK_ALLOWLIST_SHA256
  ) {
    throw new ContractError(
      'blindness_attestation network_allowlist_sha256 must identify the canonical empty allowlist',
    );
  }
  if (
    attestation.network_policy === 'provider-api-only'
    && networkAllowlistSha256 === EMPTY_NETWORK_ALLOWLIST_SHA256
  ) {
    throw new ContractError(
      'blindness_attestation provider-api-only network policy requires a non-empty allowlist digest',
    );
  }
  for (const capability of [
    'browser_enabled',
    'web_search_enabled',
    'external_retrieval_enabled',
    'shared_cache_enabled',
  ]) {
    if (attestation[capability] !== false) {
      throw new ContractError(`blindness_attestation ${capability} must be false`);
    }
  }
  return {
    schema_version: BLINDNESS_ATTESTATION_SCHEMA_VERSION,
    workspace_policy: 'verified-kit-only',
    network_policy: attestation.network_policy,
    network_allowlist_sha256: networkAllowlistSha256,
    browser_enabled: false,
    web_search_enabled: false,
    external_retrieval_enabled: false,
    shared_cache_enabled: false,
  };
}

function buildBlindIndex(blindManifest) {
  if (!blindManifest || typeof blindManifest !== 'object' || Array.isArray(blindManifest)) {
    throw new ContractError('blind manifest must be an object');
  }
  const items = blindManifest.items;
  if (!Array.isArray(items) || items.length === 0) {
    throw new ContractError('blind manifest items must be a non-empty array');
  }
  const index = new Map();
  for (const item of items) {
    if (!item || typeof item !== 'object') {
      throw new ContractError('blind manifest item is invalid');
    }
    const itemId = requireId(item.id, 'blind manifest item id');
    if (index.has(itemId)) {
      throw new ContractError(`blind manifest contains duplicate item id: ${itemId}`);
    }
    if (!Array.isArray(item.choices)) {
      throw new ContractError(`blind manifest choices are invalid for item ${itemId}`);
    }
    const choiceIds = new Set();
    const clusterIds = new Set();
    for (const choice of item.choices) {
      if (!choice || typeof choice !== 'object') {
        throw new ContractError(`blind manifest choice is invalid for item ${itemId}`);
      }
      const choiceId = requireId(choice.id, 'blind manifest choice id');
      if (choiceIds.has(choiceId)) {
        throw new ContractError(`blind manifest contains duplicate choice id for item ${itemId}`);
      }
      choiceIds.add(choiceId);
      if (choice.cluster_id != null) {
        clusterIds.add(requireId(choice.cluster_id, 'blind manifest cluster_id'));
      }
    }
    index.set(itemId, { choiceIds, clusterIds });
  }
  return index;
}

function validateDecision(decision, mode, manifestItem, itemId, path) {
  if (!decision || typeof decision !== 'object' || Array.isArray(decision)) {
    throw new ContractError(`${path} must be an object`);
  }
  if (decision.selection_kind === 'none') {
    assertExactKeys(decision, NONE_DECISION_KEYS, path);
    return { selection_kind: 'none' };
  }
  if (mode === 'clustered' && decision.selection_kind === 'cluster') {
    assertExactKeys(decision, CLUSTER_DECISION_KEYS, path);
    const clusterId = requireId(decision.cluster_id, `${path}.cluster_id`);
    if (!manifestItem.clusterIds.has(clusterId)) {
      throw new ContractError(`cluster_id is not valid for item ${itemId}`);
    }
    return { selection_kind: 'cluster', cluster_id: clusterId };
  }
  if (mode === 'unclustered' && decision.selection_kind === 'exact') {
    assertExactKeys(decision, EXACT_DECISION_KEYS, path);
    const choiceId = requireId(decision.choice_id, `${path}.choice_id`);
    if (!manifestItem.choiceIds.has(choiceId)) {
      throw new ContractError(`choice_id is not valid for item ${itemId}`);
    }
    return { selection_kind: 'exact', choice_id: choiceId };
  }
  throw new ContractError(`${path}.selection_kind is invalid`);
}

function requireSubmissionContext(context) {
  if (!context || typeof context !== 'object') {
    throw new ContractError('trusted submission context is required');
  }
  return {
    environment: requireEnvironment(context.environment, 'context environment'),
    roundId: requireId(context.roundId, 'context round_id'),
    blindManifestSha256: requireDigest(
      context.blindManifestSha256,
      'context blind_manifest_sha256',
    ),
    kitSha256: requireDigest(context.kitSha256, 'context kit_sha256'),
    blindIndex: buildBlindIndex(context.blindManifest),
  };
}

export function validateCompleteSubmission(submission, context) {
  assertExactKeys(submission, SUBMISSION_KEYS, 'submission');
  rejectForbiddenKeys(submission);
  const trusted = requireSubmissionContext(context);

  if (submission.schema_version !== SUBMISSION_SCHEMA_VERSION) {
    throw new ContractError(`unsupported submission schema_version; expected ${SUBMISSION_SCHEMA_VERSION}`);
  }
  if (typeof submission.submission_id !== 'string' || !UUID_RE.test(submission.submission_id)) {
    throw new ContractError('submission_id must be a canonical lowercase UUID');
  }
  requireEnvironment(submission.environment, 'submission environment');
  requireId(submission.round_id, 'submission round_id');
  requireDigest(submission.blind_manifest_sha256, 'submission blind_manifest_sha256');
  requireDigest(submission.kit_sha256, 'submission kit_sha256');
  if (submission.environment !== trusted.environment) {
    throw new ContractError('submission environment does not match the deployment');
  }
  if (submission.round_id !== trusted.roundId) {
    throw new ContractError('submission round_id does not match the requested round');
  }
  if (submission.blind_manifest_sha256 !== trusted.blindManifestSha256) {
    throw new ContractError('submission blind_manifest_sha256 does not match the round');
  }
  if (submission.kit_sha256 !== trusted.kitSha256) {
    throw new ContractError('submission kit_sha256 does not match the round kit');
  }
  if (!Array.isArray(submission.items) || submission.items.length === 0) {
    throw new ContractError('submission items must be a non-empty array');
  }
  if (submission.items.length !== trusted.blindIndex.size) {
    throw new ContractError('submission must include exactly one decision pair per round item');
  }

  const seenItems = new Set();
  const normalizedItems = [];
  for (const [index, item] of submission.items.entries()) {
    assertExactKeys(item, SUBMISSION_ITEM_KEYS, `submission.items[${index}]`);
    const itemId = requireId(item.item_id, `submission.items[${index}].item_id`);
    if (seenItems.has(itemId)) {
      throw new ContractError(`duplicate submission item_id: ${itemId}`);
    }
    seenItems.add(itemId);
    const manifestItem = trusted.blindIndex.get(itemId);
    if (!manifestItem) {
      throw new ContractError(`submission item_id is not in the blind manifest: ${itemId}`);
    }
    normalizedItems.push({
      item_id: itemId,
      clustered: validateDecision(
        item.clustered,
        'clustered',
        manifestItem,
        itemId,
        `submission.items[${index}].clustered`,
      ),
      unclustered: validateDecision(
        item.unclustered,
        'unclustered',
        manifestItem,
        itemId,
        `submission.items[${index}].unclustered`,
      ),
    });
  }

  for (const itemId of trusted.blindIndex.keys()) {
    if (!seenItems.has(itemId)) {
      throw new ContractError(`submission is missing round item: ${itemId}`);
    }
  }

  const normalized = {
    schema_version: SUBMISSION_SCHEMA_VERSION,
    submission_id: submission.submission_id,
    environment: submission.environment,
    round_id: submission.round_id,
    blind_manifest_sha256: submission.blind_manifest_sha256,
    kit_sha256: submission.kit_sha256,
    items: normalizedItems.sort((left, right) => (
      left.item_id < right.item_id ? -1 : left.item_id > right.item_id ? 1 : 0
    )),
  };
  const canonical = canonicalJson(normalized);
  if (Buffer.byteLength(canonical, 'utf8') > MAX_SUBMISSION_PAYLOAD_BYTES) {
    throw new ContractError(`submission payload exceeds ${MAX_SUBMISSION_PAYLOAD_BYTES} bytes`);
  }
  if (canonicalJson(submission) !== canonical) {
    throw new ContractError('submission payload is not in canonical item order');
  }
  return normalized;
}

export function canonicalSubmissionJson(submission, context) {
  return canonicalJson(validateCompleteSubmission(submission, context));
}

export function digestSubmission(submission, context) {
  return sha256Hex(canonicalSubmissionJson(submission, context));
}
