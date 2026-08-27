import { createHash, createHmac } from 'node:crypto';
import { buildAnswerOverlays } from './private-evaluation-contract.js';

export const PUBLICATION_FORMAT_VERSION = 'foldarium.weekly-retrospective-publication/v1';
export const PUBLIC_ARTIFACT_FORMAT_VERSION = 'foldarium.weekly-retrospective-public/v1';
export const ADMIN_ARTIFACT_FORMAT_VERSION = 'foldarium.weekly-retrospective-admin/v1';
export const SOURCE_SNAPSHOT_FORMAT_VERSION = 'foldarium.weekly-retrospective-source/v1';
export const PRIVATE_EVALUATION_FORMAT_VERSION = 'foldarium.weekly-private-evaluation/v5';
export const ARCHIVE_LIST_FORMAT_VERSION = 'foldarium.weekly-retrospective-list/v1';
export const ARCHIVE_DETAIL_FORMAT_VERSION = 'foldarium.weekly-retrospective-detail/v1';
export const ARCHIVE_ADMIN_DETAIL_FORMAT_VERSION = 'foldarium.weekly-retrospective-admin-detail/v1';
export const ARCHIVE_ALL_TIME_FORMAT_VERSION = 'foldarium.weekly-retrospective-all-time/v1';
export const ARCHIVE_CURSOR_VERSION = 1;

export const APPROVED_LLM_IDENTITIES = Object.freeze([
  'Claude Opus',
  'Codex GPT-5.6',
  'GPT-5.6 Sol',
]);
export const BASELINE_IDENTITY = 'Smina';
export const LIGAND_PLDDT_BASELINE_IDENTITY = 'Ligand pLDDT';
export const PARTICIPANT_KINDS = Object.freeze(['human', 'llm', 'baseline']);
export const RANKING_VIEWS = Object.freeze(['total_correct', 'weighted_average_accuracy']);
export const LEGACY_EXACT_SCOPE_ROUND_ID = 'weekly-2026-08-08-beta-v5-global-tm-29';

const APPROVED_LLM_SET = new Set(APPROVED_LLM_IDENTITIES);
const PARTICIPANT_KIND_SET = new Set(PARTICIPANT_KINDS);
const SHA256 = /^[0-9a-f]{64}$/;
const PUBLICATION_ID = /^weekly_archive_[0-9a-f]{32}$/;
const EVALUATION_ID = /^weekly_eval_[0-9a-f]{32}$/;
const URI = /^[a-z][a-z0-9+.-]*:\/\//i;
const PUBLIC_ASSET_RESPONSE_KEYS = new Set(['pose_uri', 'protein_uri', 'pocket_uri']);
const PUBLIC_REFERENCE_RESPONSE_KEYS = new Set(['reference_uri']);
const FORBIDDEN_RESPONSE_KEYS = new Set([
  'publication_id',
  'evaluation_id',
  'object_uri',
  'sha256',
  'participant_link',
  'user_id',
  'session_id',
  'participant_hash',
  'display_name_hash',
  'vote_id',
  'vote_attempt_id',
  'trace',
  'viewer_trace',
  'comments',
  'comment',
  'app_state',
  'initial_app_state',
  'authorization',
  'credential',
]);

const PUBLICATION_FIELDS = Object.freeze([
  'publication_id',
  'round_id',
  'campaign_id',
  'environment',
  'format_version',
  'evaluation_id',
  'evaluation_format_version',
  'round_opens_at',
  'round_closes_at',
  'round_revealed_at',
  'blind_manifest_sha256',
  'private_index_sha256',
  'reveal_manifest_sha256',
  'reference_set_sha256',
  'prediction_set_sha256',
  'evaluation_artifact_sha256',
  'item_count',
  'choice_count',
  'source_snapshot_object_uri',
  'source_snapshot_sha256',
  'source_snapshot_size_bytes',
  'source_snapshot_media_type',
  'public_artifact_object_uri',
  'public_artifact_sha256',
  'public_artifact_size_bytes',
  'public_artifact_media_type',
  'admin_artifact_object_uri',
  'admin_artifact_sha256',
  'admin_artifact_size_bytes',
  'admin_artifact_media_type',
  'created_at',
]);

export const PUBLICATION_SELECT_FIELDS = PUBLICATION_FIELDS.join(',');

export const EVALUATION_SELECT_FIELDS = [
  'evaluation_id',
  'round_id',
  'campaign_id',
  'environment',
  'round_opens_at',
  'round_closes_at',
  'blind_manifest_sha256',
  'private_index_sha256',
  'reveal_manifest_sha256',
  'reference_set_sha256',
  'prediction_set_sha256',
  'format_version',
  'item_count',
  'choice_count',
  'artifact_object_uri',
  'artifact_sha256',
  'artifact_size_bytes',
  'artifact_media_type',
].join(',');

export const ROUND_SELECT_FIELDS = [
  'round_id',
  'campaign_id',
  'environment',
  'status',
  'opens_at',
  'closes_at',
  'blind_manifest',
  'blind_manifest_sha256',
  'reveal_manifest',
  'reveal_manifest_sha256',
  'item_count',
  'revealed_at',
].join(',');

export class WeeklyRetrospectiveError extends Error {
  constructor(message) {
    super(message);
    this.name = 'WeeklyRetrospectiveError';
  }
}

export function canonicalJson(value) {
  return JSON.stringify(sortKeys(value));
}

export function encodeArchiveCursor({ revealedAt, roundId }) {
  requiredTimestamp(revealedAt, 'cursor revealed_at');
  requiredRoundId(roundId, 'cursor round_id');
  return Buffer.from(canonicalJson({
    revealed_at: revealedAt,
    round_id: roundId,
    v: ARCHIVE_CURSOR_VERSION,
  }), 'utf8').toString('base64url');
}

export function decodeArchiveCursor(value) {
  if (typeof value !== 'string' || !value || value.length > 2048
    || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new WeeklyRetrospectiveError('archive cursor is invalid');
  }
  let parsed;
  try {
    const bytes = Buffer.from(value, 'base64url');
    if (bytes.toString('base64url') !== value) {
      throw new Error('non-canonical base64url');
    }
    parsed = JSON.parse(bytes.toString('utf8'));
  } catch {
    throw new WeeklyRetrospectiveError('archive cursor is invalid');
  }
  exactKeys(parsed, ['revealed_at', 'round_id', 'v'], 'archive cursor');
  if (parsed.v !== ARCHIVE_CURSOR_VERSION) {
    throw new WeeklyRetrospectiveError('archive cursor version is invalid');
  }
  return {
    revealedAt: requiredTimestamp(parsed.revealed_at, 'cursor revealed_at'),
    roundId: requiredRoundId(parsed.round_id, 'cursor round_id'),
  };
}

export function parseSupabaseObjectUri(objectUri, expectedSha256) {
  const digest = requiredSha256(expectedSha256, 'object digest');
  if (typeof objectUri !== 'string') {
    throw new WeeklyRetrospectiveError('object URI is invalid');
  }
  const match = /^supabase:\/\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\/sha256\/([0-9a-f]{2})\/([0-9a-f]{64})$/.exec(
    objectUri,
  );
  if (!match || match[2] !== digest.slice(0, 2) || match[3] !== digest) {
    throw new WeeklyRetrospectiveError('object URI is not content-addressed');
  }
  return {
    bucket: match[1],
    objectPath: `sha256/${match[2]}/${match[3]}`,
  };
}

