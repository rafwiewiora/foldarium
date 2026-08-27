import {
  ALLOWED_ROUND_ID,
  parseSupabaseObjectUri,
} from './private-evaluation-contract.js';

export { ALLOWED_ROUND_ID };

export const WEEKLY_RESULTS_FORMAT_VERSION = 'foldarium.weekly-leaderboard/v1';
export const WEEKLY_QUESTION_RESULTS_FORMAT_VERSION = 'foldarium.weekly-question-results/v2';
export const LEGACY_V4_ROUND_ID = 'weekly-2026-08-08-beta-v4';
export const LEADERBOARD_NAME_VERSION = 1;

export const LEGACY_ALLOW_LISTED_DISPLAY_NAMES = Object.freeze([
  'Claude Opus',
  'Codex GPT-5.6',
]);

export const SMINA_DISPLAY_NAME = 'Smina';

export const EXPECTED_SMINA_SCORE_SCHEMA = Object.freeze({
  metric: 'smina_affinity',
  protocol: 'score_only',
  scoring_function: 'vina',
  units: 'kcal/mol',
});

const LEGACY_NAME_SET = new Set(LEGACY_ALLOW_LISTED_DISPLAY_NAMES);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UUID_IN_TEXT = /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/i;
const SHA256 = /^[0-9a-f]{64}$/;
const CHOICE_ID = /^choice[_-]/i;

export class WeeklyResultsError extends Error {
  constructor(message) {
    super(message);
    this.name = 'WeeklyResultsError';
  }
}

export function isLeaderboardOptIn(initialAppState) {
  if (!initialAppState || typeof initialAppState !== 'object' || Array.isArray(initialAppState)) {
    return false;
  }
  return initialAppState.leaderboard_opt_in === true
    && initialAppState.leaderboard_name_version === LEADERBOARD_NAME_VERSION;
}

export function buildRevealAnswerKey(revealManifest) {
  if (!revealManifest || typeof revealManifest !== 'object' || Array.isArray(revealManifest)) {
    throw new WeeklyResultsError('reveal manifest is invalid');
  }
  const rawItems = revealManifest.items;
  if (!Array.isArray(rawItems) || !rawItems.length) {
    throw new WeeklyResultsError('reveal manifest has no items');
  }
  const items = new Map();
  for (const rawItem of rawItems) {
    if (!rawItem || typeof rawItem !== 'object' || Array.isArray(rawItem)) {
      throw new WeeklyResultsError('reveal item is invalid');
    }
    const itemId = requiredText(rawItem.id, 'reveal item id');
    if (items.has(itemId)) throw new WeeklyResultsError('reveal item IDs are duplicated');
    const choices = new Map();
    let hasAcceptedCorrect = false;
    const rawChoices = rawItem.choices;
    if (!Array.isArray(rawChoices) || !rawChoices.length) {
      throw new WeeklyResultsError('reveal item has no choices');
    }
    for (const rawChoice of rawChoices) {
      if (!rawChoice || typeof rawChoice !== 'object' || Array.isArray(rawChoice)) {
        throw new WeeklyResultsError('reveal choice is invalid');
      }
      const choiceId = requiredText(rawChoice.id, 'reveal choice id');
      if (choices.has(choiceId)) throw new WeeklyResultsError('reveal choice IDs are duplicated');
      const acceptedCorrect = rawChoice.accepted_correct === true;
      choices.set(choiceId, acceptedCorrect);
      if (acceptedCorrect) hasAcceptedCorrect = true;
    }
    items.set(itemId, { choices, hasAcceptedCorrect });
  }
  return items;
}

export function resolveEligibleParticipants({
  currentSessions = [],
  legacySessions = [],
} = {}) {
  const byUserId = new Map();
  for (const session of legacySessions) {
    if (session?.round_id !== LEGACY_V4_ROUND_ID) {
      throw new WeeklyResultsError('legacy session round_id mismatch');
    }
    const userId = requiredUserId(session?.user_id, 'legacy session user_id');
    const displayName = requiredText(session?.display_name, 'legacy session display_name');
    if (!LEGACY_NAME_SET.has(displayName)) continue;
    setParticipantIdentity(byUserId, userId, displayName, 'legacy');
  }
  for (const session of currentSessions) {
    if (session?.round_id !== ALLOWED_ROUND_ID) {
      throw new WeeklyResultsError('session round_id mismatch');
    }
    const userId = requiredUserId(session?.user_id, 'session user_id');
    if (!isLeaderboardOptIn(session?.initial_app_state)) continue;
    const displayName = requiredText(session?.display_name, 'session display_name');
    setParticipantIdentity(byUserId, userId, displayName, 'current');
  }
  return byUserId;
}

