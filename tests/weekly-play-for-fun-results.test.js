import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createWeeklyPlayForFunResultsHandler,
  weeklyPlayForFunResultsConfig,
} from '../api/weekly-play-for-fun-results.js';
import {
  scorePlayForFunResults,
  selectLatestOptInSessions,
  selectLatestParticipantAttempts,
  validateArchiveRoundId,
  verifyRevealedPlayForFunRound,
  WEEKLY_PLAY_FOR_FUN_FORMAT_VERSION,
  WeeklyPlayForFunResultsError,
} from '../lib/weekly-play-for-fun-results.js';
import {
  buildRevealManifest,
  buildScoringFixtures,
  USER_COMPLETE,
} from './weekly-results-fixtures.js';

const ROUND_ID = 'weekly-archive-play-for-fun-v1';
const USER_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const USER_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const SESSION_A1 = '11111111-1111-4111-8111-111111111111';
const SESSION_A2 = '22222222-2222-4222-8222-222222222222';
const SESSION_B1 = '33333333-3333-4333-8333-333333333333';
const ATTEMPT_A1_OLD = '44444444-4444-4444-8444-444444444444';
const ATTEMPT_A1_NEW = '55555555-5555-4555-8555-555555555555';
const ATTEMPT_A2 = '66666666-6666-4666-8666-666666666666';

function optInState(overrides = {}) {
  return {
    leaderboard_opt_in: true,
    leaderboard_name_version: 1,
    ...overrides,
  };
}

function revealManifestForRound(itemCount = 2) {
  const manifest = buildRevealManifest({ itemCount, noneItemIndex: 1 });
  return {
    ...manifest,
    round_id: ROUND_ID,
  };
}

function revealedRound({ itemCount = 2, status = 'revealed' } = {}) {
  return {
    round_id: ROUND_ID,
    status,
    revealed_at: '2026-08-20T00:00:00Z',
    item_count: itemCount,
    reveal_manifest: revealManifestForRound(itemCount),
    reveal_manifest_sha256: 'a'.repeat(64),
  };
}

function postRevealSession({
  sessionId,
  userId,
  displayName,
  startedAt,
  initialAppState = optInState(),
}) {
  return {
    session_id: sessionId,
    round_id: ROUND_ID,
    user_id: userId,
    display_name: displayName,
    initial_app_state: initialAppState,
    started_at: startedAt,
  };
}

function postRevealAttempt({
  voteAttemptId,
  sessionId,
  userId,
  itemId,
  choiceId = 'choice-a',
  pickedNone = false,
  submittedAt,
}) {
  return {
    vote_attempt_id: voteAttemptId,
    session_id: sessionId,
    round_id: ROUND_ID,
    user_id: userId,
    item_id: itemId,
    choice_id: pickedNone ? null : choiceId,
    picked_none: pickedNone,
    submitted_at: submittedAt,
  };
}

function invoke(handler, { method = 'GET', query = {}, env = productionEnv() } = {}) {
  const headers = {};
  let statusCode;
  let responseBody;
  const response = {
    setHeader(name, value) { headers[name] = value; return this; },
    status(value) { statusCode = value; return this; },
    json(value) { responseBody = value; return this; },
  };
  return handler({ method, query }, response).then(() => ({
    statusCode,
    headers,
    body: responseBody,
    serialized: JSON.stringify(responseBody),
  }));
}

function productionEnv(overrides = {}) {
  return {
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_production',
    ...overrides,
  };
}

function recordingFetch({
  round = revealedRound(),
  sessions = [],
  voteAttempts = [],
} = {}) {
  async function fetchImpl(url, options = {}) {
    fetchImpl.calls.push({ url, method: options.method || 'GET', headers: options.headers || {} });
    if (url.includes('/weekly_quiz_rounds')) {
      return { ok: true, json: async () => [round] };
    }
    if (url.includes('/weekly_quiz_post_reveal_sessions')) {
      return { ok: true, json: async () => sessions };
    }
    if (url.includes('/weekly_quiz_post_reveal_vote_attempts')) {
      return { ok: true, json: async () => voteAttempts };
    }
    throw new Error(`unexpected fetch: ${url}`);
  }
  fetchImpl.calls = [];
  return fetchImpl;
}

test('validateArchiveRoundId matches archive round id rules', () => {
  assert.equal(validateArchiveRoundId('weekly-2026-08-08-beta-v5-global-tm-29'), 'weekly-2026-08-08-beta-v5-global-tm-29');
  assert.throws(() => validateArchiveRoundId(''), /invalid/);
  assert.throws(() => validateArchiveRoundId('-bad'), /invalid/);
  assert.throws(() => validateArchiveRoundId('a'.repeat(201)), /invalid/);
});

