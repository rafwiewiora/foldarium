import {
  buildRevealAnswerKey,
  isLeaderboardOptIn,
  scoreParticipantVotes,
  WeeklyResultsError,
} from './weekly-results.js';

export const WEEKLY_PLAY_FOR_FUN_FORMAT_VERSION =
  'foldarium.weekly-play-for-fun-leaderboard/v1';

const ARCHIVE_ROUND_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UUID_IN_TEXT =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/i;
const SHA256 = /^[0-9a-f]{64}$/;
const CHOICE_ID = /^choice[_-]/i;

export class WeeklyPlayForFunResultsError extends Error {
  constructor(message) {
    super(message);
    this.name = 'WeeklyPlayForFunResultsError';
  }
}

export function validateArchiveRoundId(value, field = 'round_id') {
  if (typeof value !== 'string' || !ARCHIVE_ROUND_ID.test(value)) {
    throw new WeeklyPlayForFunResultsError(`${field} is invalid`);
  }
  return value;
}

export function verifyRevealedPlayForFunRound(round, roundId) {
  validateArchiveRoundId(roundId, 'round_id');
  if (!round || typeof round !== 'object' || Array.isArray(round)) {
    throw new WeeklyPlayForFunResultsError('round is missing');
  }
  if (round.round_id !== roundId) {
    throw new WeeklyPlayForFunResultsError('round_id mismatch');
  }
  if (round.status !== 'revealed') {
    throw new WeeklyPlayForFunResultsError('round is not revealed');
  }
  if (round.revealed_at == null) {
    throw new WeeklyPlayForFunResultsError('round revealed_at is missing');
  }
  if (round.reveal_manifest == null || typeof round.reveal_manifest !== 'object') {
    throw new WeeklyPlayForFunResultsError('reveal manifest is missing');
  }
  if (round.reveal_manifest.round_id !== roundId) {
    throw new WeeklyPlayForFunResultsError('reveal manifest round_id is inconsistent');
  }
  const itemCount = positiveInt(round.item_count, 'item_count');
  const answerKey = buildRevealAnswerKey(round.reveal_manifest);
  if (answerKey.size !== itemCount) {
    throw new WeeklyPlayForFunResultsError('reveal item_count mismatch');
  }
  if (round.reveal_manifest_sha256 != null) {
    requiredSha256(round.reveal_manifest_sha256, 'reveal manifest sha256');
  }
  return {
    roundId,
    itemCount,
    revealManifest: round.reveal_manifest,
  };
}

export function selectLatestOptInSessions(sessions = [], roundId) {
  validateArchiveRoundId(roundId, 'round_id');
  const byUserId = new Map();
  for (const session of sessions) {
    if (!session || typeof session !== 'object' || Array.isArray(session)) {
      throw new WeeklyPlayForFunResultsError('session row is invalid');
    }
    if (session.round_id !== roundId) {
      throw new WeeklyPlayForFunResultsError('session round_id mismatch');
    }
    if (!isLeaderboardOptIn(session.initial_app_state)) continue;
    const userId = requiredUserId(session.user_id, 'session user_id');
    const sessionId = requiredUserId(session.session_id, 'session session_id');
    const displayName = requiredText(session.display_name, 'session display_name');
    const startedAt = requiredTimestamp(session.started_at, 'session started_at');
    const existing = byUserId.get(userId);
    if (existing) existing.sessionIds.add(sessionId);
    if (
      !existing
      || startedAt > existing.startedAt
      || (startedAt === existing.startedAt && sessionId > existing.sessionId)
    ) {
      byUserId.set(userId, {
        sessionId,
        sessionIds: existing?.sessionIds || new Set([sessionId]),
        displayName,
        startedAt,
      });
    }
  }
  return byUserId;
}

