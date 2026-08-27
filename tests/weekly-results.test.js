import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ALLOWED_ROUND_ID,
  buildWeeklyQuestionResults,
  enrichVotesWithSelectionKinds,
  LEGACY_V4_ROUND_ID,
  SMINA_DISPLAY_NAME,
  WEEKLY_QUESTION_RESULTS_FORMAT_VERSION,
  WEEKLY_RESULTS_FORMAT_VERSION,
  WeeklyResultsError,
  isLeaderboardOptIn,
  scoreSminaBaseline,
  scoreWeeklyResults,
  verifyRevealedLiveRoundState,
} from '../lib/weekly-results.js';
import { createPrivateEvaluationHandler } from '../api/private-evaluation.js';
import { createWeeklyResultsHandler } from '../api/weekly-results.js';
import { buildFixture } from './private-evaluation-fixtures.js';
import {
  USER_CLAUDE,
  USER_CODEX,
  USER_COMPLETE,
  buildBetaFixture,
  buildBlindManifest,
  buildCompleteFixture,
  buildOptInOverrideFixture,
  buildRevealManifest,
  buildScoringFixtures,
  buildSminaThirteenOfTwentyNineFixture,
  sminaScore,
  vote,
} from './weekly-results-fixtures.js';

function previewEnv(overrides = {}) {
  const fixture = buildFixture();
  return {
    FOLDARIUM_ENV: 'preview',
    SUPABASE_URL: 'https://preview-private.supabase.co',
    SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_preview_private',
    FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_DESCRIPTOR: fixture.descriptorRaw,
    FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_PASSWORD: 'preview-secret',
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_production',
    ...overrides,
  };
}

function invokePrivate(handler, body, { method = 'POST', env = previewEnv() } = {}) {
  const headers = {};
  let statusCode;
  let responseBody;
  const response = {
    setHeader(name, value) { headers[name] = value; return this; },
    status(value) { statusCode = value; return this; },
    json(value) { responseBody = value; return this; },
  };
  return handler({ method, body }, response).then(() => ({
    statusCode,
    headers,
    body: responseBody,
    serialized: JSON.stringify(responseBody),
  }));
}

function invokeWeekly(handler, { query = {}, env = previewEnv({ FOLDARIUM_ENV: 'production' }) } = {}) {
  const headers = {};
  let statusCode;
  let responseBody;
  const response = {
    setHeader(name, value) { headers[name] = value; return this; },
    status(value) { statusCode = value; return this; },
    json(value) { responseBody = value; return this; },
  };
  return handler({ method: 'GET', query }, response).then(() => ({
    statusCode,
    headers,
    body: responseBody,
    serialized: JSON.stringify(responseBody),
  }));
}

function recordingPrivateFetch(fixture, leaderboardFixture) {
  async function fetchImpl(url, options = {}) {
    fetchImpl.calls.push({ url, method: options.method || 'GET', headers: options.headers || {} });
    if (url.includes('/weekly_quiz_rounds')) {
      return { ok: true, json: async () => [fixture.liveRound] };
    }
    if (url.includes('/storage/v1/object/authenticated/')) {
      return { ok: true, arrayBuffer: async () => fixture.artifactBytes };
    }
    if (url.includes('/weekly_quiz_votes')) {
      return { ok: true, json: async () => leaderboardFixture.votes };
    }
    if (url.includes('/weekly_quiz_vote_attempts')) {
      return { ok: true, json: async () => leaderboardFixture.voteAttempts || [] };
    }
    if (url.includes('/weekly_quiz_sessions')) {
      const rows = url.includes(encodeURIComponent(LEGACY_V4_ROUND_ID))
        ? leaderboardFixture.legacySessions
        : leaderboardFixture.currentSessions;
      return { ok: true, json: async () => rows };
    }
    throw new Error(`unexpected fetch: ${url}`);
  }
  fetchImpl.calls = [];
  return fetchImpl;
}