test('verifyRevealedPlayForFunRound rejects unrevealed and malformed rounds', () => {
  assert.throws(
    () => verifyRevealedPlayForFunRound(revealedRound({ status: 'open' }), ROUND_ID),
    WeeklyPlayForFunResultsError,
  );
  assert.throws(
    () => verifyRevealedPlayForFunRound({
      ...revealedRound(),
      item_count: 99,
    }, ROUND_ID),
    /item_count mismatch/,
  );
});

test('selectLatestOptInSessions keeps latest opted-in session per user', () => {
  const selected = selectLatestOptInSessions([
    postRevealSession({
      sessionId: SESSION_A1,
      userId: USER_A,
      displayName: 'Old Player A',
      startedAt: '2026-08-20T10:00:00Z',
    }),
    postRevealSession({
      sessionId: SESSION_A2,
      userId: USER_A,
      displayName: 'Player A',
      startedAt: '2026-08-21T10:00:00Z',
    }),
    postRevealSession({
      sessionId: SESSION_B1,
      userId: USER_B,
      displayName: 'Player B',
      startedAt: '2026-08-20T11:00:00Z',
      initialAppState: { leaderboard_opt_in: false },
    }),
  ], ROUND_ID);
  assert.equal(selected.size, 1);
  assert.equal(selected.get(USER_A).sessionId, SESSION_A2);
  assert.equal(selected.get(USER_A).displayName, 'Player A');
});

test('selectLatestOptInSessions preserves legacy opt-in sessions without play_mode', () => {
  const selected = selectLatestOptInSessions([
    postRevealSession({
      sessionId: SESSION_A1,
      userId: USER_A,
      displayName: 'Legacy Opt-In',
      startedAt: '2026-08-20T10:00:00Z',
      initialAppState: optInState(),
    }),
  ], ROUND_ID);
  assert.equal(selected.size, 1);
  assert.equal(selected.get(USER_A).displayName, 'Legacy Opt-In');
});

test('selectLatestParticipantAttempts keeps latest item answer with deterministic tie-break', () => {
  const attempts = selectLatestParticipantAttempts([
    postRevealAttempt({
      voteAttemptId: ATTEMPT_A1_OLD,
      sessionId: SESSION_A1,
      userId: USER_A,
      itemId: 'ITEM01',
      choiceId: 'choice-b',
      submittedAt: '2026-08-20T10:00:00Z',
    }),
    postRevealAttempt({
      voteAttemptId: ATTEMPT_A1_NEW,
      sessionId: SESSION_A1,
      userId: USER_A,
      itemId: 'ITEM01',
      choiceId: 'choice-a',
      submittedAt: '2026-08-20T10:00:00Z',
    }),
    postRevealAttempt({
      voteAttemptId: ATTEMPT_A2,
      sessionId: SESSION_A1,
      userId: USER_A,
      itemId: 'ITEM02',
      pickedNone: true,
      submittedAt: '2026-08-20T11:00:00Z',
    }),
    postRevealAttempt({
      voteAttemptId: '77777777-7777-4777-8777-777777777777',
      sessionId: SESSION_A2,
      userId: USER_A,
      itemId: 'ITEM01',
      choiceId: 'choice-b',
      submittedAt: '2026-08-20T12:00:00Z',
    }),
  ], SESSION_A1, ROUND_ID);
  assert.deepEqual(attempts, [
    { item_id: 'ITEM01', choice_id: 'choice-a', picked_none: false },
    { item_id: 'ITEM02', choice_id: null, picked_none: true },
  ]);
});

test('scorePlayForFunResults scores accepted_correct and ranks complete runs within for_fun only', () => {
  const { revealManifest } = buildScoringFixtures({ itemCount: 2, noneItemIndex: 1 });
  const manifest = { ...revealManifest, round_id: ROUND_ID };
  const result = scorePlayForFunResults({
    roundId: ROUND_ID,
    itemCount: 2,
    revealManifest: manifest,
    sessions: [
      postRevealSession({
        sessionId: SESSION_A1,
        userId: USER_A,
        displayName: 'Alpha',
        startedAt: '2026-08-20T10:00:00Z',
      }),
      postRevealSession({
        sessionId: SESSION_B1,
        userId: USER_B,
        displayName: 'Bravo',
        startedAt: '2026-08-20T10:00:00Z',
      }),
    ],
    voteAttempts: [
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A1_NEW,
        sessionId: SESSION_A1,
        userId: USER_A,
        itemId: 'ITEM01',
        choiceId: 'choice-a',
        submittedAt: '2026-08-20T10:00:00Z',
      }),
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A2,
        sessionId: SESSION_A1,
        userId: USER_A,
        itemId: 'ITEM02',
        pickedNone: true,
        submittedAt: '2026-08-20T10:01:00Z',
      }),
      postRevealAttempt({
        voteAttemptId: '88888888-8888-4888-8888-888888888888',
        sessionId: SESSION_B1,
        userId: USER_B,
        itemId: 'ITEM01',
        choiceId: 'choice-a',
        submittedAt: '2026-08-20T10:00:00Z',
      }),
    ],
  });

  assert.equal(result.format_version, WEEKLY_PLAY_FOR_FUN_FORMAT_VERSION);
  assert.equal(result.round_id, ROUND_ID);
  assert.equal(result.item_count, 2);
  assert.equal(result.participant_count, 2);
  assert.equal(result.complete_runs.length, 1);
  assert.equal(result.partial_runs.length, 1);
  assert.equal(result.complete_runs[0].display_name, 'Alpha');
  assert.equal(result.complete_runs[0].correct, 2);
  assert.equal(result.complete_runs[0].rank, 1);
  assert.equal(result.complete_runs[0].participation_mode, 'for_fun');
  assert.equal(result.partial_runs[0].display_name, 'Bravo');
  assert.equal(result.partial_runs[0].rank, undefined);
  assert.equal(result.partial_runs[0].participation_mode, 'for_fun');
  assert.doesNotMatch(JSON.stringify(result), /user_id|session_id|participant_hash|vote_attempt_id/);
});

