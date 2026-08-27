import { enrichPrivateWeeklyPool } from './lib/released-crystal.js';

export const ALLOWED_ROUND_ID = 'weekly-2026-08-08-beta-v5-global-tm-29';
export const PRIVATE_EVALUATION_FORMAT_VERSION = 'foldarium.weekly-private-evaluation/v5';
export const WEEKLY_LEADERBOARD_FORMAT_VERSION = 'foldarium.weekly-leaderboard/v1';
export const WEEKLY_QUESTION_RESULTS_FORMAT_VERSION = 'foldarium.weekly-question-results/v2';

const LEADERBOARD_TOP_LEVEL_KEYS = new Set([
  'format_version',
  'round_id',
  'item_count',
  'participant_count',
  'complete_runs',
  'partial_runs',
]);
const COMPLETE_LEADERBOARD_ROW_KEYS = new Set([
  'display_name', 'correct', 'answered', 'total', 'accuracy', 'coverage', 'rank',
]);
const PARTIAL_LEADERBOARD_ROW_KEYS = new Set([
  'display_name', 'correct', 'answered', 'total', 'accuracy', 'coverage',
]);
const FORBIDDEN_LEADERBOARD_KEYS = new Set([
  'user_id', 'session_id', 'choice_id', 'item_id', 'vote_id', 'vote_attempt_id',
  'display_name_hash', 'name_hash', 'email', 'picked_choice_id', 'answers', 'votes',
  'sha256', 'hash', 'uuid', 'trace', 'app_state', 'initial_app_state', 'user_hash',
  'identity_hash', 'participant_id', 'run_id', 'sample_id', 'prediction_sha256',
  'blind_manifest', 'reveal_manifest', 'choices', 'password',
]);

export { enrichPrivateWeeklyPool };

const LEGACY_STORAGE_KEY = 'foldariumPrivateEvalBundle';
const SHA256 = /^[0-9a-f]{64}$/;
const trustedBundles = new WeakSet();

export function selectRetrospectiveAnswer(item) {
  if (!item || !Array.isArray(item.choices)) return null;
  return item.choices
    .filter(choice => choice?.correct === true && Number.isFinite(choice.rmsd))
    .sort((left, right) => (
      left.rmsd - right.rmsd
      || String(left._weeklyChoiceId || left.id || '')
        .localeCompare(String(right._weeklyChoiceId || right.id || ''))
    ))[0] || null;
}

export function isPreviewDeployment(config = {}) {
  return config.deploymentEnvironment === 'preview';
}

export function isTrustedBundle(bundle) {
  return trustedBundles.has(bundle);
}

function markTrustedBundle(bundle) {
  if (!isClientBundle(bundle)) throw new Error('Private evaluation bundle is invalid.');
  trustedBundles.add(bundle);
  return bundle;
}

export function clearLegacyStoredBundle(storage = globalThis.sessionStorage) {
  storage?.removeItem(LEGACY_STORAGE_KEY);
}

export function buildSyntheticReviewRound(bundle) {
  if (!isTrustedBundle(bundle)) return null;
  return Object.freeze({
    round_id: bundle.round_id,
    campaign_id: bundle.campaign_id,
    environment: 'production',
    public_status: 'open',
    opens_at: bundle.opens_at,
    closes_at: bundle.closes_at,
    blind_manifest: bundle.blind_manifest,
    blind_manifest_sha256: bundle.blind_manifest_sha256,
    reveal_manifest: bundle.reveal_manifest,
    reveal_manifest_sha256: bundle.reveal_manifest_sha256,
    item_count: bundle.item_count,
  });
}

export function activatePrivateReview(bundle) {
  if (!isTrustedBundle(bundle)) return null;
  const state = Object.freeze({
    active: true,
    round_id: bundle.round_id,
    evaluation_id: bundle.evaluation_id,
    item_count: bundle.item_count,
    choice_count: bundle.choice_count,
    reveal_digest_tail: bundle.reveal_manifest_sha256.slice(-8),
  });
  if (typeof globalThis !== 'undefined') {
    globalThis.FOLDARIUM_PRIVATE_REVIEW = state;
  }
  return state;
}