function recordingWeeklyFetch(liveRound, leaderboardFixture, artifactFixture = null) {
  async function fetchImpl(url, options = {}) {
    fetchImpl.calls.push({ url, method: options.method || 'GET', headers: options.headers || {} });
    if (url.includes('/weekly_quiz_rounds')) {
      return { ok: true, json: async () => [liveRound] };
    }
    if (url.includes('/storage/v1/object/authenticated/')) {
      return { ok: true, arrayBuffer: async () => artifactFixture.artifactBytes };
    }
    if (url.includes('/weekly_quiz_votes')) {
      return { ok: true, json: async () => leaderboardFixture.votes };
    }
    if (url.includes('/weekly_quiz_sessions')) {
      const rows = url.includes(encodeURIComponent(LEGACY_V4_ROUND_ID))
        ? leaderboardFixture.legacySessions
        : leaderboardFixture.currentSessions;
      return { ok: true, json: async () => rows };
    }
    throw new Error(`unexpected fetch: ${url}`);
  }
  fetchImpl.calls = [];
  return fetchImpl;
}

test('isLeaderboardOptIn requires explicit opt-in and version marker', () => {
  assert.equal(isLeaderboardOptIn(null), false);
  assert.equal(isLeaderboardOptIn({ leaderboard_opt_in: true }), false);
  assert.equal(isLeaderboardOptIn({
    leaderboard_opt_in: true,
    leaderboard_name_version: 1,
  }), true);
});

test('scores selected choices against accepted_correct only', () => {
  const { revealManifest, blindManifest } = buildScoringFixtures({ itemCount: 2, noneItemIndex: 1 });
  const result = scoreWeeklyResults({
    itemCount: 2,
    blindManifest,
    revealManifest,
    votes: [
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { choiceId: 'choice-a' }),
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM02', { pickedNone: true }),
    ],
    currentSessions: [{
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_COMPLETE,
      display_name: 'Scorer',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    }],
  });
  assert.equal(result.complete_runs.length, 2);
  const scorer = result.complete_runs.find(row => row.display_name === 'Scorer');
  assert.equal(scorer.correct, 2);
});

test('picked_none is correct only when the item has no accepted_correct choice', () => {
  const { revealManifest, blindManifest } = buildScoringFixtures({ itemCount: 2, noneItemIndex: 1 });
  const wrongNone = scoreWeeklyResults({
    itemCount: 2,
    blindManifest,
    revealManifest,
    votes: [
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { pickedNone: true }),
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM02', { pickedNone: true }),
    ],
    currentSessions: [{
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_COMPLETE,
      display_name: 'WrongNone',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    }],
  });
  assert.equal(
    wrongNone.complete_runs.find(row => row.display_name === 'WrongNone').correct,
    1,
  );

  const single = buildScoringFixtures({ itemCount: 1, noneItemIndex: 0 });
  const rightNone = scoreWeeklyResults({
    itemCount: 1,
    blindManifest: single.blindManifest,
    revealManifest: single.revealManifest,
    votes: [vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { pickedNone: true })],
    currentSessions: [{
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_COMPLETE,
      display_name: 'RightNone',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    }],
  });
  assert.equal(
    rightNone.complete_runs.find(row => row.display_name === 'RightNone').correct,
    1,
  );
});

test('rejects unknown items, unknown choices, and duplicate votes', () => {
  const { revealManifest, blindManifest } = buildScoringFixtures({ itemCount: 1, noneItemIndex: 0 });
  const sessions = [{
    round_id: ALLOWED_ROUND_ID,
    user_id: USER_COMPLETE,
    display_name: 'Player',
    initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
  }];
  assert.throws(
    () => scoreWeeklyResults({
      itemCount: 1,
      blindManifest,
      revealManifest,
      votes: [vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'MISSING', { choiceId: 'choice-a' })],
      currentSessions: sessions,
    }),
    /unknown vote item/,
  );
  assert.throws(
    () => scoreWeeklyResults({
      itemCount: 1,
      blindManifest,
      revealManifest,
      votes: [vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { choiceId: 'choice-z' })],
      currentSessions: sessions,
    }),
    /unknown vote choice/,
  );
  assert.throws(
    () => scoreWeeklyResults({
      itemCount: 1,
      blindManifest,
      revealManifest,
      votes: [
        vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { choiceId: 'choice-a' }),
        vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { choiceId: 'choice-b' }),
      ],
      currentSessions: sessions,
    }),
    /duplicate vote item/,
  );
});