export function verifyPublicationCatalogRow(raw) {
  exactKeys(raw, PUBLICATION_FIELDS, 'publication catalog row');
  if (!PUBLICATION_ID.test(raw.publication_id)) {
    throw new WeeklyRetrospectiveError('publication identity is invalid');
  }
  const roundId = requiredRoundId(raw.round_id, 'round_id');
  const campaignId = requiredText(raw.campaign_id, 'campaign_id');
  if (raw.environment !== 'production'
    || raw.format_version !== PUBLICATION_FORMAT_VERSION
    || raw.evaluation_format_version !== PRIVATE_EVALUATION_FORMAT_VERSION
    || !EVALUATION_ID.test(raw.evaluation_id)) {
    throw new WeeklyRetrospectiveError('publication binding is invalid');
  }
  const opensAt = requiredTimestamp(raw.round_opens_at, 'round_opens_at');
  const closesAt = requiredTimestamp(raw.round_closes_at, 'round_closes_at');
  const revealedAt = requiredTimestamp(raw.round_revealed_at, 'round_revealed_at');
  if (!(Date.parse(opensAt) < Date.parse(closesAt)
    && Date.parse(closesAt) <= Date.parse(revealedAt))) {
    throw new WeeklyRetrospectiveError('publication timestamps are invalid');
  }
  const digests = {};
  for (const field of [
    'blind_manifest_sha256',
    'private_index_sha256',
    'reveal_manifest_sha256',
    'reference_set_sha256',
    'prediction_set_sha256',
    'evaluation_artifact_sha256',
    'source_snapshot_sha256',
    'public_artifact_sha256',
    'admin_artifact_sha256',
  ]) {
    digests[field] = requiredSha256(raw[field], field);
  }
  const itemCount = positiveInt(raw.item_count, 'item_count');
  const choiceCount = positiveInt(raw.choice_count, 'choice_count');
  const descriptors = {};
  for (const prefix of ['source_snapshot', 'public_artifact', 'admin_artifact']) {
    if (raw[`${prefix}_media_type`] !== 'application/json') {
      throw new WeeklyRetrospectiveError(`${prefix} media type is invalid`);
    }
    descriptors[prefix] = {
      objectUri: raw[`${prefix}_object_uri`],
      sha256: digests[`${prefix}_sha256`],
      sizeBytes: positiveInt(raw[`${prefix}_size_bytes`], `${prefix} size`),
      mediaType: 'application/json',
      ...parseSupabaseObjectUri(
        raw[`${prefix}_object_uri`],
        digests[`${prefix}_sha256`],
      ),
    };
  }
  if (descriptors.source_snapshot.bucket !== descriptors.admin_artifact.bucket
    || descriptors.public_artifact.bucket !== descriptors.admin_artifact.bucket
    || digests.public_artifact_sha256 === digests.admin_artifact_sha256) {
    throw new WeeklyRetrospectiveError('publication artifact buckets differ');
  }
  const expectedId = stableId('weekly_archive', {
    admin_artifact_sha256: digests.admin_artifact_sha256,
    evaluation_artifact_sha256: digests.evaluation_artifact_sha256,
    evaluation_id: raw.evaluation_id,
    format_version: raw.format_version,
    public_artifact_sha256: digests.public_artifact_sha256,
    round_id: roundId,
    source_snapshot_sha256: digests.source_snapshot_sha256,
  }, 32);
  if (expectedId !== raw.publication_id) {
    throw new WeeklyRetrospectiveError('publication identity is not deterministic');
  }
  return {
    publicationId: raw.publication_id,
    roundId,
    campaignId,
    evaluationId: raw.evaluation_id,
    opensAt,
    closesAt,
    revealedAt,
    createdAt: requiredTimestamp(raw.created_at, 'created_at'),
    itemCount,
    choiceCount,
    digests,
    descriptors,
  };
}

export function withLigandPlddtBaseline(publicArtifact, context) {
  if (publicArtifact.automated_entries.some(
    row => row.participant === LIGAND_PLDDT_BASELINE_IDENTITY,
  )) return publicArtifact;
  const revealItems = new Map(context.revealManifest.items.map(item => [item.id, item]));
  const picks = new Map();
  let correct = 0;
  for (const item of context.blindManifest.items) {
    const reveal = revealItems.get(item.id);
    if (!reveal) throw new WeeklyRetrospectiveError('pLDDT baseline reveal item is missing');
    const revealChoices = new Map(reveal.choices.map(choice => [choice.id, choice]));
    const candidates = item.choices.map(choice => {
      const confidence = choice.confidence;
      if (confidence?.metric !== 'ligand_plddt'
          || !Number.isFinite(confidence.value)
          || !revealChoices.has(choice.id)) {
        throw new WeeklyRetrospectiveError('pLDDT baseline choice is invalid');
      }
      return { choiceId: choice.id, value: confidence.value };
    }).sort((left, right) => (
      right.value - left.value || left.choiceId.localeCompare(right.choiceId)
    ));
    if (!candidates.length) {
      throw new WeeklyRetrospectiveError('pLDDT baseline item has no choices');
    }
    const choiceId = candidates[0].choiceId;
    const isCorrect = revealChoices.get(choiceId).correct === true;
    if (isCorrect) correct += 1;
    picks.set(item.id, {
      participant: LIGAND_PLDDT_BASELINE_IDENTITY,
      participant_kind: 'baseline',
      choice_id: choiceId,
      picked_none: false,
      selection_kind: 'exact',
      correct: isCorrect,
    });
  }
  if (picks.size !== publicArtifact.questions.length) {
    throw new WeeklyRetrospectiveError('pLDDT baseline item coverage is incomplete');
  }
  const total = picks.size;
  return {
    ...publicArtifact,
    automated_entries: [
      ...publicArtifact.automated_entries,
      {
        participant: LIGAND_PLDDT_BASELINE_IDENTITY,
        participant_kind: 'baseline',
        correct,
        answered: total,
        total,
        accuracy: roundPercent(correct, total),
        coverage: 100,
        complete: true,
      },
    ],
    questions: publicArtifact.questions.map(question => {
      const response = picks.get(question.item_id);
      if (!response) {
        throw new WeeklyRetrospectiveError('pLDDT baseline question is missing');
      }
      return {
        ...question,
        automated_entries: [...question.automated_entries, response]
          .sort((left, right) => left.participant.localeCompare(right.participant)),
      };
    }),
  };
}

export function publicationSummary(publication, {
  publicArtifact = null,
  context = null,
} = {}) {
  const result = {
    round_id: publication.roundId,
    campaign_id: publication.campaignId,
    opens_at: publication.opensAt,
    closes_at: publication.closesAt,
    revealed_at: publication.revealedAt,
    item_count: publication.itemCount,
    choice_count: publication.choiceCount,
  };
  if (context) {
    const blindWeeks = new Set(
      context.blindProjection.items.map(item => requiredText(item.week, 'blind item week')),
    );
    if (blindWeeks.size !== 1) {
      throw new WeeklyRetrospectiveError('publication has inconsistent blind weeks');
    }
    result.blind_week = [...blindWeeks][0];
  }
  if (publicArtifact && context) {
    const scoredArtifact = withLigandPlddtBaseline(publicArtifact, context);
    const outcomes = {
      pose_solved: 0,
      pose_unsolved: 0,
      none_solved: 0,
      none_unsolved: 0,
      suppressed: 0,
    };
    const questions = new Map(scoredArtifact.questions.map(row => [row.item_id, row]));
    for (const item of context.revealProjection.items) {
      const question = questions.get(item.id);
      if (!question) {
        throw new WeeklyRetrospectiveError('publication summary question is missing');
      }
      if (question.human_aggregate.suppressed) {
        outcomes.suppressed += 1;
        continue;
      }
      const hasCorrectPose = item.choices.some(choice => choice.correct === true);
      const solved = question.human_aggregate.correct_count > 0;
      outcomes[`${hasCorrectPose ? 'pose' : 'none'}_${solved ? 'solved' : 'unsolved'}`] += 1;
    }
    const automatedWinner = [...scoredArtifact.automated_entries].sort((left, right) => (
      right.correct - left.correct
      || right.accuracy - left.accuracy
      || left.participant.localeCompare(right.participant)
    ))[0] || null;
    result.summary = {
      human_participant_count: scoredArtifact.human_aggregate.participant_count,
      human_complete_count: scoredArtifact.human_aggregate.complete_count,
      human_partial_count: scoredArtifact.human_aggregate.partial_count,
      human_entries: scoredArtifact.human_entries,
      automated_entries: scoredArtifact.automated_entries,
      automated_winner: automatedWinner ? {
        participant: automatedWinner.participant,
        participant_kind: automatedWinner.participant_kind,
        correct: automatedWinner.correct,
        total: automatedWinner.total,
      } : null,
      outcomes,
    };
  }
  assertResponseSafe(result);
  return result;
}