export function deactivatePrivateReview() {
  clearLegacyStoredBundle();
  if (typeof globalThis !== 'undefined') {
    globalThis.FOLDARIUM_PRIVATE_REVIEW = Object.freeze({ active: false });
  }
}

export function validatePrivateReviewRendering(bundle, pool) {
  if (!isTrustedBundle(bundle)) {
    throw new Error('Private evaluation bundle is invalid.');
  }
  if (!Array.isArray(pool)) {
    throw new Error('Private evaluation pool is incomplete.');
  }

  const blindItems = bundle.blind_manifest.items;
  const revealItems = bundle.reveal_manifest.items;
  if (blindItems.length !== bundle.item_count
    || revealItems.length !== bundle.item_count
    || pool.length !== bundle.item_count) {
    throw new Error(`Private evaluation item count mismatch (${pool.length}/${bundle.item_count}).`);
  }

  const blindChoiceCount = countChoices(blindItems);
  const revealChoiceCount = countChoices(revealItems);
  const poolChoiceCount = countChoices(pool);
  if (blindChoiceCount !== bundle.choice_count
    || revealChoiceCount !== bundle.choice_count
    || poolChoiceCount !== bundle.choice_count) {
    throw new Error(`Private evaluation choice count mismatch (${poolChoiceCount}/${bundle.choice_count}).`);
  }

  const blindIdentity = manifestIdentity(blindItems, 'blind manifest');
  const revealIdentity = manifestIdentity(revealItems, 'reveal manifest');
  assertIdentitiesEqual(blindIdentity, revealIdentity);
  assertPoolIdentity(pool, blindIdentity);

  const enrichedPool = enrichPrivateWeeklyPool(pool, bundle);

  if (bundle.weekly_leaderboard != null) {
    validateWeeklyLeaderboard(bundle.weekly_leaderboard, { roundId: bundle.round_id });
  }
  if (bundle.weekly_question_results != null) {
    validateWeeklyQuestionResults(bundle.weekly_question_results, {
      roundId: bundle.round_id,
      revealManifest: bundle.reveal_manifest,
    });
  }

  for (const item of enrichedPool) {
    if (typeof item.protein_file !== 'string' || !item.protein_file) {
      throw new Error(`Private evaluation item ${item.id} is missing a protein asset.`);
    }
    const released = item.released_crystal;
    if (!released
      || typeof released.pdb_id !== 'string' || !released.pdb_id
      || typeof released.cif_url !== 'string' || !released.cif_url
      || typeof released.structure_page_url !== 'string' || !released.structure_page_url
      || typeof released.ligand_component_id !== 'string' || !released.ligand_component_id) {
      throw new Error(`Private evaluation item ${item.id} is missing released crystal metadata.`);
    }
    if (released.pdb_id !== item.id.toUpperCase()) {
      throw new Error(`Private evaluation item ${item.id} released crystal identity is mismatched.`);
    }
    for (const choice of item.choices) {
      if (typeof choice.pose_file !== 'string' || !choice.pose_file) {
        throw new Error(`Private evaluation choice ${item.id}/${choice._weeklyChoiceId || choice.id} is missing a pose asset.`);
      }
      if (!Number.isFinite(choice.rmsd)) {
        throw new Error(`Private evaluation choice ${item.id}/${choice._weeklyChoiceId || choice.id} is missing a reveal score.`);
      }
      if (typeof choice.correct !== 'boolean') {
        throw new Error(`Private evaluation choice ${item.id}/${choice._weeklyChoiceId || choice.id} is missing correctness.`);
      }
    }
    const overlay = item.answer_overlay;
    if (!overlay || overlay.item_id !== item.id) {
      throw new Error(`Private evaluation item ${item.id} is missing its answer overlay.`);
    }
    if (typeof overlay.crystal_ligand_pdb !== 'string'
      || !overlay.crystal_ligand_pdb.endsWith('\nEND\n')) {
      throw new Error(`Private evaluation item ${item.id} crystal overlay is invalid.`);
    }
    if (!Array.isArray(overlay.poses) || overlay.poses.length !== item.choices.length) {
      throw new Error(`Private evaluation item ${item.id} pose overlays are incomplete.`);
    }
    const posesById = new Map(overlay.poses.map(pose => [pose.id, pose]));
    for (const choice of item.choices) {
      const choiceId = choice._weeklyChoiceId || choice.id;
      const pose = posesById.get(choiceId);
      if (!pose
        || typeof pose.predicted_pose_pdb !== 'string'
        || !pose.predicted_pose_pdb.endsWith('\nEND\n')
        || typeof pose.crystal_ligand_pdb !== 'string'
        || !pose.crystal_ligand_pdb.endsWith('\nEND\n')
        || typeof pose.crystal_pocket_pdb !== 'string'
        || !pose.crystal_pocket_pdb.endsWith('\nEND\n')
        || pose.rmsd !== choice.rmsd
        || pose.correct !== choice.correct) {
        throw new Error(`Private evaluation item ${item.id}/${choiceId} pose overlay is invalid.`);
      }
    }
  }
}