test('current opt-in sessions are eligible and non-opted-in voters are excluded', () => {
  const fixture = buildCompleteFixture();
  const result = scoreWeeklyResults({
    itemCount: fixture.itemCount,
    blindManifest: fixture.blindManifest,
    revealManifest: fixture.revealManifest,
    votes: fixture.votes,
    currentSessions: fixture.currentSessions,
  });
  assert.equal(result.participant_count, 2);
  assert.equal(result.complete_runs.find(row => row.display_name === 'CompletePlayer').correct, 2);
  assert.doesNotMatch(JSON.stringify(result), /HiddenPlayer/);
});

test('legacy v4 lineage maps Claude Opus and Codex GPT-5.6 with partial 21/29 coverage', () => {
  const fixture = buildBetaFixture({ itemCount: 29, answered: 21 });
  const result = scoreWeeklyResults({
    itemCount: fixture.itemCount,
    blindManifest: fixture.blindManifest,
    revealManifest: fixture.revealManifest,
    votes: fixture.votes,
    legacySessions: fixture.legacySessions,
  });
  assert.equal(result.participant_count, 3);
  assert.equal(result.complete_runs.length, 1);
  assert.equal(result.partial_runs.length, 2);
  const names = result.partial_runs.map(row => row.display_name).sort();
  assert.deepEqual(names, ['Claude Opus', 'Codex GPT-5.6']);
  for (const row of result.partial_runs) {
    assert.equal(row.answered, 21);
    assert.equal(row.total, 29);
    assert.equal(row.coverage, 72.4);
    assert.equal(row.rank, undefined);
  }
  const claude = result.partial_runs.find(row => row.display_name === 'Claude Opus');
  const codex = result.partial_runs.find(row => row.display_name === 'Codex GPT-5.6');
  assert.ok(claude.correct > codex.correct);
  assert.ok(claude.accuracy > codex.accuracy);
});

test('builds per-question popularity from eligible players without internal user IDs', () => {
  const fixture = buildBetaFixture({ itemCount: 29, answered: 21 });
  const result = buildWeeklyQuestionResults({
    itemCount: fixture.itemCount,
    blindManifest: fixture.blindManifest,
    revealManifest: fixture.revealManifest,
    votes: enrichVotesWithSelectionKinds(fixture.votes),
    legacySessions: fixture.legacySessions,
  });
  assert.equal(result.items.length, 29);
  assert.deepEqual(result.items[0], {
    item_id: 'ITEM01',
    answered_count: 3,
    correct_count: 2,
    correct_display_names: ['Claude Opus', SMINA_DISPLAY_NAME],
    answers: [{
      choice_id: 'choice-a',
      picked_none: false,
      selection_kind: 'exact',
      correct: true,
      vote_count: 2,
      display_names: ['Claude Opus', SMINA_DISPLAY_NAME],
    }, {
      choice_id: 'choice-b',
      picked_none: false,
      selection_kind: 'exact',
      correct: false,
      vote_count: 1,
      display_names: ['Codex GPT-5.6'],
    }],
  });
  assert.equal(result.items[28].answered_count, 1);
  assert.doesNotMatch(JSON.stringify(result), /11111111|22222222|user_id/);
});