export function verifyEvaluationAndRound({
  publication,
  evaluationRow,
  evaluationBytes,
  roundRow,
  assetOrigin,
}) {
  exactKeys(evaluationRow, EVALUATION_SELECT_FIELDS.split(','), 'evaluation catalog row');
  if (evaluationRow.evaluation_id !== publication.evaluationId
    || evaluationRow.round_id !== publication.roundId
    || evaluationRow.campaign_id !== publication.campaignId
    || evaluationRow.environment !== 'production'
    || evaluationRow.format_version !== PRIVATE_EVALUATION_FORMAT_VERSION
    || evaluationRow.artifact_media_type !== 'application/json'
    || evaluationRow.artifact_sha256 !== publication.digests.evaluation_artifact_sha256
    || evaluationRow.item_count !== publication.itemCount
    || evaluationRow.choice_count !== publication.choiceCount) {
    throw new WeeklyRetrospectiveError('evaluation catalog binding is invalid');
  }
  const expectedEvaluationId = stableId('weekly_eval', {
    artifact_sha256: evaluationRow.artifact_sha256,
    blind_manifest_sha256: evaluationRow.blind_manifest_sha256,
    format_version: evaluationRow.format_version,
    private_index_sha256: evaluationRow.private_index_sha256,
    round_id: evaluationRow.round_id,
  }, 32);
  if (expectedEvaluationId !== evaluationRow.evaluation_id) {
    throw new WeeklyRetrospectiveError('evaluation identity is not deterministic');
  }
  for (const [field, expected] of Object.entries({
    blind_manifest_sha256: publication.digests.blind_manifest_sha256,
    private_index_sha256: publication.digests.private_index_sha256,
    reveal_manifest_sha256: publication.digests.reveal_manifest_sha256,
    reference_set_sha256: publication.digests.reference_set_sha256,
    prediction_set_sha256: publication.digests.prediction_set_sha256,
  })) {
    if (evaluationRow[field] !== expected) {
      throw new WeeklyRetrospectiveError(`evaluation ${field} binding is invalid`);
    }
  }
  sameTimestamp(evaluationRow.round_opens_at, publication.opensAt, 'evaluation opens_at');
  sameTimestamp(evaluationRow.round_closes_at, publication.closesAt, 'evaluation closes_at');
  const evaluationDescriptor = {
    objectUri: evaluationRow.artifact_object_uri,
    sha256: requiredSha256(evaluationRow.artifact_sha256, 'evaluation artifact digest'),
    sizeBytes: positiveInt(evaluationRow.artifact_size_bytes, 'evaluation artifact size'),
    mediaType: 'application/json',
    ...parseSupabaseObjectUri(
      evaluationRow.artifact_object_uri,
      evaluationRow.artifact_sha256,
    ),
  };
  if (evaluationDescriptor.bucket !== publication.descriptors.admin_artifact.bucket) {
    throw new WeeklyRetrospectiveError('evaluation artifact bucket is invalid');
  }
  const evaluation = parseVerifiedJsonBytes(
    evaluationBytes,
    evaluationDescriptor,
    'evaluation artifact',
  );
  if (evaluation.format_version !== PRIVATE_EVALUATION_FORMAT_VERSION) {
    throw new WeeklyRetrospectiveError('evaluation artifact format is invalid');
  }
  exactKeys(evaluation.counts, ['choice_count', 'item_count'], 'evaluation counts');
  if (evaluation.counts.item_count !== publication.itemCount
    || evaluation.counts.choice_count !== publication.choiceCount) {
    throw new WeeklyRetrospectiveError('evaluation artifact counts are invalid');
  }
  const blindManifest = parseCanonicalManifest(
    evaluation.blind_manifest_canonical_json,
    evaluation.blind_manifest,
    publication.digests.blind_manifest_sha256,
    'blind manifest',
  );
  const revealManifest = parseCanonicalManifest(
    evaluation.reveal_manifest_canonical_json,
    evaluation.reveal_manifest,
    publication.digests.reveal_manifest_sha256,
    'reveal manifest',
  );
  if (revealManifest.round_id !== publication.roundId
    || revealManifest.blind_manifest_sha256 !== publication.digests.blind_manifest_sha256
    || evaluation.integrity?.reveal_manifest_sha256
      !== publication.digests.reveal_manifest_sha256
    || evaluation.integrity?.reference_set_sha256
      !== publication.digests.reference_set_sha256
    || evaluation.integrity?.prediction_set_sha256
      !== publication.digests.prediction_set_sha256) {
    throw new WeeklyRetrospectiveError('evaluation artifact integrity is invalid');
  }
  let answerOverlays;
  try {
    answerOverlays = buildAnswerOverlays(evaluation.answer_overlays, revealManifest);
  } catch {
    throw new WeeklyRetrospectiveError('evaluation answer overlays are invalid');
  }
  const answerOverlayDigest = createHash('sha256')
    .update(canonicalJson(answerOverlays), 'utf8')
    .digest('hex');
  if (requiredSha256(
    evaluation.integrity?.answer_overlay_set_sha256,
    'evaluation answer overlay digest',
  ) !== answerOverlayDigest) {
    throw new WeeklyRetrospectiveError('evaluation answer overlay integrity is invalid');
  }

  exactKeys(roundRow, ROUND_SELECT_FIELDS.split(','), 'weekly round row');
  if (roundRow.round_id !== publication.roundId
    || roundRow.campaign_id !== publication.campaignId
    || roundRow.environment !== 'production'
    || roundRow.status !== 'revealed'
    || roundRow.item_count !== publication.itemCount
    || roundRow.blind_manifest_sha256 !== publication.digests.blind_manifest_sha256
    || roundRow.reveal_manifest_sha256 !== publication.digests.reveal_manifest_sha256
    || !roundRow.revealed_at) {
    throw new WeeklyRetrospectiveError('weekly round binding is invalid');
  }
  sameTimestamp(roundRow.opens_at, publication.opensAt, 'round opens_at');
  sameTimestamp(roundRow.closes_at, publication.closesAt, 'round closes_at');
  sameTimestamp(roundRow.revealed_at, publication.revealedAt, 'round revealed_at');
  if (canonicalJson(roundRow.blind_manifest) !== canonicalJson(blindManifest)
    || canonicalJson(roundRow.reveal_manifest) !== canonicalJson(revealManifest)) {
    throw new WeeklyRetrospectiveError('weekly round manifests differ from evaluation');
  }
  return {
    blindManifest,
    revealManifest,
    blindProjection: projectBlindManifest(blindManifest, publication, assetOrigin),
    revealProjection: projectRevealManifest(revealManifest, publication),
    answerOverlayProjection: projectAnswerOverlays(answerOverlays),
    evaluationDescriptor,
  };
}

export function verifyPublicArtifact(bytes, publication) {
  const artifact = parseVerifiedJsonBytes(
    bytes,
    publication.descriptors.public_artifact,
    'public retrospective artifact',
  );
  exactKeys(
    artifact,
    ['automated_entries', 'format_version', 'human_aggregate', 'questions', 'round'],
    'public retrospective artifact',
  );
  if (artifact.format_version !== PUBLIC_ARTIFACT_FORMAT_VERSION) {
    throw new WeeklyRetrospectiveError('public artifact format is invalid');
  }
  const round = verifyArtifactRound(artifact.round, publication);
  exactKeys(
    artifact.human_aggregate,
    [
      'complete_count',
      'participant_count',
      'partial_count',
      'score_distribution',
      'suppressed',
    ],
    'public human aggregate',
  );
  const humanSuppressed = requiredBoolean(
    artifact.human_aggregate.suppressed,
    'human aggregate suppressed',
  );
  const humanAggregate = {
    participant_count: nonnegativeInt(
      artifact.human_aggregate.participant_count,
      'human participant_count',
    ),
    suppressed: humanSuppressed,
    complete_count: humanSuppressed
      ? requiredNull(artifact.human_aggregate.complete_count, 'human complete_count')
      : nonnegativeInt(artifact.human_aggregate.complete_count, 'human complete_count'),
    partial_count: humanSuppressed
      ? requiredNull(artifact.human_aggregate.partial_count, 'human partial_count')
      : nonnegativeInt(artifact.human_aggregate.partial_count, 'human partial_count'),
    score_distribution: requireArray(
      artifact.human_aggregate.score_distribution,
      'human score_distribution',
    ).map((row, index) => {
      exactKeys(
        row,
        ['answered', 'correct', 'participant_count'],
        `human score_distribution[${index}]`,
      );
      return {
        correct: nonnegativeInt(row.correct, 'distribution correct'),
        answered: nonnegativeInt(row.answered, 'distribution answered'),
        participant_count: positiveInt(
          row.participant_count,
          'distribution participant_count',
        ),
      };
    }),
  };
  if (humanSuppressed && humanAggregate.score_distribution.length) {
    throw new WeeklyRetrospectiveError('suppressed human distribution must be empty');
  }
  if (!humanSuppressed && humanAggregate.complete_count + humanAggregate.partial_count
      > humanAggregate.participant_count) {
    throw new WeeklyRetrospectiveError('public human aggregate counts are invalid');
  }
  const automatedEntries = requireArray(
    artifact.automated_entries,
    'public automated entries',
  ).map((row, index) => verifyResultRow(row, `automated_entries[${index}]`, false));
  requireUniqueParticipants(automatedEntries, 'public automated entries');
  if (automatedEntries.filter(row => row.participant_kind === 'baseline').length !== 1
    || automatedEntries.some(row => row.total !== publication.itemCount)) {
    throw new WeeklyRetrospectiveError('public automated entries are incomplete');
  }
  const questions = requireArray(artifact.questions, 'public questions')
    .map((question, index) => verifyPublicQuestion(question, index, publication));
  verifyQuestionCoverage(questions, publication.itemCount);
  const result = {
    format_version: PUBLIC_ARTIFACT_FORMAT_VERSION,
    round,
    human_aggregate: humanAggregate,
    automated_entries: automatedEntries,
    questions,
  };
  assertResponseSafe(result);
  return result;
}

