import { createHash } from 'node:crypto';

import {
  ContractError,
  ID_RE,
  SHA256_RE,
  sha256Hex,
  validateBlindnessAttestation,
} from './weekly-selector-contract.js';
import {
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
} from './weekly-selector-prompt.js';

export const SELECTOR_RESULTS_FORMAT_VERSION = 'foldarium.weekly-selector-results/v2';
export const SMINA_DISPLAY_NAME = 'Smina';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const FORBIDDEN_RESULT_KEY = /^(?:(?:auth_)?user_id|auth_id|(?:access_)?token(?:_.+)?|submission(?:_.+)?|identity_id|choice_id|cluster_id|payload|decisions|private_.+|run_id|sample_id)$/i;
const DIGEST_FIELDS = ['prompt_sha256', 'tools_sha256', 'config_sha256'];
const BLINDNESS_ATTESTATION_FIELDS = [
  'blindness_attestation',
  'blindness_attestation_sha256',
];

export class WeeklySelectorResultsError extends Error {
  constructor(message) {
    super(message);
    this.name = 'WeeklySelectorResultsError';
  }
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => (
      `${canonicalString(key)}:${canonicalJson(value[key])}`
    )).join(',')}}`;
  }
  if (typeof value === 'string') return canonicalString(value);
  const serialized = JSON.stringify(value);
  if (serialized === undefined || (typeof value === 'number' && !Number.isFinite(value))) {
    throw new WeeklySelectorResultsError('manifest contains a non-canonical value');
  }
  return serialized;
}

export function manifestSha256(manifest) {
  return createHash('sha256').update(canonicalJson(manifest), 'utf8').digest('hex');
}

export function buildSelectorAnswerKeys(blindManifest, revealManifest, itemCount) {
  const total = positiveInt(itemCount, 'item_count');
  const blindItems = parseBlindManifest(blindManifest);
  const revealItems = parseRevealManifest(revealManifest);
  if (blindItems.size !== total) throw new WeeklySelectorResultsError('blind item_count mismatch');
  if (revealItems.size !== total) throw new WeeklySelectorResultsError('reveal item_count mismatch');

  const joined = new Map();
  for (const [itemId, blindItem] of blindItems.entries()) {
    const revealItem = revealItems.get(itemId);
    if (!revealItem || revealItem.choices.size !== blindItem.choices.size) {
      throw new WeeklySelectorResultsError('blind reveal item mismatch');
    }

    const choices = new Map();
    const acceptedByCluster = new Map([...blindItem.clusters.keys()].map(clusterId => [clusterId, false]));
    let hasRawCorrect = false;
    for (const [choiceId, blindChoice] of blindItem.choices.entries()) {
      const revealChoice = revealItem.choices.get(choiceId);
      if (!revealChoice) throw new WeeklySelectorResultsError('blind reveal choice mismatch');
      hasRawCorrect ||= revealChoice.correct;
      acceptedByCluster.set(
        blindChoice.clusterId,
        acceptedByCluster.get(blindChoice.clusterId) || revealChoice.acceptedCorrect,
      );
      choices.set(choiceId, {
        ...blindChoice,
        correct: revealChoice.correct,
        acceptedCorrect: revealChoice.acceptedCorrect,
      });
    }
    for (const choiceId of revealItem.choices.keys()) {
      if (!blindItem.choices.has(choiceId)) {
        throw new WeeklySelectorResultsError('blind reveal choice mismatch');
      }
    }

    joined.set(itemId, {
      choices,
      clusters: blindItem.clusters,
      acceptedByCluster,
      hasRawCorrect,
      hasAcceptedCorrectCluster: [...acceptedByCluster.values()].some(Boolean),
    });
  }
  for (const itemId of revealItems.keys()) {
    if (!blindItems.has(itemId)) throw new WeeklySelectorResultsError('blind reveal item mismatch');
  }
  return joined;
}

export function buildSminaSubmission(answerKeys) {
  if (!(answerKeys instanceof Map) || !answerKeys.size) {
    throw new WeeklySelectorResultsError('Smina answer keys are missing');
  }
  const items = [];
  let scoringFunction = null;
  for (const [itemId, answerKey] of [...answerKeys.entries()].sort(([left], [right]) => (
    compareText(left, right)
  ))) {
    let selected = null;
    for (const [choiceId, choice] of answerKey.choices.entries()) {
      const score = choice.sminaScore;
      if (
        !score
        || score.metric !== 'smina_affinity'
        || typeof score.value !== 'number'
        || !Number.isFinite(score.value)
        || score.units !== 'kcal/mol'
        || score.protocol !== 'score_only'
        || typeof score.scoring_function !== 'string'
        || !score.scoring_function.trim()
      ) {
        throw new WeeklySelectorResultsError('Smina score or provenance is invalid');
      }
      if (scoringFunction == null) scoringFunction = score.scoring_function;
      if (score.scoring_function !== scoringFunction) {
        throw new WeeklySelectorResultsError('Smina scoring provenance is inconsistent');
      }
      if (
        selected == null
        || score.value < selected.value
        || (score.value === selected.value && choiceId < selected.choiceId)
      ) {
        selected = { choiceId, clusterId: choice.clusterId, value: score.value };
      }
    }
    if (!selected?.clusterId) throw new WeeklySelectorResultsError('Smina selection is incomplete');
    items.push({
      item_id: itemId,
      clustered: { selection_kind: 'cluster', cluster_id: selected.clusterId },
      unclustered: { selection_kind: 'exact', choice_id: selected.choiceId },
    });
  }
  return {
    identity: {
      display_name: SMINA_DISPLAY_NAME,
      method_name: 'smina-affinity',
      method_version: 'score-only/v1',
      provider: 'smina',
      model_name: 'smina',
      model_version: scoringFunction,
    },
    participantType: 'synthetic',
    items,
  };
}

export function scoreSelectorSubmission(items, answerKeys, itemCount) {
  if (!Array.isArray(items)) throw new WeeklySelectorResultsError('submission items must be an array');
  const total = positiveInt(itemCount, 'item_count');
  const seenItems = new Set();
  let clusteredCorrect = 0;
  let unclusteredCorrect = 0;

  for (const item of items) {
    exactKeys(item, ['item_id', 'clustered', 'unclustered'], 'submission item');
    const itemId = requiredId(item.item_id, 'submission item_id');
    if (seenItems.has(itemId)) throw new WeeklySelectorResultsError('duplicate submission item');
    seenItems.add(itemId);
    const answerKey = answerKeys.get(itemId);
    if (!answerKey) throw new WeeklySelectorResultsError('unknown submission item');

    const clustered = parseClusteredDecision(item.clustered, answerKey);
    const unclustered = parseUnclusteredDecision(item.unclustered, answerKey);
    if (clustered.selectionKind === 'none') {
      if (!answerKey.hasAcceptedCorrectCluster) clusteredCorrect += 1;
    } else if (answerKey.acceptedByCluster.get(clustered.clusterId) === true) {
      clusteredCorrect += 1;
    }
    if (unclustered.selectionKind === 'none') {
      if (!answerKey.hasRawCorrect) unclusteredCorrect += 1;
    } else if (answerKey.choices.get(unclustered.choiceId)?.correct === true) {
      unclusteredCorrect += 1;
    }
  }
  if (seenItems.size !== total) {
    throw new WeeklySelectorResultsError('submission item_count mismatch');
  }
  return { clusteredCorrect, unclusteredCorrect, answered: seenItems.size };
}

export function scoreWeeklySelectorResults({
  roundId,
  itemCount,
  blindManifest,
  revealManifest,
  submissions = [],
  includeSmina = true,
} = {}) {
  requiredId(roundId, 'round_id');
  const total = positiveInt(itemCount, 'item_count');
  if (!Array.isArray(submissions)) throw new WeeklySelectorResultsError('submissions are invalid');
  const answerKeys = buildSelectorAnswerKeys(blindManifest, revealManifest, total);
  const participants = submissions.map(submission => normalizedParticipant(
    submission,
    submission.participantType || 'selector',
  ));
  if (includeSmina) {
    const smina = buildSminaSubmission(answerKeys);
    participants.push({
      identity: smina.identity,
      participantType: smina.participantType,
      items: smina.items,
    });
  }

  const rows = participants.map(participant => {
    const score = scoreSelectorSubmission(participant.items, answerKeys, total);
    return {
      participant_type: participant.participantType,
      identity: participant.identity,
      clustered: scoreTrack(score.clusteredCorrect, total),
      unclustered: scoreTrack(score.unclusteredCorrect, total),
    };
  });
  rankTrack(rows, 'clustered');
  rankTrack(rows, 'unclustered');
  rows.sort(compareOverallRows);

  const result = {
    format_version: SELECTOR_RESULTS_FORMAT_VERSION,
    round_id: roundId,
    item_count: total,
    participant_count: rows.length,
    selector_count: participants.filter(row => row.participantType === 'selector').length,
    post_close_benchmark_count: participants.filter(
      row => row.participantType === 'post_close_benchmark',
    ).length,
    rows,
    questions: buildQuestionResults(answerKeys, participants),
  };
  assertSanitizedSelectorResult(result);
  return result;
}

export function verifyRevealedSelectorRound(round) {
  if (!round || typeof round !== 'object' || Array.isArray(round)) {
    throw new WeeklySelectorResultsError('round is missing');
  }
  const roundId = requiredId(round.round_id, 'round_id');
  if (round.status !== 'revealed' && round.public_status !== 'revealed') {
    throw new WeeklySelectorResultsError('round is not revealed');
  }
  if (round.revealed_at == null) throw new WeeklySelectorResultsError('round revealed_at is missing');
  if (!round.blind_manifest || !round.reveal_manifest) {
    throw new WeeklySelectorResultsError('revealed manifests are missing');
  }
  const blindDigest = requiredSha256(round.blind_manifest_sha256, 'blind manifest sha256');
  const revealDigest = requiredSha256(round.reveal_manifest_sha256, 'reveal manifest sha256');
  if (manifestSha256(round.blind_manifest) !== blindDigest) {
    throw new WeeklySelectorResultsError('blind manifest digest is inconsistent');
  }
  if (manifestSha256(round.reveal_manifest) !== revealDigest) {
    throw new WeeklySelectorResultsError('reveal manifest digest is inconsistent');
  }
  if (
    round.blind_manifest.round_id !== roundId
    || round.reveal_manifest.round_id !== roundId
    || round.reveal_manifest.blind_manifest_sha256 !== blindDigest
  ) {
    throw new WeeklySelectorResultsError('revealed manifest binding is inconsistent');
  }
  const itemCount = positiveInt(round.item_count, 'item_count');
  buildSelectorAnswerKeys(round.blind_manifest, round.reveal_manifest, itemCount);
  return {
    roundId,
    itemCount,
    blindManifest: round.blind_manifest,
    revealManifest: round.reveal_manifest,
    blindManifestSha256: blindDigest,
    revealManifestSha256: revealDigest,
    environment: requiredEnvironment(round.environment),
  };
}

export function verifyKitCatalogRow(catalogRow, live) {
  if (!catalogRow || typeof catalogRow !== 'object' || Array.isArray(catalogRow)) {
    throw new WeeklySelectorResultsError('kit catalog row is missing');
  }
  if (catalogRow.round_id !== live.roundId) {
    throw new WeeklySelectorResultsError('kit catalog round_id mismatch');
  }
  requiredSha256(catalogRow.kit_sha256, 'kit sha256');
  if (requiredSha256(catalogRow.blind_manifest_sha256, 'kit blind manifest sha256')
      !== live.blindManifestSha256) {
    throw new WeeklySelectorResultsError('kit catalog blind digest mismatch');
  }
  if (positiveInt(catalogRow.item_count, 'kit item_count') !== live.itemCount) {
    throw new WeeklySelectorResultsError('kit catalog item_count mismatch');
  }
  return catalogRow;
}

export function normalizeLatestSubmissionRows(rows, roundId, environment = null) {
  if (!Array.isArray(rows)) throw new WeeklySelectorResultsError('submission rows are invalid');
  return rows.map((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      throw new WeeklySelectorResultsError('submission row is invalid');
    }
    if (row.round_id !== roundId) throw new WeeklySelectorResultsError('submission round_id mismatch');
    if (environment && row.environment !== environment) {
      throw new WeeklySelectorResultsError('submission environment mismatch');
    }
    const sourceIdentity = row.weekly_selector_identities ?? row.identity ?? row;
    const payload = row.payload;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)
        || !Array.isArray(payload.items) || !payload.items.length) {
      throw new WeeklySelectorResultsError(`submission payload is invalid at row ${index}`);
    }
    return {
      identity: normalizeIdentity(sourceIdentity, { requireBlindnessAttestation: true }),
      items: payload.items,
    };
  });
}

export function normalizePostCloseBenchmarkRows(rows, roundId, environment = null) {
  if (!Array.isArray(rows)) throw new WeeklySelectorResultsError('benchmark rows are invalid');
  return rows.map((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      throw new WeeklySelectorResultsError('benchmark row is invalid');
    }
    if (row.run_class !== 'post_close_benchmark') {
      throw new WeeklySelectorResultsError('benchmark run_class is invalid');
    }
    const payload = row.payload;
    if (!payload || typeof payload !== 'object' || !Array.isArray(payload.items)) {
      throw new WeeklySelectorResultsError(`benchmark payload is invalid at row ${index}`);
    }
    if (payload.round_id !== roundId || environment && payload.environment !== environment) {
      throw new WeeklySelectorResultsError('benchmark payload binding is invalid');
    }
    if (!Array.isArray(row.observed_model_ids) || row.observed_model_ids.length !== 1) {
      throw new WeeklySelectorResultsError('benchmark observed model is invalid');
    }
    return {
      participantType: 'post_close_benchmark',
      identity: normalizeIdentity({
        ...row,
        model_name: row.requested_model_id,
        model_version: row.observed_model_ids[0],
        benchmark: {
          run_class: row.run_class,
          requested_model_id: row.requested_model_id,
          observed_model_id: row.observed_model_ids[0],
          requested_effort: row.requested_effort,
          applied_effort: row.applied_effort,
          effort_reporting: row.effort_reporting,
          input_manifest_sha256: row.input_manifest_sha256,
          runtime_sha256: row.runtime_sha256,
          execution_sha256: row.execution_sha256,
        },
      }, { requireBlindnessAttestation: true }),
      items: payload.items,
    };
  });
}

export function assertSanitizedSelectorResult(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new WeeklySelectorResultsError('result is invalid');
  }
  exactKeys(
    result,
    [
      'format_version',
      'round_id',
      'item_count',
      'participant_count',
      'selector_count',
      'post_close_benchmark_count',
      'rows',
      'questions',
    ],
    'result',
  );
  walkSanitized(result);
  if (!Array.isArray(result.rows) || !Array.isArray(result.questions)) {
    throw new WeeklySelectorResultsError('result collections are invalid');
  }
  if (
    result.format_version !== SELECTOR_RESULTS_FORMAT_VERSION
    || requiredId(result.round_id, 'result round_id') !== result.round_id
    || positiveInt(result.item_count, 'result item_count') !== result.item_count
    || !Number.isInteger(result.participant_count)
    || result.participant_count !== result.rows.length
    || !Number.isInteger(result.selector_count)
    || result.selector_count < 0
    || !Number.isInteger(result.post_close_benchmark_count)
    || result.post_close_benchmark_count < 0
    || result.selector_count + result.post_close_benchmark_count
      > result.participant_count
    || result.questions.length !== result.item_count
  ) {
    throw new WeeklySelectorResultsError('result envelope is invalid');
  }
  result.rows.forEach(validateResultRow);
  result.questions.forEach(validateQuestionResult);
}

function parseBlindManifest(blindManifest) {
  if (!blindManifest || typeof blindManifest !== 'object' || Array.isArray(blindManifest)) {
    throw new WeeklySelectorResultsError('blind manifest is invalid');
  }
  if (!Array.isArray(blindManifest.items) || !blindManifest.items.length) {
    throw new WeeklySelectorResultsError('blind manifest has no items');
  }
  const items = new Map();
  for (const rawItem of blindManifest.items) {
    if (!rawItem || typeof rawItem !== 'object' || Array.isArray(rawItem)) {
      throw new WeeklySelectorResultsError('blind item is invalid');
    }
    const itemId = requiredId(rawItem.id, 'blind item id');
    if (items.has(itemId)) throw new WeeklySelectorResultsError('blind item IDs are duplicated');
    if (!Array.isArray(rawItem.choices) || !rawItem.choices.length) {
      throw new WeeklySelectorResultsError('blind item has no choices');
    }
    const rawChoices = new Map();
    for (const rawChoice of rawItem.choices) {
      if (!rawChoice || typeof rawChoice !== 'object' || Array.isArray(rawChoice)) {
        throw new WeeklySelectorResultsError('blind choice is invalid');
      }
      const choiceId = requiredId(rawChoice.id, 'blind choice id');
      const clusterId = requiredId(rawChoice.cluster_id, 'blind cluster id');
      if (rawChoices.has(choiceId)) throw new WeeklySelectorResultsError('blind choice IDs are duplicated');
      rawChoices.set(choiceId, {
        clusterId,
        sminaScore: rawChoice.smina_score,
      });
    }
    const clusterIds = [...new Set([...rawChoices.values()].map(choice => choice.clusterId))]
      .sort(compareText);
    const clusters = new Map();
    const choices = new Map();
    clusterIds.forEach((clusterId, clusterIndex) => {
      const memberIds = [...rawChoices.entries()]
        .filter(([, choice]) => choice.clusterId === clusterId)
        .map(([choiceId]) => choiceId)
        .sort(compareText);
      const clusterLabel = `Cluster ${alphabeticLabel(clusterIndex)}`;
      clusters.set(clusterId, { label: clusterLabel, choiceIds: memberIds });
      memberIds.forEach((choiceId, memberIndex) => {
        choices.set(choiceId, {
          ...rawChoices.get(choiceId),
          label: `Pose ${alphabeticLabel(clusterIndex)}-${memberIndex + 1}`,
        });
      });
    });
    items.set(itemId, { choices, clusters });
  }
  return items;
}

function parseRevealManifest(revealManifest) {
  if (!revealManifest || typeof revealManifest !== 'object' || Array.isArray(revealManifest)
      || !Array.isArray(revealManifest.items) || !revealManifest.items.length) {
    throw new WeeklySelectorResultsError('reveal manifest is invalid');
  }
  const items = new Map();
  for (const rawItem of revealManifest.items) {
    const itemId = requiredId(rawItem?.id, 'reveal item id');
    if (items.has(itemId)) throw new WeeklySelectorResultsError('reveal item IDs are duplicated');
    if (!Array.isArray(rawItem.choices) || !rawItem.choices.length) {
      throw new WeeklySelectorResultsError('reveal item has no choices');
    }
    const choices = new Map();
    for (const rawChoice of rawItem.choices) {
      const choiceId = requiredId(rawChoice?.id, 'reveal choice id');
      if (choices.has(choiceId)) throw new WeeklySelectorResultsError('reveal choice IDs are duplicated');
      choices.set(choiceId, {
        correct: rawChoice.correct === true,
        acceptedCorrect: rawChoice.accepted_correct === true,
      });
    }
    items.set(itemId, { choices });
  }
  return items;
}

function parseClusteredDecision(value, answerKey) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new WeeklySelectorResultsError('clustered decision is invalid');
  }
  if (value.selection_kind === 'none') {
    exactKeys(value, ['selection_kind'], 'clustered none decision');
    return { selectionKind: 'none', clusterId: null };
  }
  exactKeys(value, ['selection_kind', 'cluster_id'], 'clustered decision');
  if (value.selection_kind !== 'cluster') {
    throw new WeeklySelectorResultsError('clustered selection_kind is invalid');
  }
  const clusterId = requiredId(value.cluster_id, 'submission cluster_id');
  if (!answerKey.clusters.has(clusterId)) throw new WeeklySelectorResultsError('unknown submission cluster');
  return { selectionKind: 'cluster', clusterId };
}

function parseUnclusteredDecision(value, answerKey) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new WeeklySelectorResultsError('unclustered decision is invalid');
  }
  if (value.selection_kind === 'none') {
    exactKeys(value, ['selection_kind'], 'unclustered none decision');
    return { selectionKind: 'none', choiceId: null };
  }
  exactKeys(value, ['selection_kind', 'choice_id'], 'unclustered decision');
  if (value.selection_kind !== 'exact') {
    throw new WeeklySelectorResultsError('unclustered selection_kind is invalid');
  }
  const choiceId = requiredId(value.choice_id, 'submission choice_id');
  if (!answerKey.choices.has(choiceId)) throw new WeeklySelectorResultsError('unknown submission choice');
  return { selectionKind: 'exact', choiceId };
}

function normalizedParticipant(submission, participantType) {
  if (!submission || typeof submission !== 'object' || Array.isArray(submission)) {
    throw new WeeklySelectorResultsError('submission row is invalid');
  }
  const sourceIdentity = submission.identity ?? submission.weekly_selector_identities ?? submission;
  const items = submission.items ?? submission.payload?.items;
  if (!Array.isArray(items)) throw new WeeklySelectorResultsError('submission payload items are invalid');
  return {
    identity: normalizeIdentity(sourceIdentity, {
      requireBlindnessAttestation: participantType === 'selector',
    }),
    participantType,
    items,
  };
}

function normalizeIdentity(source, { requireBlindnessAttestation = false } = {}) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) {
    throw new WeeklySelectorResultsError('submission identity is missing');
  }
  const identity = {
    display_name: requiredText(source.display_name, 'display_name'),
    method_name: requiredText(source.method_name ?? source.model_name ?? source.model, 'method_name'),
    method_version: requiredText(
      source.method_version ?? source.model_version,
      'method_version',
    ),
    provider: requiredText(source.provider, 'provider'),
    model_name: requiredText(source.model_name ?? source.model ?? source.method_name, 'model_name'),
    model_version: requiredText(source.model_version ?? source.method_version, 'model_version'),
  };
  if (source.prompt_profile_id != null) {
    const promptProfileId = requiredText(source.prompt_profile_id, 'prompt_profile_id');
    if (promptProfileId !== SELECTOR_PROMPT_PROFILE_ID) {
      throw new WeeklySelectorResultsError('identity prompt_profile_id is invalid');
    }
    identity.prompt_profile_id = promptProfileId;
  } else if (requireBlindnessAttestation) {
    throw new WeeklySelectorResultsError('identity prompt profile is missing');
  }
  const digestSource = {
    prompt_sha256: source.prompt_sha256,
    tools_sha256: source.tools_sha256 ?? source.tool_sha256,
    config_sha256: source.config_sha256,
  };
  const providedDigests = DIGEST_FIELDS.filter(field => digestSource[field] != null);
  if (providedDigests.length && providedDigests.length !== DIGEST_FIELDS.length) {
    throw new WeeklySelectorResultsError('identity provenance digests are incomplete');
  }
  for (const field of providedDigests) identity[field] = requiredSha256(digestSource[field], field);
  if (
    identity.prompt_profile_id === SELECTOR_PROMPT_PROFILE_ID
    && identity.prompt_sha256 !== SELECTOR_PROMPT_SHA256
  ) {
    throw new WeeklySelectorResultsError('identity prompt digest does not match prompt profile');
  }
  const providedAttestation = BLINDNESS_ATTESTATION_FIELDS.filter(field => source[field] != null);
  if (requireBlindnessAttestation && providedAttestation.length === 0) {
    throw new WeeklySelectorResultsError('identity blindness attestation is missing');
  }
  if (providedAttestation.length && providedAttestation.length !== BLINDNESS_ATTESTATION_FIELDS.length) {
    throw new WeeklySelectorResultsError('identity blindness attestation is incomplete');
  }
  if (providedAttestation.length) {
    let attestation;
    try {
      attestation = validateBlindnessAttestation(source.blindness_attestation);
    } catch (error) {
      if (!(error instanceof ContractError)) throw error;
      throw new WeeklySelectorResultsError('identity blindness attestation is invalid');
    }
    const attestationSha256 = requiredSha256(
      source.blindness_attestation_sha256,
      'blindness_attestation_sha256',
    );
    if (sha256Hex(attestation) !== attestationSha256) {
      throw new WeeklySelectorResultsError('identity blindness attestation digest is invalid');
    }
    identity.blindness_attestation = attestation;
    identity.blindness_attestation_sha256 = attestationSha256;
  }
  if (source.benchmark != null) {
    identity.benchmark = normalizeBenchmarkProvenance(source.benchmark);
  }
  return identity;
}

function normalizeBenchmarkProvenance(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new WeeklySelectorResultsError('benchmark provenance is invalid');
  }
  exactKeys(raw, [
    'run_class',
    'requested_model_id',
    'observed_model_id',
    'requested_effort',
    'applied_effort',
    'effort_reporting',
    'input_manifest_sha256',
    'runtime_sha256',
    'execution_sha256',
  ], 'benchmark provenance');
  if (raw.run_class !== 'post_close_benchmark') {
    throw new WeeklySelectorResultsError('benchmark provenance run_class is invalid');
  }
  const requestedEffort = requiredText(raw.requested_effort, 'requested_effort');
  const effortReporting = requiredText(raw.effort_reporting, 'effort_reporting');
  const appliedEffort = raw.applied_effort == null
    ? null
    : requiredText(raw.applied_effort, 'applied_effort');
  if (
    !['default', 'low', 'medium', 'high', 'max'].includes(requestedEffort)
    || !['reported', 'not_exposed'].includes(effortReporting)
    || effortReporting === 'reported' && appliedEffort == null
    || effortReporting === 'not_exposed' && appliedEffort != null
  ) {
    throw new WeeklySelectorResultsError('benchmark effort provenance is invalid');
  }
  return {
    run_class: 'post_close_benchmark',
    requested_model_id: requiredText(raw.requested_model_id, 'requested_model_id'),
    observed_model_id: requiredText(raw.observed_model_id, 'observed_model_id'),
    requested_effort: requestedEffort,
    applied_effort: appliedEffort,
    effort_reporting: effortReporting,
    input_manifest_sha256: requiredSha256(
      raw.input_manifest_sha256,
      'input_manifest_sha256',
    ),
    runtime_sha256: requiredSha256(raw.runtime_sha256, 'runtime_sha256'),
    execution_sha256: requiredSha256(raw.execution_sha256, 'execution_sha256'),
  };
}

function scoreTrack(correct, total) {
  return { correct, item_count: total, accuracy: roundPercent(correct, total) };
}

function rankTrack(rows, track) {
  const ranked = [...rows].sort((left, right) => (
    right[track].correct - left[track].correct
    || compareText(identitySortKey(left.identity), identitySortKey(right.identity))
  ));
  let previousCorrect = null;
  let previousRank = 0;
  ranked.forEach((row, index) => {
    if (row[track].correct !== previousCorrect) previousRank = index + 1;
    row[track].rank = previousRank;
    previousCorrect = row[track].correct;
  });
}

function compareOverallRows(left, right) {
  return left.clustered.rank - right.clustered.rank
    || left.unclustered.rank - right.unclustered.rank
    || compareText(identitySortKey(left.identity), identitySortKey(right.identity));
}

function buildQuestionResults(answerKeys, participants) {
  const questionRows = new Map();
  for (const [itemId, answerKey] of answerKeys.entries()) {
    const clustered = [...answerKey.clusters.entries()].map(([clusterId, cluster]) => ({
      key: clusterId,
      selection_kind: 'cluster',
      label: cluster.label,
      correct: answerKey.acceptedByCluster.get(clusterId) === true,
      count: 0,
      display_names: [],
    }));
    clustered.push({
      key: null,
      selection_kind: 'none',
      label: 'None',
      correct: !answerKey.hasAcceptedCorrectCluster,
      count: 0,
      display_names: [],
    });
    const unclustered = [...answerKey.choices.entries()].map(([choiceId, choice]) => ({
      key: choiceId,
      selection_kind: 'exact',
      label: choice.label,
      correct: choice.correct,
      count: 0,
      display_names: [],
    }));
    unclustered.push({
      key: null,
      selection_kind: 'none',
      label: 'None',
      correct: !answerKey.hasRawCorrect,
      count: 0,
      display_names: [],
    });
    questionRows.set(itemId, { item_id: itemId, clustered, unclustered });
  }

  for (const participant of participants) {
    for (const item of participant.items) {
      const row = questionRows.get(item.item_id);
      const answerKey = answerKeys.get(item.item_id);
      if (!row || !answerKey) throw new WeeklySelectorResultsError('unknown submission item');
      const clustered = parseClusteredDecision(item.clustered, answerKey);
      const unclustered = parseUnclusteredDecision(item.unclustered, answerKey);
      addQuestionDecision(
        row.clustered,
        clustered.selectionKind,
        clustered.clusterId,
        participant.identity.display_name,
      );
      addQuestionDecision(
        row.unclustered,
        unclustered.selectionKind,
        unclustered.choiceId,
        participant.identity.display_name,
      );
    }
  }

  return [...questionRows.values()]
    .sort((left, right) => compareText(left.item_id, right.item_id))
    .map(row => ({
      item_id: row.item_id,
      clustered: { answers: publicQuestionAnswers(row.clustered) },
      unclustered: { answers: publicQuestionAnswers(row.unclustered) },
    }));
}

function addQuestionDecision(answers, selectionKind, key, displayName) {
  const answer = answers.find(candidate => (
    candidate.selection_kind === selectionKind && candidate.key === key
  ));
  if (!answer) throw new WeeklySelectorResultsError('question decision is invalid');
  answer.count += 1;
  answer.display_names.push(displayName);
}

function publicQuestionAnswers(answers) {
  return answers.map(({ key: _key, ...answer }) => ({
    ...answer,
    display_names: answer.display_names.sort(compareText),
  }));
}

function validateResultRow(row) {
  exactKeys(row, ['participant_type', 'identity', 'clustered', 'unclustered'], 'result row');
  if (!['selector', 'post_close_benchmark', 'synthetic'].includes(row.participant_type)) {
    throw new WeeklySelectorResultsError('participant_type is invalid');
  }
  const digestKeys = DIGEST_FIELDS.filter(field => Object.hasOwn(row.identity, field));
  const attestationKeys = BLINDNESS_ATTESTATION_FIELDS.filter(
    field => Object.hasOwn(row.identity, field),
  );
  exactKeys(
    row.identity,
    [
      'display_name',
      'method_name',
      'method_version',
      'provider',
      'model_name',
      'model_version',
      ...(Object.hasOwn(row.identity, 'prompt_profile_id') ? ['prompt_profile_id'] : []),
      ...digestKeys,
      ...attestationKeys,
      ...(Object.hasOwn(row.identity, 'benchmark') ? ['benchmark'] : []),
    ],
    'result identity',
  );
  normalizeIdentity(row.identity, {
    requireBlindnessAttestation: row.participant_type !== 'synthetic',
  });
  validateTrack(row.clustered, 'clustered');
  validateTrack(row.unclustered, 'unclustered');
}

function validateTrack(track, field) {
  exactKeys(track, ['correct', 'item_count', 'accuracy', 'rank'], `${field} result`);
  if (!Number.isInteger(track.correct) || track.correct < 0
      || !Number.isInteger(track.item_count) || track.item_count <= 0
      || track.correct > track.item_count
      || !Number.isInteger(track.rank) || track.rank <= 0) {
    throw new WeeklySelectorResultsError(`${field} result is invalid`);
  }
  validatePercent(track.accuracy, `${field} accuracy`);
}

function validateQuestionResult(question) {
  exactKeys(question, ['item_id', 'clustered', 'unclustered'], 'question result');
  requiredId(question.item_id, 'question item_id');
  for (const mode of ['clustered', 'unclustered']) {
    exactKeys(question[mode], ['answers'], `${mode} question result`);
    if (!Array.isArray(question[mode].answers) || !question[mode].answers.length) {
      throw new WeeklySelectorResultsError(`${mode} question answers are invalid`);
    }
    for (const answer of question[mode].answers) {
      exactKeys(
        answer,
        ['selection_kind', 'label', 'correct', 'count', 'display_names'],
        `${mode} question answer`,
      );
      if (!['cluster', 'exact', 'none'].includes(answer.selection_kind)
          || typeof answer.correct !== 'boolean'
          || !Number.isInteger(answer.count) || answer.count < 0
          || !Array.isArray(answer.display_names)
          || answer.display_names.length !== answer.count) {
        throw new WeeklySelectorResultsError(`${mode} question answer is invalid`);
      }
      requiredText(answer.label, 'answer label');
      answer.display_names.forEach(name => requiredText(name, 'answer display name'));
    }
  }
}

function walkSanitized(value) {
  if (Array.isArray(value)) {
    value.forEach(walkSanitized);
    return;
  }
  if (!value || typeof value !== 'object') return;
  for (const [key, nested] of Object.entries(value)) {
    if (FORBIDDEN_RESULT_KEY.test(key)) {
      throw new WeeklySelectorResultsError(`result contains forbidden field ${key}`);
    }
    walkSanitized(nested);
  }
}

function exactKeys(value, allowed, field) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new WeeklySelectorResultsError(`${field} must be an object`);
  }
  const expected = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) throw new WeeklySelectorResultsError(`${field} contains unknown key ${key}`);
  }
  for (const key of allowed) {
    if (!Object.hasOwn(value, key)) throw new WeeklySelectorResultsError(`${field} is missing ${key}`);
  }
}

function alphabeticLabel(index) {
  let value = index + 1;
  let label = '';
  while (value > 0) {
    value -= 1;
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26);
  }
  return label;
}

function canonicalString(value) {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, character => (
    `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`
  ));
}

function identitySortKey(identity) {
  return `${identity.display_name}\u0000${identity.provider}\u0000${identity.model_name}\u0000${identity.model_version}`;
}

function compareText(left, right) {
  return left < right ? -1 : (left > right ? 1 : 0);
}

function roundPercent(numerator, denominator) {
  return Math.round((numerator / denominator) * 1000) / 10;
}

function validatePercent(value, field) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100) {
    throw new WeeklySelectorResultsError(`${field} is invalid`);
  }
}

function requiredText(value, field) {
  if (typeof value !== 'string' || !value.trim() || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new WeeklySelectorResultsError(`${field} must be non-empty text`);
  }
  return value.trim();
}

function requiredId(value, field) {
  if (typeof value !== 'string' || !ID_RE.test(value)) {
    throw new WeeklySelectorResultsError(`${field} must be a valid ID`);
  }
  return value;
}

function requiredSha256(value, field) {
  if (typeof value !== 'string' || !SHA256_RE.test(value)) {
    throw new WeeklySelectorResultsError(`${field} must be a lowercase SHA-256`);
  }
  return value;
}

function requiredEnvironment(value) {
  if (!['production', 'preview', 'development'].includes(value)) {
    throw new WeeklySelectorResultsError('round environment is invalid');
  }
  return value;
}

function positiveInt(value, field) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new WeeklySelectorResultsError(`${field} must be a positive integer`);
  }
  return value;
}

export { UUID_PATTERN };