test('vote-attempt provenance distinguishes cluster and exact-pose selections', () => {
  const votes = [
    {
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_CLAUDE,
      item_id: 'ITEM01',
      choice_id: 'choice-a',
      picked_none: false,
    },
    {
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_CODEX,
      item_id: 'ITEM01',
      choice_id: 'choice-a',
      picked_none: false,
    },
  ];
  const enriched = enrichVotesWithSelectionKinds(votes, [{
    ...votes[0],
    app_state: { selection_kind: 'cluster' },
    submitted_at: '2026-08-08T12:00:00Z',
  }, {
    ...votes[1],
    app_state: { selection_kind: 'exact' },
    submitted_at: '2026-08-08T12:00:00Z',
  }]);
  assert.deepEqual(enriched.map(vote => vote.selection_kind), ['cluster', 'exact']);
});

test('known unclustered legacy ballots default missing provenance to exact pose', () => {
  const votes = [{
    round_id: ALLOWED_ROUND_ID,
    user_id: USER_CLAUDE,
    item_id: 'ITEM01',
    choice_id: 'choice-a',
    picked_none: false,
  }];
  assert.equal(enrichVotesWithSelectionKinds(votes)[0].selection_kind, 'exact');
});

test('prefers current opted-in identity over legacy allow-listed name', () => {
  const fixture = buildOptInOverrideFixture();
  const result = scoreWeeklyResults({
    itemCount: fixture.itemCount,
    blindManifest: fixture.blindManifest,
    revealManifest: fixture.revealManifest,
    votes: fixture.votes,
    currentSessions: fixture.currentSessions,
    legacySessions: fixture.legacySessions,
  });
  assert.equal(result.partial_runs[0].display_name, 'Current Opt-In Name');
});

test('complete runs are ranked and partial runs stay unranked with deterministic ordering', () => {
  const { revealManifest, blindManifest } = buildScoringFixtures({ itemCount: 3, noneItemIndex: 2 });
  const sessions = [
    {
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_CLAUDE,
      display_name: 'Alpha',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    },
    {
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_CODEX,
      display_name: 'Beta',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    },
    {
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_COMPLETE,
      display_name: 'Gamma',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    },
  ];
  const result = scoreWeeklyResults({
    itemCount: 3,
    blindManifest,
    revealManifest,
    votes: [
      vote(ALLOWED_ROUND_ID, USER_CLAUDE, 'ITEM01', { choiceId: 'choice-a' }),
      vote(ALLOWED_ROUND_ID, USER_CLAUDE, 'ITEM02', { choiceId: 'choice-a' }),
      vote(ALLOWED_ROUND_ID, USER_CLAUDE, 'ITEM03', { pickedNone: true }),
      vote(ALLOWED_ROUND_ID, USER_CODEX, 'ITEM01', { choiceId: 'choice-a' }),
      vote(ALLOWED_ROUND_ID, USER_CODEX, 'ITEM02', { choiceId: 'choice-b' }),
      vote(ALLOWED_ROUND_ID, USER_CODEX, 'ITEM03', { pickedNone: true }),
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { choiceId: 'choice-a' }),
    ],
    currentSessions: sessions,
  });
  assert.equal(result.complete_runs.length, 3);
  assert.equal(result.partial_runs.length, 1);
  assert.deepEqual(
    result.complete_runs.map(row => row.display_name),
    ['Alpha', 'Beta', SMINA_DISPLAY_NAME],
  );
  assert.equal(result.complete_runs[0].rank, 1);
  assert.equal(result.complete_runs[1].rank, 2);
  assert.equal(result.complete_runs[2].rank, 3);
  assert.equal(result.partial_runs[0].display_name, 'Gamma');
  assert.equal(result.partial_runs[0].rank, undefined);
});

test('sanitized result JSON excludes internal ids, hashes, and choice ids', () => {
  const fixture = buildBetaFixture({ itemCount: 5, answered: 3 });
  const result = scoreWeeklyResults({
    itemCount: fixture.itemCount,
    blindManifest: fixture.blindManifest,
    revealManifest: fixture.revealManifest,
    votes: fixture.votes,
    legacySessions: fixture.legacySessions,
  });
  const serialized = JSON.stringify(result);
  assert.equal(result.format_version, WEEKLY_RESULTS_FORMAT_VERSION);
  assert.doesNotMatch(serialized, /user_id|choice_id|participant_hash|vote_id/);
  assert.doesNotMatch(serialized, /11111111-1111-4111-8111-111111111111/);
  assert.doesNotMatch(serialized, /choice-a|choice-b/);
  assert.doesNotMatch(serialized, /openfold3|boltz2|smina_score/);
});