export function verifyAdminArtifact(bytes, publication) {
  const artifact = parseVerifiedJsonBytes(
    bytes,
    publication.descriptors.admin_artifact,
    'admin retrospective artifact',
  );
  exactKeys(
    artifact,
    ['format_version', 'participants', 'questions', 'round'],
    'admin retrospective artifact',
  );
  if (artifact.format_version !== ADMIN_ARTIFACT_FORMAT_VERSION) {
    throw new WeeklyRetrospectiveError('admin artifact format is invalid');
  }
  const round = verifyArtifactRound(artifact.round, publication);
  const nameMap = new Map();
  const participants = requireArray(artifact.participants, 'admin participants')
    .map((row, index) => {
      const verified = verifyResultRow(row, `participants[${index}]`, true);
      const safeName = verified.participant_kind === 'human'
        ? sanitizePseudonym(verified.participant)
        : verified.participant;
      nameMap.set(verified.participant, safeName);
      return { ...verified, participant: safeName };
    });
  requireUniqueParticipants(participants, 'admin participants', true);
  if (participants.filter(row => row.participant_kind === 'baseline').length !== 1
    || participants.some(row => row.total !== publication.itemCount)) {
    throw new WeeklyRetrospectiveError('admin participants are incomplete');
  }
  const questions = requireArray(artifact.questions, 'admin questions').map(
    (question, index) => {
      exactKeys(question, ['item_id', 'responses'], `admin questions[${index}]`);
      return {
        item_id: requiredText(question.item_id, 'admin question item_id'),
        responses: requireArray(question.responses, 'admin question responses').map(
          (response, responseIndex) => {
            const verified = verifyAutomatedResponse(
              response,
              `admin questions[${index}].responses[${responseIndex}]`,
              true,
              publication,
            );
            if (!nameMap.has(verified.participant)) {
              throw new WeeklyRetrospectiveError('admin response participant is unknown');
            }
            return {
              ...verified,
              participant: nameMap.get(verified.participant),
            };
          },
        ),
      };
    },
  );
  verifyQuestionCoverage(questions, publication.itemCount);
  const result = {
    format_version: ADMIN_ARTIFACT_FORMAT_VERSION,
    round,
    participants,
    questions,
  };
  assertResponseSafe(result);
  return result;
}

export function verifySourceSnapshot(bytes, publication) {
  const source = parseVerifiedJsonBytes(
    bytes,
    publication.descriptors.source_snapshot,
    'retrospective source snapshot',
  );
  exactKeys(
    source,
    ['format_version', 'participants', 'round_id', 'votes'],
    'retrospective source snapshot',
  );
  if (source.format_version !== SOURCE_SNAPSHOT_FORMAT_VERSION
    || source.round_id !== publication.roundId) {
    throw new WeeklyRetrospectiveError('source snapshot binding is invalid');
  }
  const participants = requireArray(source.participants, 'source participants').map(
    (row, index) => {
      exactKeys(
        row,
        [
          'automated_identity',
          'current_session_count',
          'display_name',
          'participant_kind',
          'participant_link',
        ],
        `source participants[${index}]`,
      );
      const participantLink = requiredUuid(
        row.participant_link,
        'source participant_link',
      );
      const currentSessionCount = nonnegativeInt(
        row.current_session_count,
        'source current_session_count',
      );
      if (row.participant_kind === 'human') {
        if (row.automated_identity !== null) {
          throw new WeeklyRetrospectiveError('human source identity is invalid');
        }
        return {
          participantLink,
          participantKind: 'human',
          displayName: normalizePseudonym(row.display_name),
          currentSessionCount,
        };
      }
      if (row.participant_kind !== 'automated'
        || !APPROVED_LLM_SET.has(row.automated_identity)
        || row.display_name !== null) {
        throw new WeeklyRetrospectiveError('automated source identity is invalid');
      }
      return {
        participantLink,
        participantKind: 'llm',
        displayName: row.automated_identity,
        currentSessionCount,
      };
    },
  );
  const participantMap = new Map(participants.map(row => [row.participantLink, row]));
  if (participantMap.size !== participants.length) {
    throw new WeeklyRetrospectiveError('source participants are duplicated');
  }
  const votes = requireArray(source.votes, 'source votes').map((row, index) => {
    exactKeys(
      row,
      ['choice_id', 'item_id', 'participant_link', 'picked_none', 'selection_kind'],
      `source votes[${index}]`,
    );
    const participantLink = requiredUuid(row.participant_link, 'source vote participant_link');
    if (!participantMap.has(participantLink)) {
      throw new WeeklyRetrospectiveError('source vote participant is unknown');
    }
    if (typeof row.picked_none !== 'boolean') {
      throw new WeeklyRetrospectiveError('source vote picked_none is invalid');
    }
    const choiceId = row.picked_none
      ? (row.choice_id === null ? null : invalid('picked-none source choice is invalid'))
      : requiredText(row.choice_id, 'source vote choice_id');
    const legacyExact = !row.picked_none
      && publication.roundId === LEGACY_EXACT_SCOPE_ROUND_ID;
    const allowedSelectionKinds = row.picked_none
      ? new Set(['none']) : new Set(['exact', 'cluster']);
    if (
      (!legacyExact && !allowedSelectionKinds.has(row.selection_kind))
      || (legacyExact && !new Set(['exact', 'cluster', 'unknown']).has(row.selection_kind))
    ) {
      throw new WeeklyRetrospectiveError('source vote selection_kind is invalid');
    }
    return {
      participantLink,
      itemId: requiredText(row.item_id, 'source vote item_id'),
      choiceId,
      pickedNone: row.picked_none,
      selectionKind: legacyExact ? 'exact' : row.selection_kind,
    };
  });
  return { participants, participantMap, votes };
}

export function publishHumanPseudonyms({
  publication,
  publicArtifact,
  adminArtifact,
}) {
  const humanEntries = adminArtifact.participants
    .filter(row => row.participant_kind === 'human')
    .map(row => ({ ...row }));
  if (humanEntries.length !== publicArtifact.human_aggregate.participant_count) {
    throw new WeeklyRetrospectiveError('human participant projections are inconsistent');
  }
  const distribution = new Map();
  for (const row of humanEntries) {
    const key = `${row.correct}:${row.answered}`;
    distribution.set(key, {
      correct: row.correct,
      answered: row.answered,
      participant_count: (distribution.get(key)?.participant_count || 0) + 1,
    });
  }
  const adminQuestionMap = new Map(
    adminArtifact.questions.map(question => [question.item_id, question]),
  );
  const questions = publicArtifact.questions.map(question => {
    const adminQuestion = adminQuestionMap.get(question.item_id);
    if (!adminQuestion) {
      throw new WeeklyRetrospectiveError('admin question coverage is inconsistent');
    }
    const humanResponses = adminQuestion.responses
      .filter(response => response.participant_kind === 'human')
      .map(response => {
        if (response.picked_none || response.selection_kind !== 'unknown') {
          return publication.roundId === LEGACY_EXACT_SCOPE_ROUND_ID
            && !response.picked_none
            ? { ...response, selection_kind: 'exact' }
            : response;
        }
        if (publication.roundId !== LEGACY_EXACT_SCOPE_ROUND_ID) {
          throw new WeeklyRetrospectiveError('human vote scope is unavailable');
        }
        return { ...response, selection_kind: 'exact' };
      });
    if (humanResponses.length !== question.human_aggregate.answered_count) {
      throw new WeeklyRetrospectiveError('human question projections are inconsistent');
    }
    const grouped = new Map();
    for (const response of humanResponses) {
      const key = JSON.stringify([
        response.choice_id,
        response.picked_none,
        response.selection_kind,
        response.correct,
      ]);
      const current = grouped.get(key) || {
        choice_id: response.choice_id,
        picked_none: response.picked_none,
        selection_kind: response.selection_kind,
        correct: response.correct,
        vote_count: 0,
        display_names: [],
      };
      current.vote_count += 1;
      if (!current.display_names.includes(response.participant)) {
        current.display_names.push(response.participant);
      }
      grouped.set(key, current);
    }
    const answers = [...grouped.values()]
      .map(row => ({ ...row, display_names: row.display_names.sort() }))
      .sort((left, right) => (
        right.vote_count - left.vote_count
        || Number(right.correct) - Number(left.correct)
        || String(left.choice_id || '').localeCompare(String(right.choice_id || ''))
      ));
    return {
      ...question,
      human_aggregate: {
        answered_count: humanResponses.length,
        suppressed: false,
        correct_count: humanResponses.filter(response => response.correct).length,
        answers,
      },
      automated_entries: question.automated_entries.map(response => (
        publication.roundId === LEGACY_EXACT_SCOPE_ROUND_ID && !response.picked_none
          ? { ...response, selection_kind: 'exact' }
          : response
      )),
    };
  });
  const result = {
    ...publicArtifact,
    human_aggregate: {
      participant_count: humanEntries.length,
      suppressed: false,
      complete_count: humanEntries.filter(row => row.complete).length,
      partial_count: humanEntries.filter(
        row => row.answered > 0 && row.answered < row.total,
      ).length,
      score_distribution: [...distribution.values()].sort(
        (left, right) => right.correct - left.correct || right.answered - left.answered,
      ),
    },
    human_entries: humanEntries,
    questions,
  };
  assertResponseSafe(result);
  return result;
}