export function scoreParticipantVotes(votes, answerKey, itemCount) {
  if (!Array.isArray(votes)) throw new WeeklyResultsError('votes must be an array');
  const seenItems = new Set();
  let correct = 0;
  for (const vote of votes) {
    if (!vote || typeof vote !== 'object' || Array.isArray(vote)) {
      throw new WeeklyResultsError('vote row is invalid');
    }
    if (typeof vote.picked_none !== 'boolean') {
      throw new WeeklyResultsError('vote picked_none is invalid');
    }
    const itemId = requiredText(vote.item_id, 'vote item_id');
    if (seenItems.has(itemId)) throw new WeeklyResultsError('duplicate vote item');
    seenItems.add(itemId);
    const itemKey = answerKey.get(itemId);
    if (!itemKey) throw new WeeklyResultsError('unknown vote item');
    if (vote.picked_none) {
      if (vote.choice_id != null && vote.choice_id !== '') {
        throw new WeeklyResultsError('picked_none vote has choice_id');
      }
      if (!itemKey.hasAcceptedCorrect) correct += 1;
      continue;
    }
    const choiceId = requiredText(vote.choice_id, 'vote choice_id');
    if (!itemKey.choices.has(choiceId)) throw new WeeklyResultsError('unknown vote choice');
    if (itemKey.choices.get(choiceId) === true) correct += 1;
  }
  const answered = seenItems.size;
  if (answered > itemCount) throw new WeeklyResultsError('vote count exceeds item_count');
  return { correct, answered };
}

export function parseSminaScore(rawScore, field) {
  if (!rawScore || typeof rawScore !== 'object' || Array.isArray(rawScore)) {
    throw new WeeklyResultsError(`${field} is invalid`);
  }
  const allowedKeys = new Set([...Object.keys(EXPECTED_SMINA_SCORE_SCHEMA), 'value']);
  if (Object.keys(rawScore).some(key => !allowedKeys.has(key))) {
    throw new WeeklyResultsError(`${field} schema mismatch`);
  }
  for (const [key, expected] of Object.entries(EXPECTED_SMINA_SCORE_SCHEMA)) {
    if (rawScore[key] !== expected) {
      throw new WeeklyResultsError(`${field} schema mismatch`);
    }
  }
  const value = rawScore.value;
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new WeeklyResultsError(`${field} value is invalid`);
  }
  return value;
}