test('scoreWeeklyResults requires blind manifest', () => {
  const { revealManifest } = buildScoringFixtures({ itemCount: 1, noneItemIndex: 0 });
  assert.throws(
    () => scoreWeeklyResults({ itemCount: 1, revealManifest }),
    /blind manifest is required/,
  );
});

test('Smina baseline scores 13/29 on complete coverage using lowest-affinity picks', () => {
  const { revealManifest, blindManifest, itemCount } = buildSminaThirteenOfTwentyNineFixture();
  const result = scoreWeeklyResults({
    itemCount,
    blindManifest,
    revealManifest,
    votes: [],
    currentSessions: [],
    legacySessions: [],
  });
  assert.equal(result.participant_count, 1);
  const smina = result.complete_runs.find(row => row.display_name === SMINA_DISPLAY_NAME);
  assert.ok(smina);
  assert.equal(smina.correct, 13);
  assert.equal(smina.answered, 29);
  assert.equal(smina.total, 29);
  assert.equal(smina.coverage, 100);
  assert.equal(smina.accuracy, 44.8);
  assert.equal(smina.rank, 1);
});

test('Smina picks the lowest finite smina affinity and tie-breaks by choice id', () => {
  const revealAcceptedA = buildRevealManifest({
    itemCount: 1,
    noneItemIndex: -1,
    acceptedByItem: { ITEM01: 'choice-a' },
  });
  const tiedBlind = buildBlindManifest({
    itemCount: 1,
    noneItemIndex: -1,
    sminaByItem: {
      ITEM01: { 'choice-a': -5.0, 'choice-b': -5.0 },
    },
  });
  const acceptedPick = scoreSminaBaseline(tiedBlind, revealAcceptedA, 1);
  assert.equal(acceptedPick.correct, 1);

  const revealAcceptedB = buildRevealManifest({
    itemCount: 1,
    noneItemIndex: -1,
    acceptedByItem: { ITEM01: 'choice-b' },
  });
  const rejectedPick = scoreSminaBaseline(tiedBlind, revealAcceptedB, 1);
  assert.equal(rejectedPick.correct, 0);

  const lowestBlind = buildBlindManifest({
    itemCount: 1,
    noneItemIndex: -1,
    sminaByItem: {
      ITEM01: { 'choice-a': -4.0, 'choice-b': -6.5 },
    },
  });
  const lowestPick = scoreSminaBaseline(lowestBlind, revealAcceptedB, 1);
  assert.equal(lowestPick.correct, 1);
});

test('Smina rejects malformed or missing smina_score data', () => {
  const { revealManifest } = buildScoringFixtures({ itemCount: 1, noneItemIndex: 0 });
  const missing = buildBlindManifest({ itemCount: 1, noneItemIndex: 0 });
  delete missing.items[0].choices[0].smina_score;
  assert.throws(
    () => scoreSminaBaseline(missing, revealManifest, 1),
    /smina_score is invalid/,
  );

  const malformed = buildBlindManifest({ itemCount: 1, noneItemIndex: 0 });
  malformed.items[0].choices[0].smina_score = sminaScore(Number.NaN);
  assert.throws(
    () => scoreSminaBaseline(malformed, revealManifest, 1),
    /value is invalid/,
  );

  const wrongSchema = buildBlindManifest({ itemCount: 1, noneItemIndex: 0 });
  wrongSchema.items[0].choices[0].smina_score = {
    ...sminaScore(-7.0),
    scoring_function: 'vinardo',
  };
  assert.throws(
    () => scoreSminaBaseline(wrongSchema, revealManifest, 1),
    /schema mismatch/,
  );

  const extraField = buildBlindManifest({ itemCount: 1, noneItemIndex: 0 });
  extraField.items[0].choices[0].smina_score.untrusted = true;
  assert.throws(
    () => scoreSminaBaseline(extraField, revealManifest, 1),
    /schema mismatch/,
  );
});