export function buildPublicDetail({
  publication,
  context,
  publicArtifact,
}) {
  const scoredArtifact = withLigandPlddtBaseline(publicArtifact, context);
  const response = {
    format_version: ARCHIVE_DETAIL_FORMAT_VERSION,
    round: publicationSummary(publication, { context }),
    blind_manifest: context.blindProjection,
    reveal_manifest: context.revealProjection,
    answer_overlays: context.answerOverlayProjection,
    retrospective: scoredArtifact,
  };
  assertResponseSafe(response);
  return response;
}

export function buildAdminDetail({
  publication,
  context,
  adminArtifact,
}) {
  const response = {
    format_version: ARCHIVE_ADMIN_DETAIL_FORMAT_VERSION,
    round: publicationSummary(publication, { context }),
    blind_manifest: context.blindProjection,
    reveal_manifest: context.revealProjection,
    answer_overlays: context.answerOverlayProjection,
    retrospective: adminArtifact,
  };
  assertResponseSafe(response);
  return response;
}

export function buildPublicAllTime(
  weeks,
  {
    ranking = 'total_correct',
    participantKind = null,
  } = {},
) {
  validateRankingOptions(ranking, participantKind, false);
  const aggregates = new Map();
  for (const { publication, publicArtifact, context } of weeks) {
    const scoredArtifact = withLigandPlddtBaseline(publicArtifact, context);
    for (const row of scoredArtifact.automated_entries) {
      const identity = row.participant;
      const aggregate = aggregates.get(identity) || newAggregate(
        identity,
        row.participant_kind,
      );
      addWeek(aggregate, row, publication.revealedAt);
      aggregates.set(identity, aggregate);
    }
  }
  return buildAllTimeResponse(
    [...aggregates.values()],
    { ranking, participantKind, admin: false },
  );
}

export function buildAdminAllTime(
  weeks,
  {
    hmacKey,
    ranking = 'total_correct',
    participantKind = null,
  } = {},
) {
  if (typeof hmacKey !== 'string' || hmacKey.length < 32) {
    throw new WeeklyRetrospectiveError('participant HMAC key is unavailable');
  }
  return buildLinkedAllTime(weeks, {
    ranking,
    participantKind,
    admin: true,
    humanKey: row => createHmac('sha256', hmacKey)
      .update(row.participantLink)
      .digest('base64url'),
  });
}

export function buildPublicHumanAllTime(
  weeks,
  {
    ranking = 'total_correct',
    participantKind = null,
  } = {},
) {
  return buildLinkedAllTime(weeks, {
    ranking,
    participantKind,
    admin: false,
    humanKey: row => row.participantLink,
  });
}

function buildLinkedAllTime(
  weeks,
  {
    ranking,
    participantKind,
    admin,
    humanKey,
  },
) {
  validateRankingOptions(ranking, participantKind, true);
  const aggregates = new Map();
  for (const {
    publication,
    sourceSnapshot,
    publicArtifact,
    context,
  } of weeks) {
    const scored = scoreSourceWeek(sourceSnapshot, context.revealManifest, publication);
    for (const row of scored) {
      const key = row.participantKind === 'human'
        ? `human:${humanKey(row)}`
        : `llm:${row.participant}`;
      const aggregate = aggregates.get(key) || newAggregate(
        row.participant,
        row.participantKind,
      );
      addWeek(aggregate, row, publication.revealedAt);
      if (row.participantKind === 'human'
        && Date.parse(publication.revealedAt) >= Date.parse(aggregate.latestNameAt || '1970-01-01')) {
        aggregate.participant = normalizePseudonym(row.participant);
        aggregate.latestNameAt = publication.revealedAt;
      }
      aggregates.set(key, aggregate);
    }
    const baseline = publicArtifact.automated_entries.find(
      row => row.participant_kind === 'baseline' && row.participant === BASELINE_IDENTITY,
    );
    if (!baseline) {
      throw new WeeklyRetrospectiveError('baseline entry is missing');
    }
    const baselineAggregate = aggregates.get('baseline:Smina')
      || newAggregate(BASELINE_IDENTITY, 'baseline');
    addWeek(baselineAggregate, baseline, publication.revealedAt);
    aggregates.set('baseline:Smina', baselineAggregate);
  }
  return buildAllTimeResponse(
    [...aggregates.values()],
    { ranking, participantKind, admin },
  );
}

export function assertResponseSafe(value) {
  const visit = node => {
    if (Array.isArray(node)) {
      node.forEach(visit);
      return;
    }
    if (node && typeof node === 'object') {
      for (const [key, child] of Object.entries(node)) {
        if (FORBIDDEN_RESPONSE_KEYS.has(key.toLowerCase())
          || key.toLowerCase().endsWith('_sha256')
          || key.toLowerCase().endsWith('_object_uri')) {
          throw new WeeklyRetrospectiveError(`response contains forbidden field ${key}`);
        }
        if (typeof child === 'string' && URI.test(child)
          && PUBLIC_ASSET_RESPONSE_KEYS.has(key.toLowerCase())
          && /^https:\/\/[^/]+\/storage\/v1\/object\/public\/[^/]+\/.+/.test(child)) {
          continue;
        }
        if (typeof child === 'string' && URI.test(child)
          && PUBLIC_REFERENCE_RESPONSE_KEYS.has(key.toLowerCase())
          && /^https:\/\/files\.rcsb\.org\/download\/[A-Z0-9]{4}\.cif\.gz$/.test(child)) {
          continue;
        }
        visit(child);
      }
      return;
    }
    if (typeof node === 'string' && URI.test(node)) {
      throw new WeeklyRetrospectiveError('response contains a URI');
    }
  };
  visit(value);
}

function parseVerifiedJsonBytes(bytes, descriptor, field) {
  if (!Buffer.isBuffer(bytes) || bytes.length !== descriptor.sizeBytes
    || createHash('sha256').update(bytes).digest('hex') !== descriptor.sha256
    || descriptor.mediaType !== 'application/json') {
    throw new WeeklyRetrospectiveError(`${field} descriptor is inconsistent`);
  }
  let value;
  try {
    value = JSON.parse(bytes.toString('utf8'));
  } catch {
    throw new WeeklyRetrospectiveError(`${field} is not valid JSON`);
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new WeeklyRetrospectiveError(`${field} must be an object`);
  }
  return value;
}

function parseCanonicalManifest(raw, parsed, expectedDigest, field) {
  if (typeof raw !== 'string' || !raw) {
    throw new WeeklyRetrospectiveError(`${field} canonical JSON is missing`);
  }
  let fromCanonical;
  try {
    fromCanonical = JSON.parse(raw);
  } catch {
    throw new WeeklyRetrospectiveError(`${field} canonical JSON is invalid`);
  }
  if (createHash('sha256').update(raw, 'utf8').digest('hex') !== expectedDigest
    || canonicalJson(fromCanonical) !== canonicalJson(parsed)) {
    throw new WeeklyRetrospectiveError(`${field} canonical binding is invalid`);
  }
  return fromCanonical;
}