export function buildBlindRevealJoin(blindManifest, revealManifest, itemCount) {
  if (!blindManifest || typeof blindManifest !== 'object' || Array.isArray(blindManifest)) {
    throw new WeeklyResultsError('blind manifest is invalid');
  }
  const revealKey = buildRevealAnswerKey(revealManifest);
  const rawCorrectByItem = new Map(revealManifest.items.map(item => [
    item.id,
    new Map(item.choices.map(choice => [choice.id, choice.correct === true])),
  ]));
  if (revealKey.size !== itemCount) {
    throw new WeeklyResultsError('reveal item_count mismatch');
  }
  const rawBlindItems = blindManifest.items;
  if (!Array.isArray(rawBlindItems) || !rawBlindItems.length) {
    throw new WeeklyResultsError('blind manifest has no items');
  }
  const blindItems = new Map();
  for (const rawItem of rawBlindItems) {
    if (!rawItem || typeof rawItem !== 'object' || Array.isArray(rawItem)) {
      throw new WeeklyResultsError('blind item is invalid');
    }
    const itemId = requiredText(rawItem.id, 'blind item id');
    if (blindItems.has(itemId)) throw new WeeklyResultsError('blind item IDs are duplicated');
    const rawChoices = rawItem.choices;
    if (!Array.isArray(rawChoices) || !rawChoices.length) {
      throw new WeeklyResultsError('blind item has no choices');
    }
    const choices = new Map();
    for (const rawChoice of rawChoices) {
      if (!rawChoice || typeof rawChoice !== 'object' || Array.isArray(rawChoice)) {
        throw new WeeklyResultsError('blind choice is invalid');
      }
      const choiceId = requiredText(rawChoice.id, 'blind choice id');
      if (choices.has(choiceId)) throw new WeeklyResultsError('blind choice IDs are duplicated');
      const sminaValue = parseSminaScore(
        rawChoice.smina_score,
        `blind choice ${itemId}/${choiceId} smina_score`,
      );
      const clusterId = requiredText(
        rawChoice.cluster_id,
        `blind choice ${itemId}/${choiceId} cluster_id`,
      );
      if (typeof rawChoice.is_rep !== 'boolean') {
        throw new WeeklyResultsError(`blind choice ${itemId}/${choiceId} is_rep is invalid`);
      }
      choices.set(choiceId, {
        sminaValue,
        clusterId,
        isRepresentative: rawChoice.is_rep,
      });
    }
    blindItems.set(itemId, { choices });
  }
  if (blindItems.size !== itemCount) {
    throw new WeeklyResultsError('blind item_count mismatch');
  }
  const joined = new Map();
  for (const [itemId, revealItem] of revealKey.entries()) {
    const blindItem = blindItems.get(itemId);
    if (!blindItem) throw new WeeklyResultsError('blind reveal item mismatch');
    if (blindItem.choices.size !== revealItem.choices.size) {
      throw new WeeklyResultsError('blind reveal choice mismatch');
    }
    const choices = new Map();
    for (const [choiceId, acceptedCorrect] of revealItem.choices.entries()) {
      const blindChoice = blindItem.choices.get(choiceId);
      if (!blindChoice) throw new WeeklyResultsError('blind reveal choice mismatch');
      choices.set(choiceId, {
        sminaValue: blindChoice.sminaValue,
        clusterId: blindChoice.clusterId,
        isRepresentative: blindChoice.isRepresentative,
        acceptedCorrect,
        rawCorrect: rawCorrectByItem.get(itemId)?.get(choiceId) === true,
      });
    }
    for (const choiceId of blindItem.choices.keys()) {
      if (!revealItem.choices.has(choiceId)) {
        throw new WeeklyResultsError('blind reveal choice mismatch');
      }
    }
    joined.set(itemId, { choices });
  }
  for (const itemId of blindItems.keys()) {
    if (!revealKey.has(itemId)) throw new WeeklyResultsError('blind reveal item mismatch');
  }
  return joined;
}

export function scoreSminaBaseline(blindManifest, revealManifest, itemCount) {
  const joined = buildBlindRevealJoin(blindManifest, revealManifest, itemCount);
  let correct = 0;
  for (const item of joined.values()) {
    let bestChoiceId = null;
    let bestValue = Infinity;
    for (const [choiceId, data] of item.choices.entries()) {
      if (
        data.sminaValue < bestValue
        || (data.sminaValue === bestValue && choiceId < bestChoiceId)
      ) {
        bestValue = data.sminaValue;
        bestChoiceId = choiceId;
      }
    }
    if (item.choices.get(bestChoiceId).rawCorrect === true) correct += 1;
  }
  return { correct, answered: itemCount };
}

export function scoreWeeklyResults({
  roundId = ALLOWED_ROUND_ID,
  itemCount,
  blindManifest,
  revealManifest,
  votes = [],
  currentSessions = [],
  legacySessions = [],
} = {}) {
  if (roundId !== ALLOWED_ROUND_ID) throw new WeeklyResultsError('round_id is not allow-listed');
  if (blindManifest == null) throw new WeeklyResultsError('blind manifest is required');
  const total = positiveInt(itemCount, 'item_count');
  const answerKey = buildRevealAnswerKey(revealManifest);
  if (answerKey.size !== total) {
    throw new WeeklyResultsError('reveal item_count mismatch');
  }
  const sminaScore = scoreSminaBaseline(blindManifest, revealManifest, total);
  const participants = resolveEligibleParticipants({ currentSessions, legacySessions });
  const votesByUser = groupVotesByUser(votes, roundId);
  const completeRuns = [];
  const partialRuns = [];
  for (const [userId, participant] of participants.entries()) {
    const userVotes = votesByUser.get(userId) || [];
    const { correct, answered } = scoreParticipantVotes(userVotes, answerKey, total);
    const row = buildResultRow({
      displayName: participant.display_name,
      correct,
      answered,
      total,
    });
    if (answered === total) completeRuns.push(row);
    else if (answered > 0) partialRuns.push(row);
  }
  completeRuns.push(buildResultRow({
    displayName: SMINA_DISPLAY_NAME,
    correct: sminaScore.correct,
    answered: sminaScore.answered,
    total,
  }));
  rankCompleteRuns(completeRuns);
  sortPartialRuns(partialRuns);
  const result = {
    format_version: WEEKLY_RESULTS_FORMAT_VERSION,
    round_id: roundId,
    item_count: total,
    participant_count: completeRuns.length + partialRuns.length,
    complete_runs: completeRuns,
    partial_runs: partialRuns,
  };
  assertSanitizedResult(result);
  return result;
}