test('Smina rejects blind reveal item and choice mismatches', () => {
  const { revealManifest, blindManifest } = buildScoringFixtures({ itemCount: 2, noneItemIndex: 1 });
  const extraItem = structuredClone(blindManifest);
  extraItem.items.push({
    id: 'ITEM99',
    choices: [{
      id: 'choice-a',
      method: 'openfold3',
      cluster_id: 'cluster-a',
      is_rep: true,
      smina_score: sminaScore(-7.0),
    }],
  });
  assert.throws(
    () => scoreSminaBaseline(extraItem, revealManifest, 2),
    /blind item_count mismatch/,
  );

  const missingChoice = structuredClone(blindManifest);
  missingChoice.items[0].choices.pop();
  assert.throws(
    () => scoreSminaBaseline(missingChoice, revealManifest, 2),
    /blind reveal choice mismatch/,
  );

  const unknownItem = structuredClone(blindManifest);
  unknownItem.items[0].id = 'ITEM99';
  assert.throws(
    () => scoreSminaBaseline(unknownItem, revealManifest, 2),
    /blind reveal item mismatch/,
  );
});

test('Smina participates in complete ranking without hardcoded score', () => {
  const { revealManifest, blindManifest } = buildScoringFixtures({ itemCount: 2, noneItemIndex: 1 });
  const result = scoreWeeklyResults({
    itemCount: 2,
    blindManifest,
    revealManifest,
    votes: [
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM01', { choiceId: 'choice-a' }),
      vote(ALLOWED_ROUND_ID, USER_COMPLETE, 'ITEM02', { pickedNone: true }),
    ],
    currentSessions: [{
      round_id: ALLOWED_ROUND_ID,
      user_id: USER_COMPLETE,
      display_name: 'Human',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    }],
  });
  assert.equal(result.complete_runs.length, 2);
  const human = result.complete_runs.find(row => row.display_name === 'Human');
  const smina = result.complete_runs.find(row => row.display_name === SMINA_DISPLAY_NAME);
  assert.equal(human.correct, 2);
  assert.equal(smina.correct, 1);
  assert.equal(human.rank, 1);
  assert.equal(smina.rank, 2);
  assert.equal(result.complete_runs[0].display_name, 'Human');
  assert.equal(result.complete_runs[1].display_name, SMINA_DISPLAY_NAME);
});

test('private evaluation attaches leaderboard and question results using GET-only upstream reads', async () => {
  const fixture = buildFixture();
  const leaderboardFixture = {
    votes: [],
    currentSessions: [],
    legacySessions: [],
  };
  const fetchImpl = recordingPrivateFetch(fixture, leaderboardFixture);
  const response = await invokePrivate(
    createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl }),
    { password: 'preview-secret' },
  );
  assert.equal(response.statusCode, 200);
  assert.ok(response.body.weekly_leaderboard);
  assert.equal(response.body.weekly_leaderboard.format_version, WEEKLY_RESULTS_FORMAT_VERSION);
  assert.equal(response.body.weekly_leaderboard.participant_count, 1);
  assert.equal(response.body.weekly_leaderboard.complete_runs[0].display_name, SMINA_DISPLAY_NAME);
  assert.equal(
    response.body.weekly_question_results.format_version,
    WEEKLY_QUESTION_RESULTS_FORMAT_VERSION,
  );
  assert.equal(response.body.weekly_question_results.items[0].answered_count, 1);
  assert.match(fetchImpl.calls.map(call => call.url).join('\n'), /weekly_quiz_votes/);
  assert.match(fetchImpl.calls.map(call => call.url).join('\n'), /weekly_quiz_sessions/);
  for (const call of fetchImpl.calls.filter(call => /weekly_quiz_(votes|sessions)/.test(call.url))) {
    assert.equal(call.headers.Range, '0-999');
  }
  for (const call of fetchImpl.calls) {
    assert.notEqual(call.method, 'POST');
    assert.notEqual(call.method, 'PATCH');
    assert.notEqual(call.method, 'PUT');
    assert.notEqual(call.method, 'DELETE');
  }
  assert.doesNotMatch(response.serialized, /user_id|sb_secret/);
});