function projectBlindManifest(manifest, publication, assetOrigin) {
  if (!manifest || typeof manifest !== 'object' || !Array.isArray(manifest.items)
    || manifest.round_id !== publication.roundId
    || manifest.items.length !== publication.itemCount) {
    throw new WeeklyRetrospectiveError('blind manifest shape is invalid');
  }
  return {
    schema_version: positiveInt(manifest.schema_version, 'blind schema_version'),
    round_id: publication.roundId,
    items: manifest.items.map((item, itemIndex) => {
      if (!item || typeof item !== 'object' || !Array.isArray(item.choices)
        || !item.choices.length) {
        throw new WeeklyRetrospectiveError(`blind item ${itemIndex} is invalid`);
      }
      const projectedItem = {
        id: requiredText(item.id, 'blind item id'),
        ligand: projectLigand(item.ligand),
        week: requiredText(item.week, 'blind item week'),
        choices: item.choices.map((choice, choiceIndex) => {
          const projected = {
            id: requiredText(choice?.id, `blind choice ${choiceIndex} id`),
            pose_uri: projectPublicAssetUri(
              choice?.pose_uri,
              assetOrigin,
              `blind choice ${choiceIndex} pose_uri`,
            ),
          };
          for (const field of ['protein_uri', 'pocket_uri']) {
            if (choice?.[field] != null) {
              projected[field] = projectPublicAssetUri(
                choice[field],
                assetOrigin,
                `blind choice ${choiceIndex} ${field}`,
              );
            }
          }
          for (const field of ['method', 'method_version']) {
            if (choice?.[field] != null) {
              projected[field] = requiredText(choice[field], `blind choice ${field}`);
            }
          }
          if (choice.confidence != null) {
            projected.confidence = projectConfidence(choice.confidence);
          }
          if (choice.cluster_id != null) {
            projected.cluster_id = requiredText(choice.cluster_id, 'blind cluster_id');
          }
          if (choice.is_rep != null) {
            if (typeof choice.is_rep !== 'boolean') {
              throw new WeeklyRetrospectiveError('blind is_rep is invalid');
            }
            projected.is_rep = choice.is_rep;
          }
          if (choice.smina_score != null) {
            exactKeys(
              choice.smina_score,
              ['metric', 'protocol', 'scoring_function', 'units', 'value'],
              'blind smina_score',
            );
            projected.smina_score = {
              metric: requiredText(choice.smina_score.metric, 'smina metric'),
              protocol: requiredText(choice.smina_score.protocol, 'smina protocol'),
              scoring_function: requiredText(
                choice.smina_score.scoring_function,
                'smina scoring_function',
              ),
              units: requiredText(choice.smina_score.units, 'smina units'),
              value: finiteNumber(choice.smina_score.value, 'smina value'),
            };
          }
          if (choice.interaction_count != null) {
            exactKeys(
              choice.interaction_count,
              ['metric', 'policy', 'value'],
              'blind interaction_count',
            );
            projected.interaction_count = {
              metric: requiredText(
                choice.interaction_count.metric,
                'blind interaction_count metric',
              ),
              policy: requiredText(
                choice.interaction_count.policy,
                'blind interaction_count policy',
              ),
              value: nonnegativeInt(
                choice.interaction_count.value,
                'blind interaction_count value',
              ),
            };
          }
          return projected;
        }),
      };
      for (const field of ['protein_uri', 'pocket_uri']) {
        if (item[field] != null) {
          projectedItem[field] = projectPublicAssetUri(
            item[field],
            assetOrigin,
            `blind item ${field}`,
          );
        }
      }
      projectedItem.metadata = projectItemMetadata(item.metadata);
      if (!projectedItem.protein_uri
        && !projectedItem.choices.some(choice => choice.protein_uri)) {
        throw new WeeklyRetrospectiveError(`blind item ${itemIndex} has no protein asset`);
      }
      return projectedItem;
    }),
  };
}

function projectLigand(value) {
  if (typeof value === 'string') return requiredText(value, 'blind ligand');
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new WeeklyRetrospectiveError('blind ligand is invalid');
  }
  const result = {};
  for (const field of ['component_id', 'name']) {
    if (value[field] != null) result[field] = requiredText(value[field], `blind ligand ${field}`);
  }
  if (value.heavy_atoms != null) {
    result.heavy_atoms = positiveInt(value.heavy_atoms, 'blind ligand heavy_atoms');
  }
  if (!Object.keys(result).length) {
    throw new WeeklyRetrospectiveError('blind ligand is empty');
  }
  return result;
}

function projectItemMetadata(value) {
  if (value == null) return {};
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new WeeklyRetrospectiveError('blind item metadata is invalid');
  }
  const result = {};
  if (value.presentation != null) {
    const presentation = value.presentation;
    if (!presentation || typeof presentation !== 'object' || Array.isArray(presentation)) {
      throw new WeeklyRetrospectiveError('blind presentation metadata is invalid');
    }
    result.presentation = {
      policy: requiredText(presentation.policy, 'blind presentation policy'),
      group: requiredText(presentation.group, 'blind presentation group'),
      cluster_count: positiveInt(
        presentation.cluster_count,
        'blind presentation cluster_count',
      ),
    };
  }
  if (value.display_alignment != null) {
    const alignment = value.display_alignment;
    exactKeys(alignment, ['code', 'message'], 'blind display_alignment metadata');
    result.display_alignment = {
      code: requiredText(alignment.code, 'blind display_alignment code'),
      message: requiredText(alignment.message, 'blind display_alignment message'),
    };
  }
  return result;
}

function projectConfidence(value) {
  exactKeys(
    value,
    ['aggregation', 'metric', 'scale_max', 'scale_min', 'value'],
    'blind confidence',
  );
  const scaleMin = finiteNumber(value.scale_min, 'blind confidence scale_min');
  const scaleMax = finiteNumber(value.scale_max, 'blind confidence scale_max');
  const metricValue = finiteNumber(value.value, 'blind confidence value');
  if (scaleMin > metricValue || metricValue > scaleMax) {
    throw new WeeklyRetrospectiveError('blind confidence value is outside its scale');
  }
  return {
    metric: requiredText(value.metric, 'blind confidence metric'),
    value: metricValue,
    scale_min: scaleMin,
    scale_max: scaleMax,
    aggregation: requiredText(value.aggregation, 'blind confidence aggregation'),
  };
}

export function projectPublicAssetUri(value, assetOrigin, field = 'asset URI') {
  let configured;
  try {
    configured = new URL(assetOrigin);
  } catch {
    throw new WeeklyRetrospectiveError(`${field} origin is invalid`);
  }
  if (configured.protocol !== 'https:' || configured.origin !== assetOrigin) {
    throw new WeeklyRetrospectiveError(`${field} origin is invalid`);
  }
  let bucket;
  let rawSegments;
  if (typeof value === 'string' && value.startsWith('supabase://')) {
    const match = /^supabase:\/\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\/(.+)$/.exec(value);
    if (!match || value.includes('?') || value.includes('#')) {
      throw new WeeklyRetrospectiveError(`${field} is invalid`);
    }
    bucket = match[1];
    rawSegments = match[2].split('/');
  } else {
    let parsed;
    if (typeof value !== 'string'
      || /(?:^|\/)(?:\.|%2e){1,2}(?:\/|$)/i.test(value)) {
      throw new WeeklyRetrospectiveError(`${field} path is invalid`);
    }
    try {
      parsed = new URL(value);
    } catch {
      throw new WeeklyRetrospectiveError(`${field} is invalid`);
    }
    const prefix = '/storage/v1/object/public/';
    if (parsed.protocol !== 'https:' || parsed.origin !== configured.origin
      || parsed.username || parsed.password || parsed.search || parsed.hash
      || !parsed.pathname.startsWith(prefix)) {
      throw new WeeklyRetrospectiveError(`${field} is not a configured public asset`);
    }
    const parts = parsed.pathname.slice(prefix.length).split('/');
    bucket = parts.shift();
    rawSegments = parts;
  }
  if (!bucket || !rawSegments?.length) {
    throw new WeeklyRetrospectiveError(`${field} path is invalid`);
  }
  const segments = rawSegments.map(segment => {
    let decoded;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      throw new WeeklyRetrospectiveError(`${field} path is invalid`);
    }
    if (!decoded || decoded === '.' || decoded === '..'
      || decoded.includes('/') || decoded.includes('\\')
      || /[\u0000-\u001f\u007f]/.test(decoded)) {
      throw new WeeklyRetrospectiveError(`${field} path is invalid`);
    }
    return encodeURIComponent(decoded);
  });
  return `${configured.origin}/storage/v1/object/public/${encodeURIComponent(bucket)}/${segments.join('/')}`;
}

function projectRevealManifest(manifest, publication) {
  if (!manifest || typeof manifest !== 'object' || !Array.isArray(manifest.items)
    || manifest.round_id !== publication.roundId
    || manifest.items.length !== publication.itemCount) {
    throw new WeeklyRetrospectiveError('reveal manifest shape is invalid');
  }
  let choiceCount = 0;
  const items = manifest.items.map((item, itemIndex) => {
    if (!item || typeof item !== 'object' || !Array.isArray(item.choices)
      || !item.choices.length) {
      throw new WeeklyRetrospectiveError(`reveal item ${itemIndex} is invalid`);
    }
    choiceCount += item.choices.length;
    const itemId = requiredText(item.id, 'reveal item id');
    return {
      id: itemId,
      choices: item.choices.map((choice, choiceIndex) => {
        if (typeof choice?.correct !== 'boolean'
          || typeof choice.accepted_correct !== 'boolean') {
          throw new WeeklyRetrospectiveError(`reveal choice ${choiceIndex} is invalid`);
        }
        return {
          id: requiredText(choice.id, 'reveal choice id'),
          correct: choice.correct,
          accepted_correct: choice.accepted_correct,
          rmsd: finiteNumber(choice.rmsd, 'reveal rmsd'),
          reference_uri: projectRcsbReferenceUri(
            choice.reference_uri,
            itemId,
            `reveal choice ${choiceIndex} reference_uri`,
          ),
        };
      }),
    };
  });
  if (choiceCount !== publication.choiceCount) {
    throw new WeeklyRetrospectiveError('reveal choice_count is invalid');
  }
  return {
    schema_version: positiveInt(manifest.schema_version, 'reveal schema_version'),
    round_id: publication.roundId,
    items,
  };
}