export function buildWeeklyQuestionResults({
  roundId = ALLOWED_ROUND_ID,
  itemCount,
  blindManifest,
  revealManifest,
  votes = [],
  currentSessions = [],
  legacySessions = [],
} = {}) {
  if (roundId !== ALLOWED_ROUND_ID) throw new WeeklyResultsError('round_id is not allow-listed');
  const total = positiveInt(itemCount, 'item_count');
  const answerKey = buildRevealAnswerKey(revealManifest);
  if (answerKey.size !== total) {
    throw new WeeklyResultsError('reveal item_count mismatch');
  }
  const participants = resolveEligibleParticipants({ currentSessions, legacySessions });
  const votesByUser = groupVotesByUser(votes, roundId);
  const rows = new Map();
  revealManifest.items.forEach(rawItem => {
    const itemId = requiredText(rawItem.id, 'reveal item id');
    const choiceOrder = new Map(rawItem.choices.map((choice, index) => [
      requiredText(choice.id, 'reveal choice id'),
      index,
    ]));
    rows.set(itemId, {
      item_id: itemId,
      answered_count: 0,
      correct_count: 0,
      correct_display_names: [],
      answers: new Map(),
      choiceOrder,
    });
  });

  for (const [userId, participant] of participants.entries()) {
    const participantVotes = votesByUser.get(userId) || [];
    scoreParticipantVotes(participantVotes, answerKey, total);
    for (const vote of participantVotes) {
      const row = rows.get(vote.item_id);
      const itemKey = answerKey.get(vote.item_id);
      const pickedNone = vote.picked_none === true;
      const choiceId = pickedNone ? null : vote.choice_id;
      if (!pickedNone
        && vote.selection_kind !== 'exact'
        && vote.selection_kind !== 'cluster') {
        throw new WeeklyResultsError('non-empty vote selection_kind is missing');
      }
      const selectionKind = pickedNone ? 'none' : vote.selection_kind;
      const correct = pickedNone
        ? !itemKey.hasAcceptedCorrect
        : itemKey.choices.get(choiceId) === true;
      const answerKeyValue = pickedNone ? 'none' : `${selectionKind}:${choiceId}`;
      const answer = row.answers.get(answerKeyValue) || {
        choice_id: choiceId,
        picked_none: pickedNone,
        selection_kind: selectionKind,
        correct,
        vote_count: 0,
        display_names: [],
      };
      answer.vote_count += 1;
      answer.display_names.push(participant.display_name);
      row.answers.set(answerKeyValue, answer);
      row.answered_count += 1;
      if (correct) {
        row.correct_count += 1;
        row.correct_display_names.push(participant.display_name);
      }
    }
  }
  const sminaItems = buildBlindRevealJoin(blindManifest, revealManifest, total);
  for (const [itemId, item] of sminaItems.entries()) {
    let bestChoiceId = null;
    let bestValue = Infinity;
    for (const [choiceId, data] of item.choices.entries()) {
      if (
        data.sminaValue < bestValue
        || (data.sminaValue === bestValue && choiceId < bestChoiceId)
      ) {
        bestValue = data.sminaValue;
        bestChoiceId = choiceId;
      }
    }
    const bestChoice = item.choices.get(bestChoiceId);
    const row = rows.get(itemId);
    const answerKeyValue = `exact:${bestChoiceId}`;
    const answer = row.answers.get(answerKeyValue) || {
      choice_id: bestChoiceId,
      picked_none: false,
      selection_kind: 'exact',
      correct: bestChoice.rawCorrect === true,
      vote_count: 0,
      display_names: [],
    };
    answer.vote_count += 1;
    answer.display_names.push(SMINA_DISPLAY_NAME);
    row.answers.set(answerKeyValue, answer);
    row.answered_count += 1;
    if (answer.correct) {
      row.correct_count += 1;
      row.correct_display_names.push(SMINA_DISPLAY_NAME);
    }
  }

  const items = [...rows.values()].map(row => ({
    item_id: row.item_id,
    answered_count: row.answered_count,
    correct_count: row.correct_count,
    correct_display_names: row.correct_display_names.sort((left, right) => left.localeCompare(right)),
    answers: [...row.answers.values()]
      .map(answer => ({
        ...answer,
        display_names: answer.display_names.sort((left, right) => left.localeCompare(right)),
      }))
      .sort((left, right) => (
        right.vote_count - left.vote_count
        || (left.picked_none ? Number.MAX_SAFE_INTEGER : row.choiceOrder.get(left.choice_id))
          - (right.picked_none ? Number.MAX_SAFE_INTEGER : row.choiceOrder.get(right.choice_id))
      )),
  }));
  return {
    format_version: WEEKLY_QUESTION_RESULTS_FORMAT_VERSION,
    round_id: roundId,
    item_count: total,
    items,
  };
}