export function selectLatestParticipantAttempts(
  voteAttempts = [],
  participantSessionIds,
  roundId,
  participantUserId = null,
) {
  const sessionIds = new Set(
    typeof participantSessionIds === 'string'
      ? [requiredUserId(participantSessionIds, 'session_id')]
      : [...participantSessionIds].map(value => requiredUserId(value, 'session_id')),
  );
  if (!sessionIds.size) {
    throw new WeeklyPlayForFunResultsError('participant session IDs are missing');
  }
  const userId = participantUserId == null
    ? null
    : requiredUserId(participantUserId, 'participant user_id');
  validateArchiveRoundId(roundId, 'round_id');
  const byItemId = new Map();
  for (const attempt of voteAttempts) {
    if (!attempt || typeof attempt !== 'object' || Array.isArray(attempt)) {
      throw new WeeklyPlayForFunResultsError('vote attempt row is invalid');
    }
    if (!sessionIds.has(attempt.session_id)) continue;
    if (attempt.round_id !== roundId) {
      throw new WeeklyPlayForFunResultsError('vote attempt round_id mismatch');
    }
    if (userId != null && requiredUserId(attempt.user_id, 'vote attempt user_id') !== userId) {
      throw new WeeklyPlayForFunResultsError('vote attempt user_id mismatch');
    }
    const itemId = requiredText(attempt.item_id, 'vote attempt item_id');
    if (typeof attempt.picked_none !== 'boolean') {
      throw new WeeklyPlayForFunResultsError('vote attempt picked_none is invalid');
    }
    const submittedAt = requiredTimestamp(attempt.submitted_at, 'vote attempt submitted_at');
    const voteAttemptId = requiredUserId(attempt.vote_attempt_id, 'vote attempt vote_attempt_id');
    const existing = byItemId.get(itemId);
    if (
      !existing
      || submittedAt > existing.submittedAt
      || (submittedAt === existing.submittedAt && voteAttemptId > existing.voteAttemptId)
    ) {
      byItemId.set(itemId, {
        itemId,
        submittedAt,
        voteAttemptId,
        choiceId: attempt.choice_id,
        pickedNone: attempt.picked_none,
      });
    }
  }
  return [...byItemId.values()].map(row => ({
    item_id: row.itemId,
    choice_id: row.pickedNone === true ? null : row.choiceId,
    picked_none: row.pickedNone === true,
  }));
}

export function scorePlayForFunResults({
  roundId,
  itemCount,
  revealManifest,
  sessions = [],
  voteAttempts = [],
} = {}) {
  validateArchiveRoundId(roundId, 'round_id');
  const total = positiveInt(itemCount, 'item_count');
  const answerKey = buildRevealAnswerKey(revealManifest);
  if (answerKey.size !== total) {
    throw new WeeklyPlayForFunResultsError('reveal item_count mismatch');
  }

  const participants = selectLatestOptInSessions(sessions, roundId);
  const completeRuns = [];
  const partialRuns = [];
  for (const [userId, participant] of participants.entries()) {
    const votes = selectLatestParticipantAttempts(
      voteAttempts,
      participant.sessionIds,
      roundId,
      userId,
    );
    const { correct, answered } = scoreParticipantVotes(votes, answerKey, total);
    const row = buildForFunResultRow({
      displayName: participant.displayName,
      correct,
      answered,
      total,
    });
    if (answered === total) completeRuns.push(row);
    else if (answered > 0) partialRuns.push(row);
  }

  rankCompleteRuns(completeRuns);
  sortPartialRuns(partialRuns);

  const result = {
    format_version: WEEKLY_PLAY_FOR_FUN_FORMAT_VERSION,
    round_id: roundId,
    item_count: total,
    participant_count: completeRuns.length + partialRuns.length,
    complete_runs: completeRuns,
    partial_runs: partialRuns,
  };
  assertSanitizedPlayForFunResult(result);
  return result;
}

export function assertSanitizedPlayForFunResult(result) {
  const serialized = JSON.stringify(result);
  if (UUID_IN_TEXT.test(serialized)) {
    throw new WeeklyPlayForFunResultsError('result contains internal identifiers');
  }
  if (/\buser_id\b/i.test(serialized)) {
    throw new WeeklyPlayForFunResultsError('result contains user_id');
  }
  if (/\bsession_id\b/i.test(serialized)) {
    throw new WeeklyPlayForFunResultsError('result contains session_id');
  }
  if (/\bchoice_id\b/i.test(serialized)) {
    throw new WeeklyPlayForFunResultsError('result contains choice_id');
  }
  if (/\bparticipant_hash\b/i.test(serialized)) {
    throw new WeeklyPlayForFunResultsError('result contains participant_hash');
  }
  if (/\bdisplay_name_hash\b/i.test(serialized)) {
    throw new WeeklyPlayForFunResultsError('result contains display_name_hash');
  }
  if (/\bvote_attempt_id\b/i.test(serialized)) {
    throw new WeeklyPlayForFunResultsError('result contains vote_attempt_id');
  }
  for (const row of [...result.complete_runs, ...result.partial_runs]) {
    validateForFunResultRow(row, row.rank != null);
  }
}