function projectRcsbReferenceUri(value, itemId, field) {
  if (!/^[A-Za-z0-9]{4}$/.test(itemId)) {
    throw new WeeklyRetrospectiveError('reveal item is not a 4-character PDB target');
  }
  const canonical = `https://files.rcsb.org/download/${itemId.toUpperCase()}.cif.gz`;
  if (value !== canonical) {
    throw new WeeklyRetrospectiveError(`${field} is not the canonical RCSB reference`);
  }
  return canonical;
}

function projectAnswerOverlays(overlays) {
  return overlays.map(row => ({
    item_id: row.item_id,
    crystal_ligand_pdb: row.crystal_ligand_pdb,
    poses: row.poses.map(pose => ({
      id: pose.id,
      rmsd: pose.rmsd,
      correct: pose.correct,
      predicted_pose_pdb: pose.predicted_pose_pdb,
      crystal_ligand_pdb: pose.crystal_ligand_pdb,
      crystal_pocket_pdb: pose.crystal_pocket_pdb,
    })),
  }));
}

function verifyArtifactRound(round, publication) {
  exactKeys(
    round,
    [
      'campaign_id',
      'choice_count',
      'closes_at',
      'item_count',
      'opens_at',
      'revealed_at',
      'round_id',
    ],
    'artifact round',
  );
  if (round.round_id !== publication.roundId
    || round.campaign_id !== publication.campaignId
    || round.item_count !== publication.itemCount
    || round.choice_count !== publication.choiceCount) {
    throw new WeeklyRetrospectiveError('artifact round identity is invalid');
  }
  sameTimestamp(round.opens_at, publication.opensAt, 'artifact opens_at');
  sameTimestamp(round.closes_at, publication.closesAt, 'artifact closes_at');
  sameTimestamp(round.revealed_at, publication.revealedAt, 'artifact revealed_at');
  return {
    round_id: publication.roundId,
    campaign_id: publication.campaignId,
    opens_at: publication.opensAt,
    closes_at: publication.closesAt,
    revealed_at: publication.revealedAt,
    item_count: publication.itemCount,
    choice_count: publication.choiceCount,
  };
}

function verifyResultRow(row, field, allowHuman) {
  exactKeys(
    row,
    [
      'accuracy',
      'answered',
      'complete',
      'correct',
      'coverage',
      'participant',
      'participant_kind',
      'total',
    ],
    field,
  );
  const participant = requiredText(row.participant, `${field} participant`);
  if (!PARTICIPANT_KIND_SET.has(row.participant_kind)
    || (!allowHuman && row.participant_kind === 'human')) {
    throw new WeeklyRetrospectiveError(`${field} participant_kind is invalid`);
  }
  if (row.participant_kind === 'llm' && !APPROVED_LLM_SET.has(participant)) {
    throw new WeeklyRetrospectiveError(`${field} LLM identity is not approved`);
  }
  if (row.participant_kind === 'baseline' && participant !== BASELINE_IDENTITY) {
    throw new WeeklyRetrospectiveError(`${field} baseline identity is invalid`);
  }
  const correct = nonnegativeInt(row.correct, `${field} correct`);
  const answered = nonnegativeInt(row.answered, `${field} answered`);
  const total = positiveInt(row.total, `${field} total`);
  if (correct > answered || answered > total
    || row.complete !== (answered === total)
    || finitePercent(row.accuracy, `${field} accuracy`) !== row.accuracy
    || finitePercent(row.coverage, `${field} coverage`) !== row.coverage
    || row.accuracy !== (answered ? roundPercent(correct, answered) : 0)
    || row.coverage !== roundPercent(answered, total)) {
    throw new WeeklyRetrospectiveError(`${field} score is invalid`);
  }
  return {
    participant,
    participant_kind: row.participant_kind,
    correct,
    answered,
    total,
    accuracy: row.accuracy,
    coverage: row.coverage,
    complete: row.complete,
  };
}

function verifyPublicQuestion(question, index, publication) {
  exactKeys(
    question,
    ['automated_entries', 'human_aggregate', 'item_id'],
    `public questions[${index}]`,
  );
  exactKeys(
    question.human_aggregate,
    ['answered_count', 'answers', 'correct_count', 'suppressed'],
    `public questions[${index}] human_aggregate`,
  );
  const suppressed = requiredBoolean(
    question.human_aggregate.suppressed,
    'question suppressed',
  );
  const answeredCount = nonnegativeInt(
    question.human_aggregate.answered_count,
    'question answered_count',
  );
  const correctCount = suppressed
    ? requiredNull(question.human_aggregate.correct_count, 'question correct_count')
    : nonnegativeInt(question.human_aggregate.correct_count, 'question correct_count');
  if (!suppressed && correctCount > answeredCount) {
    throw new WeeklyRetrospectiveError('question aggregate counts are invalid');
  }
  const answers = requireArray(
    question.human_aggregate.answers,
    'question human answers',
  ).map((answer, answerIndex) => {
    exactKeys(
      answer,
      ['choice_id', 'correct', 'picked_none', 'selection_kind', 'vote_count'],
      `question human answers[${answerIndex}]`,
    );
    if (typeof answer.picked_none !== 'boolean' || typeof answer.correct !== 'boolean') {
      throw new WeeklyRetrospectiveError('question human answer is invalid');
    }
    return {
      choice_id: answer.picked_none
        ? (answer.choice_id === null ? null : invalid('picked-none answer is invalid'))
        : requiredText(answer.choice_id, 'human answer choice_id'),
      picked_none: answer.picked_none,
      selection_kind: requiredSelectionKind(
        answer.selection_kind,
        answer.picked_none,
        publication,
      ),
      correct: answer.correct,
      vote_count: positiveInt(answer.vote_count, 'human answer vote_count'),
    };
  });
  if (suppressed && answers.length) {
    throw new WeeklyRetrospectiveError('suppressed question answers must be empty');
  }
  if (answers.reduce((sum, answer) => sum + answer.vote_count, 0) !== answeredCount) {
    if (!suppressed) {
      throw new WeeklyRetrospectiveError('question human answer totals are invalid');
    }
  }
  if (!suppressed && answers.filter(answer => answer.correct)
    .reduce((sum, answer) => sum + answer.vote_count, 0) !== correctCount) {
    throw new WeeklyRetrospectiveError('question human correct total is invalid');
  }
  return {
    item_id: requiredText(question.item_id, 'public question item_id'),
    human_aggregate: {
      answered_count: answeredCount,
      correct_count: correctCount,
      suppressed,
      answers,
    },
    automated_entries: requireArray(
      question.automated_entries,
      'question automated entries',
    ).map((response, responseIndex) => verifyAutomatedResponse(
      response,
      `question automated_entries[${responseIndex}]`,
      false,
      publication,
    )),
  };
}

function verifyAutomatedResponse(response, field, allowHuman, publication) {
  exactKeys(
    response,
    [
      'choice_id',
      'correct',
      'participant',
      'participant_kind',
      'picked_none',
      'selection_kind',
    ],
    field,
  );
  const participant = requiredText(response.participant, `${field} participant`);
  if (!PARTICIPANT_KIND_SET.has(response.participant_kind)
    || (!allowHuman && response.participant_kind === 'human')
    || (response.participant_kind === 'llm' && !APPROVED_LLM_SET.has(participant))
    || (response.participant_kind === 'baseline' && participant !== BASELINE_IDENTITY)
    || typeof response.picked_none !== 'boolean'
    || typeof response.correct !== 'boolean') {
    throw new WeeklyRetrospectiveError(`${field} is invalid`);
  }
  return {
    participant,
    participant_kind: response.participant_kind,
    choice_id: response.picked_none
      ? (response.choice_id === null ? null : invalid(`${field} choice is invalid`))
      : requiredText(response.choice_id, `${field} choice_id`),
    picked_none: response.picked_none,
    selection_kind: requiredSelectionKind(
      response.selection_kind,
      response.picked_none,
      publication,
    ),
    correct: response.correct,
  };
}