export async function fetchPrivateEvaluationBundle({
  fetchImpl = globalThis.fetch,
} = {}) {
  const response = await fetchImpl('/api/private-evaluation', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = payload?.error || 'Private evaluation request failed';
    throw new Error(message);
  }
  if (!isClientBundle(payload)) throw new Error('Private evaluation bundle is invalid.');
  return markTrustedBundle(payload);
}

export function mountPrivateReviewUi({
  config,
  document = globalThis.document,
  fetchImpl = globalThis.fetch,
  onBundleLoaded,
} = {}) {
  const control = document?.getElementById('private-review-control');
  const banner = document?.getElementById('private-review-banner');
  const status = document?.getElementById('private-review-status');
  if (!control || !banner || !status) return { visible: false, ready: Promise.resolve(null) };

  const preview = isPreviewDeployment(config);
  control.hidden = true;
  if (!preview) {
    banner.hidden = true;
    return { visible: false, ready: Promise.resolve(null) };
  }

  clearLegacyStoredBundle();

  const renderStatus = (message, tone = 'muted') => {
    status.textContent = message;
    status.dataset.tone = tone;
  };

  const renderBanner = (active) => {
    banner.hidden = !active;
    banner.dataset.active = active ? 'true' : 'false';
  };

  const formatLoadedStatus = (bundle) => (
    `${bundle.round_id} · reveal …${bundle.reveal_manifest_sha256.slice(-8)} · ${bundle.evaluation_id}`
  );

  renderBanner(false);
  renderStatus('Loading retrospective…', 'loading');

  const ready = (async () => {
    renderStatus('Verifying private evaluation…', 'loading');
    try {
      const bundle = await fetchPrivateEvaluationBundle({ fetchImpl });
      if (typeof onBundleLoaded === 'function') {
        await Promise.resolve(onBundleLoaded(bundle));
        if (globalThis.FOLDARIUM_PRIVATE_REVIEW?.active !== true) {
          throw new Error('Private evaluation bundle failed to activate.');
        }
      } else {
        activatePrivateReview(bundle);
      }
      renderBanner(true);
      renderStatus(formatLoadedStatus(bundle), 'active');
      return bundle;
    } catch (error) {
      deactivatePrivateReview();
      renderBanner(false);
      renderStatus(error.message, 'error');
      return null;
    }
  })();

  return { visible: false, bundle: null, ready };
}