test('scorePlayForFunResults ignores non-opted-in sessions and zero-answer sessions', () => {
  const manifest = revealManifestForRound(1);
  const result = scorePlayForFunResults({
    roundId: ROUND_ID,
    itemCount: 1,
    revealManifest: manifest,
    sessions: [
      postRevealSession({
        sessionId: SESSION_A1,
        userId: USER_A,
        displayName: 'Hidden',
        startedAt: '2026-08-20T10:00:00Z',
        initialAppState: {},
      }),
      postRevealSession({
        sessionId: SESSION_B1,
        userId: USER_B,
        displayName: 'NoVotes',
        startedAt: '2026-08-20T10:00:00Z',
      }),
    ],
    voteAttempts: [],
  });
  assert.equal(result.participant_count, 0);
  assert.deepEqual(result.complete_runs, []);
  assert.deepEqual(result.partial_runs, []);
});

test('weeklyPlayForFunResultsConfig rejects malformed service role keys', () => {
  assert.equal(weeklyPlayForFunResultsConfig({
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: 'not-a-jwt',
  }).serviceRoleKey, '');
  assert.equal(weeklyPlayForFunResultsConfig({
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'http://insecure.example',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_production',
  }).url, '');
});

test('handler rejects non-GET, invalid round ids, and missing config', async () => {
  const handler = createWeeklyPlayForFunResultsHandler({
    env: productionEnv(),
    fetchImpl: recordingFetch(),
  });
  const methodBlocked = await invoke(handler, { method: 'POST', query: { round_id: ROUND_ID } });
  assert.equal(methodBlocked.statusCode, 405);
  assert.equal(methodBlocked.headers.Allow, 'GET');

  const invalidRound = await invoke(handler, { query: { round_id: ' bad id' } });
  assert.equal(invalidRound.statusCode, 400);

  const missingRound = await invoke(handler, { query: {} });
  assert.equal(missingRound.statusCode, 400);

  const unconfigured = await invoke(createWeeklyPlayForFunResultsHandler({
    env: productionEnv({
      FOLDARIUM_PRODUCTION_SUPABASE_URL: '',
      FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: '',
    }),
    fetchImpl: recordingFetch(),
  }), { query: { round_id: ROUND_ID } });
  assert.equal(unconfigured.statusCode, 503);
});

test('handler returns sanitized play-for-fun leaderboard with paginated upstream reads', async () => {
  const fetchImpl = recordingFetch({
    sessions: [
      postRevealSession({
        sessionId: SESSION_A1,
        userId: USER_A,
        displayName: 'Alpha',
        startedAt: '2026-08-20T10:00:00Z',
      }),
    ],
    voteAttempts: [
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A1_NEW,
        sessionId: SESSION_A1,
        userId: USER_A,
        itemId: 'ITEM01',
        choiceId: 'choice-a',
        submittedAt: '2026-08-20T10:00:00Z',
      }),
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A2,
        sessionId: SESSION_A1,
        userId: USER_A,
        itemId: 'ITEM02',
        pickedNone: true,
        submittedAt: '2026-08-20T10:01:00Z',
      }),
    ],
  });
  const response = await invoke(createWeeklyPlayForFunResultsHandler({
    env: productionEnv(),
    fetchImpl,
  }), { query: { round_id: ROUND_ID } });

  assert.equal(response.statusCode, 200);
  assert.equal(response.headers['Cache-Control'], 'no-store');
  assert.equal(response.body.format_version, WEEKLY_PLAY_FOR_FUN_FORMAT_VERSION);
  assert.equal(response.body.round_id, ROUND_ID);
  assert.equal(response.body.complete_runs[0].display_name, 'Alpha');
  assert.equal(response.body.complete_runs[0].participation_mode, 'for_fun');
  assert.doesNotMatch(response.serialized, /user_id|session_id|sb_secret|participant_hash/);
  assert.match(fetchImpl.calls.map(call => call.url).join('\n'), /weekly_quiz_post_reveal_sessions/);
  assert.match(fetchImpl.calls.map(call => call.url).join('\n'), /weekly_quiz_post_reveal_vote_attempts/);
  for (const call of fetchImpl.calls.filter(call => /weekly_quiz_post_reveal_/.test(call.url))) {
    assert.equal(call.headers.Range, '0-999');
    assert.notEqual(call.method, 'POST');
  }
});