function buildForFunResultRow({ displayName, correct, answered, total }) {
  return {
    display_name: displayName,
    correct,
    answered,
    total,
    accuracy: roundPercent(correct, answered),
    coverage: roundPercent(answered, total),
    participation_mode: 'for_fun',
  };
}

function rankCompleteRuns(rows) {
  rows.sort(compareCompleteRuns);
  rows.forEach((row, index) => {
    row.rank = index + 1;
  });
}

function sortPartialRuns(rows) {
  rows.sort(comparePartialRuns);
}

function compareCompleteRuns(left, right) {
  if (right.correct !== left.correct) return right.correct - left.correct;
  if (right.accuracy !== left.accuracy) return right.accuracy - left.accuracy;
  if (right.answered !== left.answered) return right.answered - left.answered;
  return left.display_name.localeCompare(right.display_name);
}

function comparePartialRuns(left, right) {
  if (right.correct !== left.correct) return right.correct - left.correct;
  if (right.accuracy !== left.accuracy) return right.accuracy - left.accuracy;
  if (right.answered !== left.answered) return right.answered - left.answered;
  if (right.coverage !== left.coverage) return right.coverage - left.coverage;
  return left.display_name.localeCompare(right.display_name);
}

function validateForFunResultRow(row, expectRank) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) {
    throw new WeeklyPlayForFunResultsError('result row is invalid');
  }
  const allowed = new Set([
    'display_name',
    'correct',
    'answered',
    'total',
    'accuracy',
    'coverage',
    'participation_mode',
    'rank',
  ]);
  for (const key of Object.keys(row)) {
    if (!allowed.has(key)) {
      throw new WeeklyPlayForFunResultsError(`result row has forbidden field ${key}`);
    }
  }
  requiredText(row.display_name, 'display_name');
  if (row.participation_mode !== 'for_fun') {
    throw new WeeklyPlayForFunResultsError('participation_mode is invalid');
  }
  if (!Number.isInteger(row.correct) || row.correct < 0) {
    throw new WeeklyPlayForFunResultsError('correct is invalid');
  }
  if (!Number.isInteger(row.answered) || row.answered <= 0) {
    throw new WeeklyPlayForFunResultsError('answered is invalid');
  }
  if (!Number.isInteger(row.total) || row.total <= 0) {
    throw new WeeklyPlayForFunResultsError('total is invalid');
  }
  if (row.answered > row.total) {
    throw new WeeklyPlayForFunResultsError('answered exceeds total');
  }
  if (row.correct > row.answered) {
    throw new WeeklyPlayForFunResultsError('correct exceeds answered');
  }
  validatePercent(row.accuracy, 'accuracy');
  validatePercent(row.coverage, 'coverage');
  if (expectRank) {
    if (!Number.isInteger(row.rank) || row.rank <= 0) {
      throw new WeeklyPlayForFunResultsError('rank is invalid');
    }
  } else if (row.rank != null) {
    throw new WeeklyPlayForFunResultsError('partial row must not include rank');
  }
  if (CHOICE_ID.test(row.display_name)) {
    throw new WeeklyPlayForFunResultsError('result row exposes choice identity');
  }
}

function validatePercent(value, field) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100) {
    throw new WeeklyPlayForFunResultsError(`${field} is invalid`);
  }
}

function roundPercent(numerator, denominator) {
  if (!denominator) return 0;
  return Math.round((numerator / denominator) * 1000) / 10;
}

function requiredText(value, field) {
  if (typeof value !== 'string' || !value) {
    throw new WeeklyPlayForFunResultsError(`${field} must be non-empty text`);
  }
  return value;
}

function requiredUserId(value, field) {
  if (typeof value !== 'string' || !UUID_PATTERN.test(value)) {
    throw new WeeklyPlayForFunResultsError(`${field} must be a UUID`);
  }
  return value.toLowerCase();
}

function requiredSha256(value, field) {
  if (typeof value !== 'string' || !SHA256.test(value)) {
    throw new WeeklyPlayForFunResultsError(`${field} must be a lowercase SHA-256`);
  }
  return value;
}

function requiredTimestamp(value, field) {
  const parsed = Date.parse(typeof value === 'string' ? value : '');
  if (!Number.isFinite(parsed)) {
    throw new WeeklyPlayForFunResultsError(`${field} must be an ISO timestamp`);
  }
  return parsed;
}

function positiveInt(value, field) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new WeeklyPlayForFunResultsError(`${field} must be a positive integer`);
  }
  return value;
}

export { WeeklyResultsError };