export function enrichVotesWithSelectionKinds(
  votes = [],
  voteAttempts = [],
) {
  const attemptsByVote = new Map();
  for (const attempt of voteAttempts) {
    const selectionKind = attempt?.app_state?.selection_kind;
    if (selectionKind !== 'exact' && selectionKind !== 'cluster') continue;
    const key = [
      attempt.round_id,
      attempt.user_id,
      attempt.item_id,
      attempt.picked_none === true ? 'none' : attempt.choice_id,
    ].join('|');
    const submittedAt = Date.parse(attempt.submitted_at || '') || 0;
    const previous = attemptsByVote.get(key);
    if (!previous || submittedAt >= previous.submittedAt) {
      attemptsByVote.set(key, { selectionKind, submittedAt });
    }
  }
  return votes.map(vote => {
    if (vote?.picked_none === true) return { ...vote, selection_kind: 'none' };
    const key = [vote?.round_id, vote?.user_id, vote?.item_id, vote?.choice_id].join('|');
    return {
      ...vote,
      selection_kind: attemptsByVote.get(key)?.selectionKind
        || 'exact',
    };
  });
}

export function verifyRevealedLiveRoundState(round) {
  if (!round || typeof round !== 'object') throw new WeeklyResultsError('live round is missing');
  if (round.round_id !== ALLOWED_ROUND_ID) throw new WeeklyResultsError('round_id is not allow-listed');
  if (round.environment !== 'production') throw new WeeklyResultsError('round environment is invalid');
  if (round.status !== 'revealed') throw new WeeklyResultsError('round is not revealed');
  if (round.reveal_manifest == null || typeof round.reveal_manifest !== 'object') {
    throw new WeeklyResultsError('reveal manifest is missing');
  }
  if (round.revealed_at == null) throw new WeeklyResultsError('round revealed_at is missing');
  const revealDigest = requiredSha256(round.reveal_manifest_sha256, 'reveal manifest sha256');
  if (round.reveal_manifest.round_id !== ALLOWED_ROUND_ID) {
    throw new WeeklyResultsError('reveal manifest round_id is inconsistent');
  }
  const blindDigest = requiredSha256(round.blind_manifest_sha256, 'blind manifest sha256');
  if (round.reveal_manifest.blind_manifest_sha256 !== blindDigest) {
    throw new WeeklyResultsError('reveal manifest blind digest is inconsistent');
  }
  const privateIndex = round.metadata?.private_index;
  const privateIndexSha256 = requiredSha256(privateIndex?.sha256, 'private index sha256');
  parseSupabaseObjectUri(
    requiredText(privateIndex?.object_uri, 'private index object_uri'),
    privateIndexSha256,
  );
  return {
    roundId: round.round_id,
    campaignId: requiredText(round.campaign_id, 'campaign_id'),
    opensAt: requiredText(round.opens_at, 'opens_at'),
    closesAt: requiredText(round.closes_at, 'closes_at'),
    blindManifestSha256: blindDigest,
    privateIndexSha256,
    itemCount: positiveInt(round.item_count, 'item_count'),
    revealManifest: round.reveal_manifest,
    revealManifestSha256: revealDigest,
  };
}

export function assertSanitizedResult(result) {
  const serialized = JSON.stringify(result);
  if (UUID_IN_TEXT.test(serialized)) throw new WeeklyResultsError('result contains internal identifiers');
  if (/\buser_id\b/i.test(serialized)) throw new WeeklyResultsError('result contains user_id');
  if (/\bchoice_id\b/i.test(serialized)) throw new WeeklyResultsError('result contains choice_id');
  if (/\bparticipant_hash\b/i.test(serialized)) throw new WeeklyResultsError('result contains participant_hash');
  if (/\bvote_id\b/i.test(serialized)) throw new WeeklyResultsError('result contains vote_id');
  for (const row of [...result.complete_runs, ...result.partial_runs]) {
    validateResultRow(row, row.rank != null);
  }
}