test('handler fails closed on unrevealed rounds and upstream errors without leaking secrets', async () => {
  const unrevealed = await invoke(createWeeklyPlayForFunResultsHandler({
    env: productionEnv(),
    fetchImpl: recordingFetch({ round: revealedRound({ status: 'open' }) }),
  }), { query: { round_id: ROUND_ID } });
  assert.equal(unrevealed.statusCode, 502);
  assert.equal(unrevealed.body.error, 'Play-for-fun results unavailable');
  assert.doesNotMatch(unrevealed.serialized, /sb_secret|open/);

  async function failingFetch() {
    return { ok: false, json: async () => ({ message: 'secret upstream detail' }) };
  }
  const upstream = await invoke(createWeeklyPlayForFunResultsHandler({
    env: productionEnv(),
    fetchImpl: failingFetch,
  }), { query: { round_id: ROUND_ID } });
  assert.equal(upstream.statusCode, 502);
  assert.doesNotMatch(upstream.serialized, /secret upstream detail/);
});

test('handler rejects truncated round catalog responses', async () => {
  async function fetchImpl(url) {
    if (url.includes('/weekly_quiz_rounds')) {
      return { ok: true, json: async () => [] };
    }
    throw new Error('unexpected fetch');
  }
  const response = await invoke(createWeeklyPlayForFunResultsHandler({
    env: productionEnv(),
    fetchImpl,
  }), { query: { round_id: ROUND_ID } });
  assert.equal(response.statusCode, 502);
});

test('scorePlayForFunResults matches restored latest answers across opted-in sessions', () => {
  const manifest = revealManifestForRound(2);
  const result = scorePlayForFunResults({
    roundId: ROUND_ID,
    itemCount: 2,
    revealManifest: manifest,
    sessions: [
      postRevealSession({
        sessionId: SESSION_A1,
        userId: USER_A,
        displayName: 'Old Session',
        startedAt: '2026-08-20T09:00:00Z',
      }),
      postRevealSession({
        sessionId: SESSION_A2,
        userId: USER_A,
        displayName: 'Latest Session',
        startedAt: '2026-08-20T10:00:00Z',
      }),
    ],
    voteAttempts: [
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A1_NEW,
        sessionId: SESSION_A1,
        userId: USER_A,
        itemId: 'ITEM01',
        choiceId: 'choice-a',
        submittedAt: '2026-08-20T09:00:00Z',
      }),
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A2,
        sessionId: SESSION_A2,
        userId: USER_A,
        itemId: 'ITEM02',
        pickedNone: true,
        submittedAt: '2026-08-20T10:00:00Z',
      }),
    ],
  });
  assert.equal(result.participant_count, 1);
  assert.equal(result.complete_runs[0].display_name, 'Latest Session');
  assert.equal(result.complete_runs[0].correct, 2);
});

test('newer cross-session answer replaces an older answer for the same item', () => {
  const result = scorePlayForFunResults({
    roundId: ROUND_ID,
    itemCount: 1,
    revealManifest: revealManifestForRound(1),
    sessions: [
      postRevealSession({
        sessionId: SESSION_A1,
        userId: USER_A,
        displayName: 'Player A',
        startedAt: '2026-08-20T09:00:00Z',
      }),
      postRevealSession({
        sessionId: SESSION_A2,
        userId: USER_A,
        displayName: 'Player A',
        startedAt: '2026-08-20T10:00:00Z',
      }),
    ],
    voteAttempts: [
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A1_NEW,
        sessionId: SESSION_A1,
        userId: USER_A,
        itemId: 'ITEM01',
        choiceId: 'choice-a',
        submittedAt: '2026-08-20T09:00:00Z',
      }),
      postRevealAttempt({
        voteAttemptId: ATTEMPT_A2,
        sessionId: SESSION_A2,
        userId: USER_A,
        itemId: 'ITEM01',
        choiceId: 'choice-b',
        submittedAt: '2026-08-20T10:00:00Z',
      }),
    ],
  });
  assert.equal(result.complete_runs[0].correct, 0);
});