export function validateWeeklyLeaderboard(leaderboard, { roundId = ALLOWED_ROUND_ID } = {}) {
  if (!leaderboard || typeof leaderboard !== 'object' || Array.isArray(leaderboard)) {
    throw new Error('Weekly leaderboard is invalid.');
  }
  assertLeaderboardPrivacy(leaderboard, 'weekly_leaderboard');
  for (const key of Object.keys(leaderboard)) {
    if (!LEADERBOARD_TOP_LEVEL_KEYS.has(key)) {
      throw new Error(`Weekly leaderboard contains unexpected field (${key}).`);
    }
  }
  for (const key of LEADERBOARD_TOP_LEVEL_KEYS) {
    if (!(key in leaderboard)) {
      throw new Error(`Weekly leaderboard is missing ${key}.`);
    }
  }
  if (leaderboard.format_version !== WEEKLY_LEADERBOARD_FORMAT_VERSION) {
    throw new Error('Weekly leaderboard format_version is invalid.');
  }
  if (leaderboard.round_id !== roundId) {
    throw new Error('Weekly leaderboard round_id is invalid.');
  }
  if (!Number.isInteger(leaderboard.item_count) || leaderboard.item_count <= 0) {
    throw new Error('Weekly leaderboard item_count is invalid.');
  }
  if (!Number.isInteger(leaderboard.participant_count) || leaderboard.participant_count < 0) {
    throw new Error('Weekly leaderboard participant_count is invalid.');
  }
  validateLeaderboardRunRows(leaderboard.complete_runs, 'complete_runs', COMPLETE_LEADERBOARD_ROW_KEYS, {
    requireRank: true,
  });
  validateLeaderboardRunRows(leaderboard.partial_runs, 'partial_runs', PARTIAL_LEADERBOARD_ROW_KEYS, {
    requireRank: false,
  });
  if (leaderboard.participant_count
    !== leaderboard.complete_runs.length + leaderboard.partial_runs.length) {
    throw new Error('Weekly leaderboard participant_count is inconsistent.');
  }
  leaderboard.complete_runs.forEach((row, index) => {
    if (row.total !== leaderboard.item_count || row.answered !== row.total || row.rank !== index + 1) {
      throw new Error(`Weekly leaderboard complete_runs[${index}] completion is inconsistent.`);
    }
  });
  leaderboard.partial_runs.forEach((row, index) => {
    if (row.total !== leaderboard.item_count || row.answered >= row.total) {
      throw new Error(`Weekly leaderboard partial_runs[${index}] completion is inconsistent.`);
    }
  });
  return leaderboard;
}

export function validateWeeklyQuestionResults(
  results,
  { roundId = ALLOWED_ROUND_ID, revealManifest } = {},
) {
  if (!results || typeof results !== 'object' || Array.isArray(results)) {
    throw new Error('Weekly question results are invalid.');
  }
  const topLevelKeys = new Set(['format_version', 'round_id', 'item_count', 'items']);
  if (Object.keys(results).some(key => !topLevelKeys.has(key))) {
    throw new Error('Weekly question results contain an unexpected field.');
  }
  if (results.format_version !== WEEKLY_QUESTION_RESULTS_FORMAT_VERSION
    || results.round_id !== roundId
    || !Number.isInteger(results.item_count)
    || results.item_count <= 0
    || !Array.isArray(results.items)
    || results.items.length !== results.item_count) {
    throw new Error('Weekly question results metadata is invalid.');
  }
  const revealItems = new Map((revealManifest?.items || []).map(item => [
    item.id,
    new Map((item.choices || []).map(choice => [choice.id, {
      accepted: choice.accepted_correct === true,
      exact: choice.correct === true,
    }])),
  ]));
  if (revealItems.size !== results.item_count) {
    throw new Error('Weekly question results reveal identity is invalid.');
  }
  const seenItems = new Set();
  for (const item of results.items) {
    const itemKeys = new Set([
      'item_id', 'answered_count', 'correct_count', 'correct_display_names', 'answers',
    ]);
    if (!item || typeof item !== 'object' || Array.isArray(item)
      || Object.keys(item).some(key => !itemKeys.has(key))
      || typeof item.item_id !== 'string'
      || seenItems.has(item.item_id)
      || !revealItems.has(item.item_id)
      || !Number.isInteger(item.answered_count)
      || item.answered_count < 0
      || !Number.isInteger(item.correct_count)
      || item.correct_count < 0
      || item.correct_count > item.answered_count
      || !validDisplayNames(item.correct_display_names, item.correct_count)
      || !Array.isArray(item.answers)) {
      throw new Error('Weekly question result item is invalid.');
    }
    seenItems.add(item.item_id);
    const choices = revealItems.get(item.item_id);
    const seenAnswers = new Set();
    let answeredCount = 0;
    let correctCount = 0;
    for (const answer of item.answers) {
      const answerKeys = new Set([
        'choice_id', 'picked_none', 'selection_kind', 'correct', 'vote_count', 'display_names',
      ]);
      const answerIdentity = answer?.picked_none
        ? 'none' : `${answer?.selection_kind}:${answer?.choice_id}`;
      const expectedCorrect = answer?.picked_none
        ? ![...choices.values()].some(choice => choice.accepted)
        : (answer?.selection_kind === 'exact'
          ? choices.get(answer?.choice_id)?.exact
          : choices.get(answer?.choice_id)?.accepted);
      if (!answer || typeof answer !== 'object' || Array.isArray(answer)
        || Object.keys(answer).some(key => !answerKeys.has(key))
        || typeof answer.picked_none !== 'boolean'
        || typeof answer.correct !== 'boolean'
        || !Number.isInteger(answer.vote_count)
        || answer.vote_count <= 0
        || !validDisplayNames(answer.display_names, answer.vote_count)
        || seenAnswers.has(answerIdentity)
        || (answer.picked_none
          ? answer.choice_id !== null || answer.selection_kind !== 'none'
          : (typeof answer.choice_id !== 'string'
            || !choices.has(answer.choice_id)
            || !['cluster', 'exact', 'unknown'].includes(answer.selection_kind)))
        || answer.correct !== expectedCorrect) {
        throw new Error('Weekly question result answer is invalid.');
      }
      seenAnswers.add(answerIdentity);
      answeredCount += answer.vote_count;
      if (answer.correct) correctCount += answer.vote_count;
    }
    if (answeredCount !== item.answered_count || correctCount !== item.correct_count) {
      throw new Error('Weekly question result totals are inconsistent.');
    }
  }
  return results;
}