test('public weekly-results rejects non-revealed rounds and unknown round ids', async () => {
  const openRound = buildFixture().liveRound;
  const fetchImpl = recordingWeeklyFetch(openRound, buildBetaFixture());
  const env = previewEnv({
    FOLDARIUM_ENV: 'production',
    FOLDARIUM_PRODUCTION_WEEKLY_RESULTS_DESCRIPTOR: buildFixture().descriptorRaw,
  });
  const notFound = await invokeWeekly(createWeeklyResultsHandler({
    env,
    fetchImpl,
  }), { query: { round_id: 'weekly-other' } });
  assert.equal(notFound.statusCode, 404);

  const blocked = await invokeWeekly(createWeeklyResultsHandler({
    env,
    fetchImpl,
  }), { query: { round_id: ALLOWED_ROUND_ID } });
  assert.equal(blocked.statusCode, 502);
  assert.equal(blocked.body.error, 'Weekly results unavailable');
});

test('public weekly-results returns sanitized leaderboard for verified revealed rounds', async () => {
  const artifactFixture = buildFixture();
  const liveRound = {
    ...artifactFixture.liveRound,
    status: 'revealed',
    reveal_manifest: artifactFixture.bundle.reveal_manifest,
    reveal_manifest_sha256: artifactFixture.catalog.reveal_manifest_sha256,
    revealed_at: '2026-08-18T00:00:00Z',
  };
  const leaderboardFixture = { votes: [], currentSessions: [], legacySessions: [] };
  const fetchImpl = recordingWeeklyFetch(liveRound, leaderboardFixture, artifactFixture);
  const response = await invokeWeekly(createWeeklyResultsHandler({
    env: previewEnv({
      FOLDARIUM_ENV: 'production',
      FOLDARIUM_PRODUCTION_WEEKLY_RESULTS_DESCRIPTOR: artifactFixture.descriptorRaw,
    }),
    fetchImpl,
  }), { query: { round_id: ALLOWED_ROUND_ID } });
  assert.equal(response.statusCode, 200);
  assert.equal(response.headers['Cache-Control'], 'no-store');
  assert.equal(response.body.participant_count, 1);
  assert.equal(response.body.complete_runs[0].display_name, SMINA_DISPLAY_NAME);
  assert.throws(
    () => verifyRevealedLiveRoundState({ ...liveRound, status: 'open' }),
    WeeklyResultsError,
  );
  for (const call of fetchImpl.calls) {
    assert.notEqual(call.method, 'POST');
  }
  assert.doesNotMatch(response.serialized, /user_id|choice_id|sb_secret/);
});

test('verifyRevealedLiveRoundState binds reveal to the blind manifest without reserializing floats', () => {
  const fixture = buildFixture();
  const revealManifest = structuredClone(fixture.bundle.reveal_manifest);
  revealManifest.items[0].choices[0].rmsd = 1.0;
  const liveRound = {
    ...fixture.liveRound,
    status: 'revealed',
    reveal_manifest: revealManifest,
    reveal_manifest_sha256: 'ff'.repeat(32),
    revealed_at: '2026-08-18T00:00:00Z',
  };
  assert.doesNotThrow(() => verifyRevealedLiveRoundState(liveRound));
  liveRound.reveal_manifest.blind_manifest_sha256 = 'cc'.repeat(32);
  assert.throws(
    () => verifyRevealedLiveRoundState(liveRound),
    /reveal manifest blind digest is inconsistent/,
  );
});
