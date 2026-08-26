import {
  ALLOWED_ROUND_ID,
  LEGACY_V4_ROUND_ID,
} from '../lib/weekly-results.js';

export const USER_CLAUDE = '11111111-1111-4111-8111-111111111111';
export const USER_CODEX = '22222222-2222-4222-8222-222222222222';
export const USER_COMPLETE = '33333333-3333-4333-8333-333333333333';
const USER_HIDDEN = '44444444-4444-4444-8444-444444444444';

export function sminaScore(value) {
  return {
    metric: 'smina_affinity',
    protocol: 'score_only',
    scoring_function: 'vina',
    units: 'kcal/mol',
    value,
  };
}

export function vote(roundId, userId, itemId, {
  choiceId = null,
  pickedNone = false,
  selectionKind,
} = {}) {
  const row = {
    round_id: roundId,
    user_id: userId,
    item_id: itemId,
    choice_id: pickedNone ? null : choiceId,
    picked_none: pickedNone,
  };
  if (selectionKind != null) row.selection_kind = selectionKind;
  return row;
}

export function buildBlindManifest({
  itemCount,
  noneItemIndex = -1,
  sminaByItem = {},
} = {}) {
  return {
    schema_version: 1,
    round_id: ALLOWED_ROUND_ID,
    items: Array.from({ length: itemCount }, (_, index) => {
      const itemId = itemIdAt(index);
      const configured = sminaByItem[itemId] || {};
      return {
        id: itemId,
        choices: [
          {
            id: 'choice-a',
            method: 'openfold3',
            cluster_id: 'cluster-a',
            is_rep: true,
            smina_score: sminaScore(configured['choice-a'] ?? -7),
          },
          {
            id: 'choice-b',
            method: 'boltz2',
            cluster_id: 'cluster-b',
            is_rep: true,
            smina_score: sminaScore(configured['choice-b'] ?? -6),
          },
        ],
        expected_none: index === noneItemIndex,
      };
    }),
  };
}

export function buildRevealManifest({
  itemCount,
  noneItemIndex = -1,
  acceptedByItem = {},
} = {}) {
  return {
    schema_version: 1,
    round_id: ALLOWED_ROUND_ID,
    blind_manifest_sha256: 'a'.repeat(64),
    items: Array.from({ length: itemCount }, (_, index) => {
      const itemId = itemIdAt(index);
      const accepted = index === noneItemIndex
        ? null
        : (acceptedByItem[itemId] || 'choice-a');
      return {
        id: itemId,
        choices: ['choice-a', 'choice-b'].map(choiceId => ({
          id: choiceId,
          accepted_correct: choiceId === accepted,
          correct: choiceId === accepted,
        })),
      };
    }),
  };
}

export function buildScoringFixtures({
  itemCount,
  noneItemIndex = -1,
  acceptedByItem = {},
  sminaByItem = {},
} = {}) {
  return {
    itemCount,
    blindManifest: buildBlindManifest({
      itemCount,
      noneItemIndex,
      sminaByItem,
    }),
    revealManifest: buildRevealManifest({
      itemCount,
      noneItemIndex,
      acceptedByItem,
    }),
  };
}

export function buildCompleteFixture() {
  const fixture = buildScoringFixtures({ itemCount: 2, noneItemIndex: 1 });
  return {
    ...fixture,
    votes: [
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { choiceId: 'choice-a' }),
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM02', { pickedNone: true }),
      vote(ALLOWED_ROUND_ID, USER_HIDDEN, 'ITEM01', { choiceId: 'choice-a' }),
      vote(ALLOWED_ROUND_ID, USER_HIDDEN, 'ITEM02', { pickedNone: true }),
    ],
    currentSessions: [
      currentSession(USER_COMPLETE, 'CompletePlayer', true),
      currentSession(USER_HIDDEN, 'HiddenPlayer', false),
    ],
    legacySessions: [],
  };
}

export function buildBetaFixture({ itemCount = 29, answered = 21 } = {}) {
  const fixture = buildScoringFixtures({
    itemCount,
    noneItemIndex: itemCount - 1,
  });
  const votes = [];
  for (let index = 0; index < answered; index += 1) {
    const itemId = itemIdAt(index);
    votes.push(vote(ALLOWED_ROUND_ID, USER_CLAUDE, itemId, {
      choiceId: 'choice-a',
    }));
    votes.push(vote(ALLOWED_ROUND_ID, USER_CODEX, itemId, {
      choiceId: 'choice-b',
    }));
  }
  return {
    ...fixture,
    votes,
    currentSessions: [],
    legacySessions: [
      legacySession(USER_CLAUDE, 'Claude Opus'),
      legacySession(USER_CODEX, 'Codex GPT-5.6'),
    ],
    voteAttempts: [],
  };
}

export function buildOptInOverrideFixture() {
  const fixture = buildScoringFixtures({ itemCount: 2, noneItemIndex: 1 });
  return {
    ...fixture,
    votes: [
      vote(ALLOWED_ROUND_ID, USER_CLAUDE, 'ITEM01', { choiceId: 'choice-a' }),
    ],
    currentSessions: [
      currentSession(USER_CLAUDE, 'Current Opt-In Name', true),
    ],
    legacySessions: [
      legacySession(USER_CLAUDE, 'Claude Opus'),
    ],
  };
}

export function buildSminaThirteenOfTwentyNineFixture() {
  const itemCount = 29;
  const sminaByItem = {};
  for (let index = 0; index < itemCount; index += 1) {
    sminaByItem[itemIdAt(index)] = index < 13
      ? { 'choice-a': -7, 'choice-b': -6 }
      : { 'choice-a': -5, 'choice-b': -8 };
  }
  return buildScoringFixtures({ itemCount, sminaByItem });
}

function itemIdAt(index) {
  return `ITEM${String(index + 1).padStart(2, '0')}`;
}

function currentSession(userId, displayName, optedIn) {
  return {
    round_id: ALLOWED_ROUND_ID,
    user_id: userId,
    display_name: displayName,
    initial_app_state: optedIn
      ? { leaderboard_opt_in: true, leaderboard_name_version: 1 }
      : {},
  };
}

function legacySession(userId, displayName) {
  return {
    round_id: LEGACY_V4_ROUND_ID,
    user_id: userId,
    display_name: displayName,
  };
}