function validDisplayNames(names, expectedLength) {
  return Array.isArray(names)
    && names.length === expectedLength
    && names.every(name => typeof name === 'string' && name.trim() && name.length <= 200);
}

function assertLeaderboardPrivacy(value, path) {
  if (value === null || typeof value !== 'object') return;
  if (Array.isArray(value)) {
    value.forEach((entry, index) => assertLeaderboardPrivacy(entry, `${path}[${index}]`));
    return;
  }
  for (const key of Object.keys(value)) {
    if (FORBIDDEN_LEADERBOARD_KEYS.has(key)) {
      throw new Error(`Weekly leaderboard contains forbidden field (${path}.${key}).`);
    }
    assertLeaderboardPrivacy(value[key], `${path}.${key}`);
  }
}

function validateLeaderboardRunRows(rows, label, allowedKeys, { requireRank }) {
  if (!Array.isArray(rows)) {
    throw new Error(`Weekly leaderboard ${label} is invalid.`);
  }
  rows.forEach((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      throw new Error(`Weekly leaderboard ${label}[${index}] is invalid.`);
    }
    for (const key of Object.keys(row)) {
      if (!allowedKeys.has(key)) {
        throw new Error(`Weekly leaderboard ${label}[${index}] contains unexpected field (${key}).`);
      }
    }
    for (const key of allowedKeys) {
      if (key === 'rank' && !requireRank) continue;
      if (!(key in row)) {
        throw new Error(`Weekly leaderboard ${label}[${index}] is missing ${key}.`);
      }
    }
    if (typeof row.display_name !== 'string' || !row.display_name.trim()) {
      throw new Error(`Weekly leaderboard ${label}[${index}] display_name is invalid.`);
    }
    for (const metric of ['correct', 'answered', 'total', 'rank']) {
      if (!(metric in row)) continue;
      if (!Number.isInteger(row[metric]) || row[metric] < 0) {
        throw new Error(`Weekly leaderboard ${label}[${index}] ${metric} is invalid.`);
      }
    }
    if (typeof row.accuracy !== 'number' || !Number.isFinite(row.accuracy)
      || row.accuracy < 0 || row.accuracy > 100) {
      throw new Error(`Weekly leaderboard ${label}[${index}] accuracy is invalid.`);
    }
    if (typeof row.coverage !== 'number' || !Number.isFinite(row.coverage)
      || row.coverage < 0 || row.coverage > 100) {
      throw new Error(`Weekly leaderboard ${label}[${index}] coverage is invalid.`);
    }
    if (row.answered > row.total || row.correct > row.answered) {
      throw new Error(`Weekly leaderboard ${label}[${index}] score totals are inconsistent.`);
    }
  });
}