function scoreSourceWeek(source, revealManifest, publication) {
  const answerKey = new Map();
  for (const item of revealManifest.items || []) {
    if (!item || !Array.isArray(item.choices) || answerKey.has(item.id)) {
      throw new WeeklyRetrospectiveError('reveal answer key is invalid');
    }
    const choices = new Map();
    let hasAccepted = false;
    for (const choice of item.choices) {
      if (!choice || choices.has(choice.id) || typeof choice.accepted_correct !== 'boolean') {
        throw new WeeklyRetrospectiveError('reveal answer key choice is invalid');
      }
      choices.set(choice.id, choice.accepted_correct);
      if (choice.accepted_correct) hasAccepted = true;
    }
    answerKey.set(item.id, { choices, hasAccepted });
  }
  if (answerKey.size !== publication.itemCount) {
    throw new WeeklyRetrospectiveError('reveal answer key item_count is invalid');
  }
  const votesByLink = new Map(source.participants.map(row => [row.participantLink, []]));
  for (const vote of source.votes) votesByLink.get(vote.participantLink).push(vote);
  return source.participants.map(participant => {
    const seen = new Set();
    let correct = 0;
    for (const vote of votesByLink.get(participant.participantLink)) {
      if (seen.has(vote.itemId) || !answerKey.has(vote.itemId)) {
        throw new WeeklyRetrospectiveError('source participant votes are invalid');
      }
      seen.add(vote.itemId);
      const item = answerKey.get(vote.itemId);
      if (vote.pickedNone ? !item.hasAccepted : item.choices.get(vote.choiceId) === true) {
        correct += 1;
      } else if (!vote.pickedNone && !item.choices.has(vote.choiceId)) {
        throw new WeeklyRetrospectiveError('source vote choice is unknown');
      }
    }
    return {
      participant: participant.displayName,
      participantKind: participant.participantKind,
      participantLink: participant.participantLink,
      correct,
      answered: seen.size,
      total: publication.itemCount,
      complete: seen.size === publication.itemCount,
    };
  });
}

function buildAllTimeResponse(aggregates, { ranking, participantKind, admin }) {
  const rows = aggregates
    .filter(row => participantKind == null || row.participant_kind === participantKind)
    .map(row => ({
      participant: row.participant_kind === 'human'
        ? sanitizePseudonym(row.participant) : row.participant,
      participant_kind: row.participant_kind,
      weeks_participated: row.weeks_participated,
      complete_weeks: row.complete_weeks,
      total_correct: row.total_correct,
      total_questions: row.total_questions,
      weighted_average_accuracy: row.total_questions
        ? roundPercent(row.total_correct, row.total_questions) : null,
      provisional: row.complete_weeks < 3,
    }));
  rows.sort((left, right) => compareRankings(left, right, ranking));
  rows.forEach((row, index) => {
    row.rank = index + 1;
  });
  const response = {
    format_version: ARCHIVE_ALL_TIME_FORMAT_VERSION,
    scope: admin ? 'admin' : 'public',
    ranking,
    participant_kind: participantKind,
    participants: rows,
  };
  assertResponseSafe(response);
  return response;
}

function compareRankings(left, right, ranking) {
  if (ranking === 'weighted_average_accuracy') {
    const leftAccuracy = left.weighted_average_accuracy ?? -1;
    const rightAccuracy = right.weighted_average_accuracy ?? -1;
    return rightAccuracy - leftAccuracy
      || right.total_questions - left.total_questions
      || right.total_correct - left.total_correct
      || left.participant.localeCompare(right.participant);
  }
  return right.total_correct - left.total_correct
    || (right.weighted_average_accuracy ?? -1) - (left.weighted_average_accuracy ?? -1)
    || right.complete_weeks - left.complete_weeks
    || left.participant.localeCompare(right.participant);
}

function newAggregate(participant, participantKind) {
  return {
    participant,
    participant_kind: participantKind,
    weeks_participated: 0,
    complete_weeks: 0,
    total_correct: 0,
    total_questions: 0,
    latestNameAt: null,
  };
}

function addWeek(aggregate, row) {
  aggregate.weeks_participated += 1;
  if (row.complete) {
    aggregate.complete_weeks += 1;
    aggregate.total_correct += row.correct;
    aggregate.total_questions += row.total;
  }
}

function validateRankingOptions(ranking, participantKind, admin) {
  if (!RANKING_VIEWS.includes(ranking)) {
    throw new WeeklyRetrospectiveError('ranking view is invalid');
  }
  if (participantKind != null && !PARTICIPANT_KIND_SET.has(participantKind)) {
    throw new WeeklyRetrospectiveError('participant_kind filter is invalid');
  }
  if (!admin && participantKind === 'human') {
    throw new WeeklyRetrospectiveError('public human ranking is unavailable');
  }
}

function requireUniqueParticipants(rows, field, allowDuplicateHumans = false) {
  const identities = rows
    .filter(row => !allowDuplicateHumans || row.participant_kind !== 'human')
    .map(row => `${row.participant_kind}:${row.participant}`);
  if (new Set(identities).size !== identities.length) {
    throw new WeeklyRetrospectiveError(`${field} contain duplicate identities`);
  }
}

function verifyQuestionCoverage(questions, itemCount) {
  const ids = questions.map(row => row.item_id);
  if (questions.length !== itemCount || new Set(ids).size !== ids.length) {
    throw new WeeklyRetrospectiveError('artifact question coverage is invalid');
  }
}

function requiredSelectionKind(value, pickedNone, publication) {
  if (!pickedNone && publication.roundId === LEGACY_EXACT_SCOPE_ROUND_ID) {
    if (!new Set(['exact', 'cluster', 'unknown']).has(value)) {
      throw new WeeklyRetrospectiveError('selection_kind is invalid');
    }
    return 'exact';
  }
  const allowed = pickedNone ? new Set(['none']) : new Set(['exact', 'cluster']);
  if (!allowed.has(value)) {
    throw new WeeklyRetrospectiveError('selection_kind is invalid');
  }
  return value;
}

export function sanitizePseudonym(value) {
  return normalizePseudonym(value);
}

function normalizePseudonym(value) {
  const normalized = requiredText(value, 'pseudonym').trim().replace(/\s+/g, ' ');
  if (!normalized || normalized.length > 80 || Buffer.byteLength(normalized, 'utf8') > 320
    || /[\u0000-\u001f\u007f]/.test(normalized) || SHA256.test(normalized)) {
    throw new WeeklyRetrospectiveError('pseudonym is invalid');
  }
  return normalized;
}

function exactKeys(value, expected, field) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new WeeklyRetrospectiveError(`${field} must be an object`);
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) {
    throw new WeeklyRetrospectiveError(`${field} fields are invalid`);
  }
}

function requiredText(value, field) {
  if (typeof value !== 'string' || !value || value.length > 1000) {
    throw new WeeklyRetrospectiveError(`${field} must be non-empty text`);
  }
  return value;
}

function requiredRoundId(value, field) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(value)) {
    throw new WeeklyRetrospectiveError(`${field} is invalid`);
  }
  return value;
}

function requiredUuid(value, field) {
  if (typeof value !== 'string'
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new WeeklyRetrospectiveError(`${field} must be a UUID`);
  }
  return value.toLowerCase();
}

function requiredSha256(value, field) {
  if (typeof value !== 'string' || !SHA256.test(value)) {
    throw new WeeklyRetrospectiveError(`${field} must be a lowercase SHA-256`);
  }
  return value;
}

function requiredTimestamp(value, field) {
  if (typeof value !== 'string' || !value || !Number.isFinite(Date.parse(value))) {
    throw new WeeklyRetrospectiveError(`${field} must be an ISO timestamp`);
  }
  return value;
}

function sameTimestamp(value, expected, field) {
  if (Date.parse(requiredTimestamp(value, field)) !== Date.parse(expected)) {
    throw new WeeklyRetrospectiveError(`${field} is inconsistent`);
  }
}

function positiveInt(value, field) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new WeeklyRetrospectiveError(`${field} must be a positive integer`);
  }
  return value;
}

function nonnegativeInt(value, field) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new WeeklyRetrospectiveError(`${field} must be a nonnegative integer`);
  }
  return value;
}

function requiredBoolean(value, field) {
  if (typeof value !== 'boolean') {
    throw new WeeklyRetrospectiveError(`${field} must be boolean`);
  }
  return value;
}

function requiredNull(value, field) {
  if (value !== null) {
    throw new WeeklyRetrospectiveError(`${field} must be null when suppressed`);
  }
  return null;
}

function finiteNumber(value, field) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new WeeklyRetrospectiveError(`${field} must be finite`);
  }
  return value;
}

function finitePercent(value, field) {
  const number = finiteNumber(value, field);
  if (number < 0 || number > 100) {
    throw new WeeklyRetrospectiveError(`${field} must be a percentage`);
  }
  return number;
}

function requireArray(value, field) {
  if (!Array.isArray(value)) {
    throw new WeeklyRetrospectiveError(`${field} must be an array`);
  }
  return value;
}

function stableId(prefix, value, length) {
  return `${prefix}_${createHash('sha256').update(canonicalJson(value)).digest('hex').slice(0, length)}`;
}

function roundPercent(numerator, denominator) {
  return Math.round((numerator / denominator) * 1000) / 10;
}

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === 'object') {
    return Object.keys(value).sort().reduce((result, key) => {
      result[key] = sortKeys(value[key]);
      return result;
    }, {});
  }
  return value;
}

function invalid(message) {
  throw new WeeklyRetrospectiveError(message);
}