function groupVotesByUser(votes, roundId) {
  const byUser = new Map();
  for (const vote of votes) {
    if (!vote || typeof vote !== 'object' || Array.isArray(vote)) {
      throw new WeeklyResultsError('vote row is invalid');
    }
    if (vote.round_id !== roundId) throw new WeeklyResultsError('vote round_id mismatch');
    const userId = requiredUserId(vote.user_id, 'vote user_id');
    if (!byUser.has(userId)) byUser.set(userId, []);
    byUser.get(userId).push(vote);
  }
  return byUser;
}

function buildResultRow({ displayName, correct, answered, total }) {
  return {
    display_name: displayName,
    correct,
    answered,
    total,
    accuracy: roundPercent(correct, answered),
    coverage: roundPercent(answered, total),
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
  if (right.accuracy !== left.accuracy) {
    return right.accuracy - left.accuracy;
  }
  return left.display_name.localeCompare(right.display_name);
}

function comparePartialRuns(left, right) {
  if (right.accuracy !== left.accuracy) {
    return right.accuracy - left.accuracy;
  }
  if (right.correct !== left.correct) return right.correct - left.correct;
  if (right.coverage !== left.coverage) {
    return right.coverage - left.coverage;
  }
  return left.display_name.localeCompare(right.display_name);
}

function validateResultRow(row, expectRank) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) {
    throw new WeeklyResultsError('result row is invalid');
  }
  const allowed = new Set([
    'display_name', 'correct', 'answered', 'total', 'accuracy', 'coverage', 'rank',
  ]);
  for (const key of Object.keys(row)) {
    if (!allowed.has(key)) throw new WeeklyResultsError(`result row has forbidden field ${key}`);
  }
  requiredText(row.display_name, 'display_name');
  if (!Number.isInteger(row.correct) || row.correct < 0) throw new WeeklyResultsError('correct is invalid');
  if (!Number.isInteger(row.answered) || row.answered <= 0) throw new WeeklyResultsError('answered is invalid');
  if (!Number.isInteger(row.total) || row.total <= 0) throw new WeeklyResultsError('total is invalid');
  if (row.answered > row.total) throw new WeeklyResultsError('answered exceeds total');
  if (row.correct > row.answered) throw new WeeklyResultsError('correct exceeds answered');
  validatePercent(row.accuracy, 'accuracy');
  validatePercent(row.coverage, 'coverage');
  if (expectRank) {
    if (!Number.isInteger(row.rank) || row.rank <= 0) throw new WeeklyResultsError('rank is invalid');
  } else if (row.rank != null) {
    throw new WeeklyResultsError('partial row must not include rank');
  }
  if (CHOICE_ID.test(row.display_name)) {
    throw new WeeklyResultsError('result row exposes choice identity');
  }
}

function validatePercent(value, field) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100) {
    throw new WeeklyResultsError(`${field} is invalid`);
  }
}

function setParticipantIdentity(byUserId, userId, displayName, source) {
  const existing = byUserId.get(userId);
  if (existing?.source === 'current' && source === 'legacy') return;
  if (existing && existing.display_name !== displayName && existing.source === source) {
    throw new WeeklyResultsError('participant display name is ambiguous');
  }
  byUserId.set(userId, { display_name: displayName, source });
}

function roundPercent(numerator, denominator) {
  if (!denominator) return 0;
  return Math.round((numerator / denominator) * 1000) / 10;
}

function requiredText(value, field) {
  if (typeof value !== 'string' || !value) throw new WeeklyResultsError(`${field} must be non-empty text`);
  return value;
}

function requiredUserId(value, field) {
  if (typeof value !== 'string' || !UUID_PATTERN.test(value)) {
    throw new WeeklyResultsError(`${field} must be a UUID`);
  }
  return value.toLowerCase();
}

function requiredSha256(value, field) {
  if (typeof value !== 'string' || !SHA256.test(value)) {
    throw new WeeklyResultsError(`${field} must be a lowercase SHA-256`);
  }
  return value;
}

function positiveInt(value, field) {
  if (!Number.isInteger(value) || value <= 0) throw new WeeklyResultsError(`${field} must be a positive integer`);
  return value;
}