function isClientBundle(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  if (value.format_version !== PRIVATE_EVALUATION_FORMAT_VERSION) return false;
  if (value.round_id !== ALLOWED_ROUND_ID) return false;
  if (typeof value.evaluation_id !== 'string' || !value.evaluation_id) return false;
  if (typeof value.campaign_id !== 'string' || !value.campaign_id) return false;
  if (typeof value.opens_at !== 'string' || !value.opens_at) return false;
  if (typeof value.closes_at !== 'string' || !value.closes_at) return false;
  if (!Number.isInteger(value.item_count) || value.item_count <= 0) return false;
  if (!Number.isInteger(value.choice_count) || value.choice_count <= 0) return false;
  if (typeof value.blind_manifest_sha256 !== 'string' || !SHA256.test(value.blind_manifest_sha256)) {
    return false;
  }
  if (typeof value.reveal_manifest_sha256 !== 'string' || !SHA256.test(value.reveal_manifest_sha256)) {
    return false;
  }
  if (!value.blind_manifest || typeof value.blind_manifest !== 'object') return false;
  if (!value.reveal_manifest || typeof value.reveal_manifest !== 'object') return false;
  if (value.reveal_manifest.round_id !== ALLOWED_ROUND_ID) return false;
  if (value.reveal_manifest.blind_manifest_sha256 !== value.blind_manifest_sha256) return false;
  if (!Array.isArray(value.blind_manifest.items) || !value.blind_manifest.items.length) return false;
  if (!Array.isArray(value.reveal_manifest.items) || !value.reveal_manifest.items.length) return false;
  if (!Array.isArray(value.answer_overlays) || !value.answer_overlays.length) return false;
  return true;
}

function countChoices(items) {
  return items.reduce((total, item) => total + (Array.isArray(item.choices) ? item.choices.length : 0), 0);
}

function manifestIdentity(items, label) {
  const identity = new Map();
  for (const item of items) {
    const itemId = item?.id;
    if (typeof itemId !== 'string' || !itemId) {
      throw new Error(`${label} item identity is invalid.`);
    }
    if (identity.has(itemId)) throw new Error(`${label} item IDs are duplicated.`);
    const choiceIds = new Set();
    if (!Array.isArray(item.choices) || !item.choices.length) {
      throw new Error(`${label} choice identities are invalid.`);
    }
    for (const choice of item.choices) {
      const choiceId = choice?.id;
      if (typeof choiceId !== 'string' || !choiceId) {
        throw new Error(`${label} choice identity is invalid.`);
      }
      if (choiceIds.has(choiceId)) throw new Error(`${label} choice IDs are duplicated.`);
      choiceIds.add(choiceId);
    }
    identity.set(itemId, choiceIds);
  }
  return identity;
}

function assertIdentitiesEqual(left, right) {
  if (left.size !== right.size) {
    throw new Error('Blind and reveal item identities differ.');
  }
  for (const [itemId, leftChoices] of left.entries()) {
    const rightChoices = right.get(itemId);
    if (!rightChoices || leftChoices.size !== rightChoices.size) {
      throw new Error('Blind and reveal choice identities differ.');
    }
    for (const choiceId of leftChoices) {
      if (!rightChoices.has(choiceId)) {
        throw new Error('Blind and reveal choice identities differ.');
      }
    }
  }
}

function assertPoolIdentity(pool, expectedIdentity) {
  if (pool.length !== expectedIdentity.size) {
    throw new Error('Normalized pool item identities differ from the bundle.');
  }
  for (const item of pool) {
    const expectedChoices = expectedIdentity.get(item.id);
    if (!expectedChoices) {
      throw new Error(`Normalized pool item ${item.id} is not in the bundle.`);
    }
    const poolChoiceIds = new Set(
      item.choices.map(choice => choice._weeklyChoiceId || choice.id),
    );
    if (poolChoiceIds.size !== expectedChoices.size) {
      throw new Error(`Normalized pool choices for ${item.id} differ from the bundle.`);
    }
    for (const choiceId of expectedChoices) {
      if (!poolChoiceIds.has(choiceId)) {
        throw new Error(`Normalized pool choice ${item.id}/${choiceId} is missing.`);
      }
    }
  }
}
