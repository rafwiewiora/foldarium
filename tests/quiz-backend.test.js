import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as quizBackendModule from '../quiz-backend.js';
import { createQuizBackend, initQuizBackend } from '../quiz-backend.js';

function memoryStorage() {
  const data = new Map();
  return {
    get length() {
      return data.size;
    },
    key(index) {
      return [...data.keys()][index] ?? null;
    },
    getItem(key) {
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      data.set(key, String(value));
    },
    removeItem(key) {
      data.delete(key);
    },
    keys() {
      return [...data.keys()];
    },
  };
}

function sequenceUuid(...ids) {
  let index = 0;
  return () => ids[index++] ?? `uuid-${index}`;
}

function fakeSupabase() {
  const writes = [];
  const rpcs = [];
  let failing = false;
  let errors = [];
  const rpcResults = new Map();

  function writeResult(write) {
    if (failing) return Promise.resolve({ error: new Error('write failed') });
    if (errors.length) return Promise.resolve({ error: errors.shift() });
    writes.push(write);
    return Promise.resolve({ error: null });
  }

  function from(table) {
    const chain = {
      upsert(value, _options) {
        return writeResult({ table, value, op: 'upsert' });
      },
      update(value) {
        return {
          eq(column, eqValue) {
            return {
              eq(column2, eqValue2) {
                return writeResult({
                  table,
                  value: { ...value, [column]: eqValue, [column2]: eqValue2 },
                  op: 'update',
                });
              },
            };
          },
        };
      },
    };
    return chain;
  }

  const client = {
    auth: {
      getSession: async () => ({
        data: { session: { user: { id: 'user-1' } } },
        error: null,
      }),
      signInAnonymously: async () => ({
        data: { user: { id: 'user-1' } },
        error: null,
      }),
    },
    from,
    rpc(name, args) {
      rpcs.push({ name, args });
      return Promise.resolve(
        rpcResults.get(name) ?? { data: null, error: null },
      );
    },
  };

  return {
    client,
    writes,
    rpcs,
    setFailing(value) {
      failing = value;
    },
    setErrors(...values) {
      errors = values;
    },
    setRpcResult(name, result) {
      rpcResults.set(name, result);
    },
  };
}

function answerRecord(overrides = {}) {
  return {
    item_id: 'item-7',
    source: 'cameo',
    difficulty: 'hard',
    picked_none: true,
    picked_sample: -1,
    picked_correct: true,
    picked_rmsd: null,
    af3_pick_sample: 3,
    af3_correct: false,
    has_correct: false,
    n_clusters: 4,
    ts: 1785952800,
    ...overrides,
  };
}

function answerWrite(writes) {
  return writes.find(write => write.table === 'quiz_answers' && write.op === 'upsert');
}

async function captureWarnings(run) {
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    await run();
    return warnings;
  } finally {
    console.warn = originalWarn;
  }
}

test('empty configuration disables remote persistence without loading Supabase', async () => {
  const backend = await initQuizBackend({}, {
    createClient: () => { throw new Error('must not load'); },
  });
  assert.equal(backend.startSession({ source: 'cameo', difficulty: 'easy' }), null);
  await backend.flush();
  await assert.rejects(
    backend.flush({ strict: true }),
    /persistence is unavailable/i,
  );
  await assert.rejects(
    backend.claimUsername('player_one'),
    /leaderboard persistence is unavailable/i,
  );
  await assert.rejects(
    backend.getLeaderboard(),
    /leaderboard persistence is unavailable/i,
  );
  assert.deepEqual(await backend.getWeeklyVotes('round-1'), []);
  assert.deepEqual(await backend.getWeeklyVoteTotals('round-1'), []);
});

test('read-only Preview loads public weekly data but cannot write or create auth users', async () => {
  const supabase = fakeSupabase();
  supabase.setRpcResult('get_current_weekly_quiz_round', {
    data: [{ round_id: 'weekly-v2' }], error: null,
  });
  supabase.setRpcResult('get_weekly_quiz_vote_totals', {
    data: [{ item_id: 'item-1', vote_count: 4 }], error: null,
  });
  let acquisitions = 0;
  const backend = initQuizBackend(
    {
      url: 'https://example.supabase.co',
      publishableKey: 'sb_publishable_test',
      writable: false,
      deploymentEnvironment: 'preview',
    },
    {
      createClient: () => { acquisitions++; return supabase.client; },
      storage: memoryStorage(),
    },
  );

  assert.deepEqual(await backend.getWeeklyRound(), { round_id: 'weekly-v2' });
  assert.deepEqual(await backend.getWeeklyVoteTotals('weekly-v2'), [
    { item_id: 'item-1', vote_count: 4 },
  ]);
  assert.deepEqual(await backend.getWeeklyVotes('weekly-v2'), []);
  await assert.rejects(
    backend.startNamedSession({ displayName: 'Ada' }),
    /Preview is read-only/i,
  );
  await assert.rejects(
    backend.submitWeeklyPerformanceReport({}),
    /Preview is read-only/i,
  );
  await assert.rejects(backend.flush({ strict: true }), /Preview is read-only/i);
  assert.equal(acquisitions, 1);
  assert.deepEqual(supabase.rpcs[0], {
    name: 'get_current_weekly_quiz_round',
    args: { p_environment: 'preview' },
  });
  assert.deepEqual(
    supabase.rpcs.map(row => row.name),
    ['get_current_weekly_quiz_round', 'get_weekly_quiz_vote_totals'],
  );
});

test('claims a username and loads shared leaderboard rows through Supabase RPCs', async () => {
  const { client, rpcs, setRpcResult } = fakeSupabase();
  const rows = [{
    username: 'player_one',
    items: 12,
    sessions: 2,
    accuracy: 75,
    af3_accuracy: 50,
    beat_af3_by: 25,
  }];
  setRpcResult('claim_leaderboard_username', { data: 'player_one', error: null });
  setRpcResult('get_leaderboard', { data: rows, error: null });
  const backend = createQuizBackend({ client, storage: memoryStorage() });

  assert.equal(await backend.claimUsername('player_one'), 'player_one');
  assert.deepEqual(await backend.getLeaderboard(), rows);
  assert.deepEqual(rpcs, [
    {
      name: 'claim_leaderboard_username',
      args: { p_username: 'player_one' },
    },
    { name: 'get_leaderboard', args: undefined },
  ]);
});

test('loads the current blind round and submits one server-validated weekly vote', async () => {
  const { client, rpcs, setRpcResult } = fakeSupabase();
  const round = { round_id: '2026-08-08', public_status: 'open', blind_manifest: { items: [] } };
  setRpcResult('get_current_weekly_quiz_round', { data: [round], error: null });
  setRpcResult('get_my_weekly_quiz_votes', {
    data: [{ item_id: 'item-1', choice_id: 'choice-2', picked_none: false }], error: null,
  });
  setRpcResult('get_weekly_quiz_vote_totals', {
    data: [{ item_id: 'item-1', choice_id: 'choice-2', picked_none: false, vote_count: 3 }],
    error: null,
  });
  setRpcResult('submit_weekly_quiz_vote', { data: { vote_id: 'vote-id' }, error: null });
  const backend = createQuizBackend({
    client,
    storage: memoryStorage(),
    uuid: sequenceUuid('vote-id'),
  });

  assert.deepEqual(await backend.getWeeklyRound(), round);
  assert.deepEqual(await backend.getWeeklyVotes('2026-08-08'), [
    { item_id: 'item-1', choice_id: 'choice-2', picked_none: false },
  ]);
  assert.deepEqual(await backend.getWeeklyVoteTotals('2026-08-08'), [
    { item_id: 'item-1', choice_id: 'choice-2', picked_none: false, vote_count: 3 },
  ]);
  assert.deepEqual(
    await backend.submitWeeklyVote('2026-08-08', 'item-1', 'choice-2', false),
    { vote_id: 'vote-id' },
  );
  assert.deepEqual(rpcs, [
    {
      name: 'get_current_weekly_quiz_round',
      args: { p_environment: 'production' },
    },
    { name: 'get_my_weekly_quiz_votes', args: { p_round_id: '2026-08-08' } },
    { name: 'get_weekly_quiz_vote_totals', args: { p_round_id: '2026-08-08' } },
    {
      name: 'submit_weekly_quiz_vote',
      args: {
        p_vote_id: 'vote-id',
        p_round_id: '2026-08-08',
        p_item_id: 'item-1',
        p_choice_id: 'choice-2',
        p_picked_none: false,
      },
    },
  ]);
});

test('requires a server-created named session before a classic quiz starts', async () => {
  const { client, rpcs } = fakeSupabase();
  const storage = memoryStorage();
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('00000000-0000-4000-8000-000000000101'),
  });

  const sessionId = await backend.startNamedSession({
    source: 'cameo',
    difficulty: 'easy',
    displayName: '  Ada   Lovelace  ',
  });

  assert.equal(sessionId, '00000000-0000-4000-8000-000000000101');
  assert.deepEqual(rpcs, [{
    name: 'start_named_quiz_session',
    args: {
      p_session_id: sessionId,
      p_source: 'cameo',
      p_difficulty: 'easy',
      p_display_name: 'Ada Lovelace',
    },
  }]);
  assert.deepEqual(storage.keys(), [], 'the display name must never enter browser storage');
});

test('persists a named weekly session, append-only traced vote, and contextual suggestion', async () => {
  const { client, rpcs } = fakeSupabase();
  const ids = [
    '00000000-0000-4000-8000-000000000201',
    '00000000-0000-4000-8000-000000000202',
    '00000000-0000-4000-8000-000000000203',
  ];
  const backend = createQuizBackend({
    client,
    storage: memoryStorage(),
    uuid: sequenceUuid(...ids),
    pagePath: '/quiz',
  });
  const appState = {
    schema_version: 1,
    source: 'weekly',
    item_id: 'item-1',
    active_pane_id: 'pane-0-2',
  };
  const trace = {
    version: 1,
    snapshots: [],
    app_trace: [{ t_ms: 1, action: 'choice_selected', state: appState }],
    app_state: appState,
  };

  const sessionId = await backend.startNamedSession({
    source: 'weekly',
    difficulty: 'hard',
    weeklyRoundId: 'weekly-2026-08-08',
    displayName: 'Grace Hopper',
    initialAppState: appState,
  });
  await backend.submitWeeklyVoteAttempt({
    sessionId,
    roundId: 'weekly-2026-08-08',
    itemId: 'item-1',
    questionIndex: 0,
    choiceId: 'choice-4',
    pickedNone: false,
    viewerTrace: trace,
    appState,
    voteComment: '  Strong hydrogen-bond network.  ',
  });
  await backend.submitUserSuggestion({
    sessionId,
    roundId: 'weekly-2026-08-08',
    itemId: 'item-1',
    suggestionText: '  Make the pane labels larger.  ',
    contextSnapshot: {
      app_state: appState,
      viewer_snapshot: { schema_version: 1, shared_camera: { radius: 4 } },
      viewer_trace_tail: { version: 1, snapshots: [] },
    },
  });

  assert.equal(sessionId, ids[0]);
  assert.deepEqual(rpcs, [
    {
      name: 'start_named_weekly_quiz_session',
      args: {
        p_session_id: ids[0],
        p_round_id: 'weekly-2026-08-08',
        p_display_name: 'Grace Hopper',
        p_initial_app_state: appState,
      },
    },
    {
      name: 'submit_weekly_quiz_vote_attempt',
      args: {
        p_vote_attempt_id: ids[1],
        p_session_id: ids[0],
        p_round_id: 'weekly-2026-08-08',
        p_item_id: 'item-1',
        p_question_index: 0,
        p_choice_id: 'choice-4',
        p_picked_none: false,
        p_viewer_trace: trace,
        p_app_state: appState,
        p_active_pane_id: 'pane-0-2',
        p_vote_comment: 'Strong hydrogen-bond network.',
      },
    },
    {
      name: 'submit_user_suggestion',
      args: {
        p_suggestion_id: ids[2],
        p_suggestion_text: 'Make the pane labels larger.',
        p_context: 'weekly-quiz',
        p_quiz_session_id: null,
        p_weekly_session_id: ids[0],
        p_item_id: 'item-1',
        p_page_path: '/quiz',
        p_app_state: appState,
        p_viewer_snapshot: { schema_version: 1, shared_camera: { radius: 4 } },
        p_viewer_trace_tail: { version: 1, snapshots: [] },
      },
    },
  ]);
});

test('routes post-reveal sessions and votes to the isolated annotated cohort', async () => {
  const { client, rpcs, setRpcResult } = fakeSupabase();
  const ids = [
    '00000000-0000-4000-8000-000000000211',
    '00000000-0000-4000-8000-000000000212',
  ];
  setRpcResult('get_my_weekly_post_reveal_votes', {
    data: [{
      item_id: 'item-1',
      choice_id: 'choice-4',
      picked_none: false,
      selection_kind: 'exact',
      selection_id: 'choice-4',
      submission_phase: 'post_reveal',
    }],
    error: null,
  });
  const backend = createQuizBackend({
    client,
    storage: memoryStorage(),
    uuid: sequenceUuid(...ids),
  });
  const appState = {
    schema_version: 1,
    source: 'weekly',
    item_id: 'item-1',
    selection_kind: 'exact',
    selected_choice_id: 'choice-4',
  };
  const sessionId = await backend.startNamedSession({
    source: 'weekly',
    difficulty: 'hard',
    weeklyRoundId: 'weekly-2026-08-08',
    displayName: 'Grace Hopper',
    initialAppState: appState,
    postReveal: true,
  });
  await backend.submitWeeklyVoteAttempt({
    sessionId,
    roundId: 'weekly-2026-08-08',
    itemId: 'item-1',
    questionIndex: 0,
    choiceId: 'choice-4',
    pickedNone: false,
    appState,
    postReveal: true,
  });
  const votes = await backend.getWeeklyVotes('weekly-2026-08-08', {
    postReveal: true,
  });

  assert.equal(votes[0].submission_phase, 'post_reveal');
  assert.deepEqual(rpcs.map(row => row.name), [
    'start_named_weekly_post_reveal_session',
    'submit_weekly_post_reveal_vote_attempt',
    'get_my_weekly_post_reveal_votes',
  ]);
  assert.equal(rpcs[1].args.p_app_state.selection_kind, 'exact');
});

test('resumes an owner-bound weekly session with monotonic trace continuation metadata', async () => {
  const { client, rpcs, setRpcResult } = fakeSupabase();
  setRpcResult('resume_named_weekly_quiz_session', {
    data: [{
      session_id: '00000000-0000-4000-8000-000000000701',
      round_id: 'weekly-2026-08-08',
      next_visit_ordinal: 12,
      last_visit_started_at: 1770000000123,
    }],
    error: null,
  });
  const backend = createQuizBackend({ client, storage: memoryStorage() });

  assert.deepEqual(await backend.resumeNamedWeeklySession({
    sessionId: '00000000-0000-4000-8000-000000000701',
    roundId: 'weekly-2026-08-08',
  }), {
    sessionId: '00000000-0000-4000-8000-000000000701',
    nextVisitOrdinal: 12,
    lastVisitStartedAt: 1770000000123,
  });
  assert.deepEqual(rpcs, [{
    name: 'resume_named_weekly_quiz_session',
    args: {
      p_session_id: '00000000-0000-4000-8000-000000000701',
      p_round_id: 'weekly-2026-08-08',
    },
  }]);
});

test('rejects malformed named research events before any RPC', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({ client, storage: memoryStorage() });

  await assert.rejects(
    backend.startNamedSession({ source: 'weekly', difficulty: 'hard', displayName: 'Name' }),
    /weekly session identity/i,
  );
  await assert.rejects(
    backend.submitWeeklyVoteAttempt({
      sessionId: 'session', roundId: 'round', itemId: 'item', questionIndex: -1,
      choiceId: null, pickedNone: true,
    }),
    /identity is invalid/i,
  );
  await assert.rejects(
    backend.submitUserSuggestion({
      sessionId: 'session', suggestionText: 'x', contextSnapshot: 'not-an-object',
    }),
    /context is invalid/i,
  );
  assert.deepEqual(rpcs, []);
});

test('submits one bounded idempotent weekly thinking-trace batch', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({ client, storage: memoryStorage() });
  const trace = {
    version: 1,
    stream_schema_version: 2,
    molstar_version: '4.6.0',
    visit_id: '00000000-0000-4000-8000-000000000003',
    visit_started_at: 1000,
    visit_ordinal: 0,
    entries: [
      { seq: 0, t_ms: 0, kind: 'app', action: 'question_start' },
      { seq: 1, t_ms: 250, kind: 'app', action: 'choice_rejected' },
    ],
  };
  const appState = { item_id: 'item-3', rejected_choice_ids: ['choice-2'] };
  await backend.submitWeeklyTraceBatch({
    traceBatchId: '00000000-0000-4000-8000-000000000001',
    sessionId: '00000000-0000-4000-8000-000000000002',
    roundId: 'weekly-1',
    itemId: 'item-3',
    questionIndex: 3,
    visitId: '00000000-0000-4000-8000-000000000003',
    firstSequence: 0,
    lastSequence: 1,
    reason: 'navigation',
    trace,
    appState,
  });

  assert.deepEqual(rpcs, [{
    name: 'append_weekly_quiz_trace_batch',
    args: {
      p_trace_batch_id: '00000000-0000-4000-8000-000000000001',
      p_session_id: '00000000-0000-4000-8000-000000000002',
      p_round_id: 'weekly-1',
      p_item_id: 'item-3',
      p_question_index: 3,
      p_visit_id: '00000000-0000-4000-8000-000000000003',
      p_first_sequence: 0,
      p_last_sequence: 1,
      p_flush_reason: 'navigation',
      p_trace: trace,
      p_app_state: appState,
    },
  }]);
});

test('rejects a trace batch whose visit or sequence bounds do not match its entries', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({ client, storage: memoryStorage() });
  await assert.rejects(() => backend.submitWeeklyTraceBatch({
    traceBatchId: '00000000-0000-4000-8000-000000000011',
    sessionId: '00000000-0000-4000-8000-000000000012',
    roundId: 'weekly-1',
    itemId: 'item-3',
    questionIndex: 3,
    visitId: '00000000-0000-4000-8000-000000000013',
    firstSequence: 1,
    lastSequence: 2,
    reason: 'navigation',
    trace: {
      version: 1,
      visit_id: '00000000-0000-4000-8000-000000000099',
      entries: [{ seq: 1 }, { seq: 2 }],
    },
  }), /sequence binding/);
  assert.deepEqual(rpcs, []);
});

test('submits one bounded performance report through its dedicated authenticated RPC', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({
    client,
    storage: memoryStorage(),
    uuid: sequenceUuid('00000000-0000-4000-8000-000000000041'),
  });
  const report = {
    schema_version: 'foldarium.viewer-performance-diagnostics/v1',
    consent: 'explicit-beta-checkbox',
    setup: { browser_family: 'Chrome' },
    question: { item_id: 'item-3', question_index: 3, total_ms: 2800 },
    structures: { request_count: 29 },
  };

  await backend.submitWeeklyPerformanceReport({
    sessionId: '00000000-0000-4000-8000-000000000042',
    roundId: 'weekly-1',
    itemId: 'item-3',
    questionIndex: 3,
    report,
  });

  assert.deepEqual(rpcs, [{
    name: 'append_weekly_viewer_performance_report',
    args: {
      p_report_id: '00000000-0000-4000-8000-000000000041',
      p_session_id: '00000000-0000-4000-8000-000000000042',
      p_round_id: 'weekly-1',
      p_item_id: 'item-3',
      p_question_index: 3,
      p_report: report,
    },
  }]);
});

test('rejects invalid and oversized performance reports before calling Supabase', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({ client, storage: memoryStorage() });
  const base = {
    sessionId: '00000000-0000-4000-8000-000000000042',
    roundId: 'weekly-1',
    itemId: 'item-3',
    questionIndex: 3,
  };

  await assert.rejects(
    backend.submitWeeklyPerformanceReport({
      ...base,
      report: { schema_version: 'wrong', setup: {}, question: {}, structures: {} },
    }),
    /performance report is invalid/i,
  );
  await assert.rejects(
    backend.submitWeeklyPerformanceReport({
      ...base,
      report: {
        schema_version: 'foldarium.viewer-performance-diagnostics/v1',
        consent: 'explicit-beta-checkbox',
        setup: {},
        question: { padding: 'x'.repeat(33 * 1024) },
        structures: {},
      },
    }),
    /32768-byte limit/i,
  );
  assert.deepEqual(rpcs, []);
});

test('keeps conservative headroom below the database jsonb text limit', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({ client, storage: memoryStorage() });
  const trace = {
    version: 1,
    stream_schema_version: 2,
    molstar_version: '4.6.0',
    visit_id: '00000000-0000-4000-8000-000000000013',
    visit_started_at: 1,
    visit_ordinal: 0,
    entries: [{ seq: 0, kind: 'app', action: 'x', state: { text: 'é'.repeat(160_000) } }],
  };
  const utf8Bytes = new TextEncoder().encode(JSON.stringify(trace)).byteLength;
  assert.ok(utf8Bytes > 300 * 1024 && utf8Bytes < 480 * 1024);
  await assert.rejects(() => backend.submitWeeklyTraceBatch({
    traceBatchId: '00000000-0000-4000-8000-000000000011',
    sessionId: '00000000-0000-4000-8000-000000000012',
    roundId: 'weekly-1',
    itemId: 'item-3',
    questionIndex: 3,
    visitId: trace.visit_id,
    firstSequence: 0,
    lastSequence: 0,
    reason: 'vote',
    trace,
  }), /307200-byte client limit/);
  assert.deepEqual(rpcs, []);
});

test('weekly vote-attempt callers can reuse one id across a network retry', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({
    client,
    storage: memoryStorage(),
    uuid: sequenceUuid('should-not-be-used'),
  });
  const attempt = {
    voteAttemptId: '00000000-0000-4000-8000-000000000777',
    sessionId: '00000000-0000-4000-8000-000000000201',
    roundId: 'weekly-2026-08-08',
    itemId: 'item-1',
    questionIndex: 0,
    choiceId: 'choice-1',
    pickedNone: false,
    viewerTrace: null,
    appState: { schema_version: 1, item_id: 'item-1' },
  };

  await backend.submitWeeklyVoteAttempt(attempt);
  await backend.submitWeeklyVoteAttempt(attempt);

  assert.equal(rpcs.length, 2);
  assert.equal(rpcs[0].args.p_vote_attempt_id, attempt.voteAttemptId);
  assert.deepEqual(rpcs[1].args, rpcs[0].args);
});

test('weekly votes survive an invalid optional viewer trace', async () => {
  const { client, rpcs } = fakeSupabase();
  const backend = createQuizBackend({
    client,
    storage: memoryStorage(),
    uuid: sequenceUuid('00000000-0000-4000-8000-000000000299'),
  });
  const appState = { schema_version: 1, source: 'weekly', item_id: 'item-1' };

  const warnings = await captureWarnings(() => backend.submitWeeklyVoteAttempt({
    sessionId: '00000000-0000-4000-8000-000000000201',
    roundId: 'weekly-2026-08-08',
    itemId: 'item-1',
    questionIndex: 0,
    choiceId: 'choice-1',
    pickedNone: false,
    viewerTrace: { version: 2, snapshots: [] },
    appState,
  }));

  assert.match(String(warnings[0]?.[0]), /Weekly viewer trace omitted: invalid version-1 shape/);
  assert.deepEqual(rpcs, [{
    name: 'submit_weekly_quiz_vote_attempt',
    args: {
      p_vote_attempt_id: '00000000-0000-4000-8000-000000000299',
      p_session_id: '00000000-0000-4000-8000-000000000201',
      p_round_id: 'weekly-2026-08-08',
      p_item_id: 'item-1',
      p_question_index: 0,
      p_choice_id: 'choice-1',
      p_picked_none: false,
      p_viewer_trace: null,
      p_app_state: appState,
      p_active_pane_id: null,
      p_vote_comment: null,
    },
  }]);
});

test('leaderboard RPC errors reject without creating a local fallback', async () => {
  const { client, setRpcResult } = fakeSupabase();
  const storage = memoryStorage();
  setRpcResult('claim_leaderboard_username', {
    data: null,
    error: Object.assign(new Error('username is already taken'), { code: '23505' }),
  });
  setRpcResult('get_leaderboard', {
    data: null,
    error: Object.assign(new Error('leaderboard unavailable'), { status: 503 }),
  });
  const backend = createQuizBackend({ client, storage });

  await assert.rejects(
    backend.claimUsername('Player_One'),
    /username is already taken/,
  );
  await assert.rejects(backend.getLeaderboard(), /leaderboard unavailable/);
  assert.deepEqual(storage.keys(), []);
});

test('strict flush rejects while retryable operations remain queued', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, setFailing } = fakeSupabase();
    const storage = memoryStorage();
    setFailing(true);
    const backend = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid('session-id'),
    });

    backend.startSession({ source: 'cameo', difficulty: 'easy' });
    await assert.rejects(
      backend.flush({ strict: true }),
      error => error.code === 'QUIZ_PERSISTENCE_INCOMPLETE'
        && /remain queued/i.test(error.message),
    );
    assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 1);
  });
  assert.deepEqual(warnings, [['Quiz results remain queued:', 'write failed']]);
});

test('strict flush rejects when an operation is dead-lettered', async () => {
  const { client, setErrors } = fakeSupabase();
  const storage = memoryStorage();
  setErrors({ message: 'row-level security rejected completion', status: 403, code: '42501' });
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('session-id'),
  });

  backend.startSession({ source: 'cameo', difficulty: 'easy' });
  await assert.rejects(
    backend.flush({ strict: true }),
    error => error.code === 'QUIZ_PERSISTENCE_INCOMPLETE'
      && /dead-lettered/i.test(error.message),
  );
  assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncDeadV2:')).length, 1);
  assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
});

test('strict flush ignores historical dead letters after the current drain succeeds', async () => {
  const { client, setErrors } = fakeSupabase();
  const storage = memoryStorage();
  setErrors({ message: 'historical permanent failure', status: 403, code: '42501' });
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('failed-session', 'later-session'),
  });

  backend.startSession({ source: 'cameo', difficulty: 'easy' });
  await backend.flush();
  assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncDeadV2:')).length, 1);

  backend.startSession({ source: 'rnp', difficulty: 'hard' });
  await backend.flush({ strict: true });
  assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
});

test('strict flush rejects and blocks leaderboard RPCs when completion cannot be queued', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, rpcs } = fakeSupabase();
    const storage = memoryStorage();
    const originalSetItem = storage.setItem;
    let rejectCompletion = false;
    storage.setItem = (key, value) => {
      const entry = JSON.parse(value);
      if (rejectCompletion && entry.kind === 'complete') {
        throw new Error('browser storage quota exceeded');
      }
      originalSetItem(key, value);
    };
    const backend = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid('session-id'),
    });

    const sessionId = backend.startSession({ source: 'cameo', difficulty: 'easy' });
    await backend.flush();
    rejectCompletion = true;

    let rankingsLoaded = false;
    try {
      backend.completeSession(sessionId);
      await backend.flush({ strict: true });
      await backend.claimUsername('player_one');
      await backend.getLeaderboard();
      rankingsLoaded = true;
    } catch (error) {
      assert.equal(error.code, 'QUIZ_PERSISTENCE_INCOMPLETE');
      assert.match(error.message, /could not be queued.*browser storage.*try saving again/i);
    }

    assert.equal(rankingsLoaded, false);
    assert.deepEqual(rpcs, []);
  });
  assert.deepEqual(warnings, [
    ['Quiz result queue could not be saved:', 'browser storage quota exceeded'],
  ]);
});

test('deferred strict and leaderboard calls wait for attach after queued lifecycle replay', async () => {
  assert.equal(
    typeof quizBackendModule.createDeferredBackend,
    'function',
    'expected a testable deferred backend factory',
  );
  const events = [];
  const deferred = quizBackendModule.createDeferredBackend({
    uuid: () => 'session-id',
  });
  deferred.completeSession('session-id');

  let strictSettled = false;
  const strict = deferred.flush({ strict: true }).finally(() => { strictSettled = true; });
  const claim = deferred.claimUsername('player_one');
  const leaderboard = deferred.getLeaderboard();
  await Promise.resolve();
  assert.equal(strictSettled, false);
  assert.deepEqual(events, []);

  deferred.attach({
    completeSession(sessionId) {
      events.push(`complete:${sessionId}`);
    },
    async flush(options) {
      events.push(`flush:${options.strict}`);
    },
    async claimUsername(username) {
      events.push(`claim:${username}`);
      return username;
    },
    async getLeaderboard() {
      events.push('leaderboard');
      return [];
    },
  });

  assert.deepEqual(await Promise.all([strict, claim, leaderboard]), [
    undefined,
    'player_one',
    [],
  ]);
  assert.equal(events[0], 'complete:session-id');
  assert.ok(events.indexOf('complete:session-id') < events.indexOf('flush:true'));
  assert.ok(events.indexOf('complete:session-id') < events.indexOf('claim:player_one'));
  assert.ok(events.indexOf('complete:session-id') < events.indexOf('leaderboard'));
});

test('deferred strict and leaderboard calls reject after initialization failure', async () => {
  assert.equal(
    typeof quizBackendModule.createDeferredBackend,
    'function',
    'expected a testable deferred backend factory',
  );
  const deferred = quizBackendModule.createDeferredBackend();
  const strict = deferred.flush({ strict: true });
  const claim = deferred.claimUsername('player_one');
  const leaderboard = deferred.getLeaderboard();

  deferred.fail(new Error('Supabase config failed to load'));

  await assert.rejects(strict, /persistence initialization failed.*Supabase config/i);
  await assert.rejects(claim, /persistence initialization failed.*Supabase config/i);
  await assert.rejects(leaderboard, /persistence initialization failed.*Supabase config/i);
});

test('configured initialization returns immediately and acquires the remote client only for queued work', async () => {
  const { client } = fakeSupabase();
  let acquisitions = 0;
  const backend = initQuizBackend(
    { url: 'https://example.supabase.co', publishableKey: 'sb_publishable_test' },
    {
      createClient: () => {
        acquisitions++;
        return client;
      },
      storage: memoryStorage(),
      uuid: sequenceUuid('session-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    },
  );

  assert.equal(typeof backend.startSession, 'function');
  await backend.flush();
  assert.equal(acquisitions, 0);
  assert.equal(backend.startSession({ source: 'cameo', difficulty: 'easy' }), 'session-id');
  await backend.flush();
  assert.equal(acquisitions, 1);
});

test('uses a preassigned session UUID from the startup fallback', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, setFailing } = fakeSupabase();
    const storage = memoryStorage();
    setFailing(true);
    const backend = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid('unexpected-id'),
    });

    const id = backend.startSession({
      id: 'startup-session-id',
      source: 'cameo',
      difficulty: 'easy',
    });
    await backend.flush();

    assert.equal(id, 'startup-session-id');
    assert.ok(storage.keys().some(key => key.endsWith(':startup-session-id')));
  });
  assert.deepEqual(warnings, [['Quiz results remain queued:', 'write failed']]);
});

test('quiz application loading does not await persistence startup', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(html, /window\.foldariumBackend = createDeferredBackend\(\);/);
  assert.match(html, /window\.foldariumBackend\.attach\(initQuizBackend/);
  assert.match(html, /window\.foldariumBackend\.fail\(e\);/);
  assert.ok(html.indexOf('void initPersistence();') < html.indexOf("await loadScript('app.js?v="));
  assert.doesNotMatch(html, /await initPersistence\(\)/);
  assert.doesNotMatch(html, /await initQuizBackend/);
});

test('dev mode disables every remote research lifecycle call', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  assert.match(app, /const researchBackend = \(\) => DEV \? null : window\.foldariumBackend;/);
  assert.ok((app.match(/researchBackend\(\)/g) || []).length >= 6);
  assert.match(app, /if \(DEV\) \{[\s\S]*?beginQuiz\(\);[\s\S]*?return;/);
  assert.doesNotMatch(app, /window\.foldariumBackend\?\./);
});

test('completion strictly persists before loading rankings and distinguishes failure stages', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const strictFlush = app.indexOf('await backend.flush({ strict: true });');
  const usernameClaim = app.indexOf('await backend.claimUsername(username);');
  const leaderboardRead = app.indexOf('await backend.getLeaderboard();');
  assert.ok(strictFlush >= 0, 'expected strict completion persistence');
  assert.ok(strictFlush < usernameClaim);
  assert.ok(usernameClaim < leaderboardRead);
  assert.ok(app.includes('pattern="[A-Za-z0-9_\\\\-]+"'));
  assert.match(app, /Quiz results could not be saved/);
  assert.match(app, /Check browser storage and your connection/);
  assert.match(app, /username could not be claimed/);
  assert.match(app, /leaderboard could not be loaded/);
});

test('configured initialization uses the publishable key', async () => {
  let received;
  const { client } = fakeSupabase();
  const backend = await initQuizBackend(
    { url: 'https://example.supabase.co', publishableKey: 'sb_publishable_test' },
    {
      createClient: (...args) => { received = args; return client; },
      storage: memoryStorage(),
      uuid: sequenceUuid('session-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    },
  );
  assert.equal(backend.startSession({ source: 'cameo', difficulty: 'easy' }), 'session-id');
  await backend.flush();
  assert.deepEqual(received.slice(0, 2), [
    'https://example.supabase.co',
    'sb_publishable_test',
  ]);
});

test('queues a normalized session and answer and removes them after successful writes', async () => {
  const { client, writes } = fakeSupabase();
  const storage = memoryStorage();
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('session-id', 'answer-id'),
    now: () => new Date('2026-08-05T18:00:00.000Z'),
  });

  const sessionId = backend.startSession({ source: 'cameo', difficulty: 'hard' });
  backend.recordAnswer(sessionId, 2, {
    item_id: 'item-7',
    source: 'cameo',
    difficulty: 'hard',
    picked_none: true,
    picked_sample: -1,
    picked_correct: true,
    picked_rmsd: null,
    af3_pick_sample: 3,
    af3_correct: false,
    has_correct: false,
    n_clusters: 4,
    ts: 1785952800,
  });
  await backend.flush();

  assert.equal(writes[0].table, 'quiz_sessions');
  assert.equal(writes[0].value.user_id, 'user-1');
  assert.equal(writes[1].value.picked_sample, null);
  assert.equal(writes[1].value.answered_at, '2026-08-05T18:00:00.000Z');
  assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
});

test('concurrent tabs persist operations under independent storage keys', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, setFailing } = fakeSupabase();
    const storage = memoryStorage();
    setFailing(true);
    const first = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid('session-1'),
    });
    const second = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid('session-2'),
    });

    first.startSession({ source: 'cameo', difficulty: 'easy' });
    second.startSession({ source: 'rnp', difficulty: 'hard' });
    await Promise.all([first.flush(), second.flush()]);

    const keys = storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:'));
    assert.equal(keys.length, 2);
    assert.ok(keys.some(key => key.endsWith(':session-1')));
    assert.ok(keys.some(key => key.endsWith(':session-2')));
  });
  assert.deepEqual(warnings, [
    ['Quiz results remain queued:', 'write failed'],
    ['Quiz results remain queued:', 'write failed'],
  ]);
});

test('concurrent duplicate attempts remain idempotent', async () => {
  const storage = memoryStorage();
  const entry = {
    kind: 'session',
    value: { id: 'shared-session', source: 'cameo', difficulty: 'easy' },
  };
  storage.setItem('foldariumSyncOpV2:0:shared-session', JSON.stringify(entry));
  const logicalSessions = new Map();
  let attempts = 0;
  const client = {
    auth: {
      getSession: async () => ({
        data: { session: { user: { id: 'user-1' } } },
        error: null,
      }),
    },
    from() {
      return {
        upsert(value) {
          attempts++;
          if (!logicalSessions.has(value.id)) logicalSessions.set(value.id, value);
          return Promise.resolve({ error: null });
        },
      };
    },
  };

  await Promise.all([
    createQuizBackend({ client, storage }).flush(),
    createQuizBackend({ client, storage }).flush(),
  ]);

  assert.equal(attempts, 2);
  assert.equal(logicalSessions.size, 1);
});

test('flushes sessions before answers and answers before completion', async () => {
  const { client, writes } = fakeSupabase();
  const storage = memoryStorage();
  const operations = [
    ['foldariumSyncOpV2:2:session-id', {
      kind: 'complete',
      value: { id: 'session-id', completed_at: '2026-08-05T18:02:00.000Z' },
    }],
    ['foldariumSyncOpV2:1:answer-id', {
      kind: 'answer',
      value: { id: 'answer-id', session_id: 'session-id' },
    }],
    ['foldariumSyncOpV2:0:session-id', {
      kind: 'session',
      value: { id: 'session-id', source: 'cameo', difficulty: 'easy' },
    }],
  ];
  for (const [key, entry] of operations) storage.setItem(key, JSON.stringify(entry));

  const backend = createQuizBackend({ client, storage });
  await backend.flush();

  assert.deepEqual(writes.map(write => `${write.op}:${write.table}`), [
    'upsert:quiz_sessions',
    'upsert:quiz_answers',
    'update:quiz_sessions',
  ]);
});

test('flush includes an event queued while another write is in flight', async () => {
  const storage = memoryStorage();
  const writes = [];
  let releaseFirst;
  let markFirstStarted;
  const firstStarted = new Promise(resolve => { markFirstStarted = resolve; });
  const client = {
    auth: {
      getSession: async () => ({
        data: { session: { user: { id: 'user-1' } } },
        error: null,
      }),
    },
    from() {
      return {
        upsert(value) {
          writes.push(value.id);
          if (writes.length === 1) {
            markFirstStarted();
            return new Promise(resolve => {
              releaseFirst = () => resolve({ error: null });
            });
          }
          return Promise.resolve({ error: null });
        },
      };
    },
  };
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('session-1', 'session-2'),
  });

  backend.startSession({ source: 'cameo', difficulty: 'easy' });
  await firstStarted;
  backend.startSession({ source: 'rnp', difficulty: 'hard' });
  releaseFirst();
  await backend.flush();

  assert.deepEqual(writes, ['session-1', 'session-2']);
  assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
});

test('dead-letters a permanent failure and continues with later events', async () => {
  const { client, writes, setErrors } = fakeSupabase();
  const storage = memoryStorage();
  setErrors({ message: 'violates row-level security', status: 403, code: '42501' });
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('a-rejected-session', 'z-accepted-session'),
    now: () => new Date('2026-08-05T18:00:00.000Z'),
  });

  backend.startSession({ source: 'cameo', difficulty: 'easy' });
  backend.startSession({ source: 'rnp', difficulty: 'hard' });
  await backend.flush();

  assert.deepEqual(writes.map(write => write.value.id), ['z-accepted-session']);
  assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
  const deadKeys = storage.keys().filter(key => key.startsWith('foldariumSyncDeadV2:'));
  assert.equal(deadKeys.length, 1);
  assert.ok(deadKeys[0].endsWith(':a-rejected-session'));
});

test('treats authorization, constraint, and other client errors as permanent', async () => {
  const errors = [
    { message: 'unauthorized', status: 401 },
    { message: 'check constraint failed', status: 400, code: '23514' },
    { message: 'invalid database request', status: 400, code: 'PGRST100' },
  ];

  for (const [index, error] of errors.entries()) {
    const { client, setErrors } = fakeSupabase();
    const storage = memoryStorage();
    setErrors(error);
    const backend = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid(`session-${index}`),
    });

    backend.startSession({ source: 'cameo', difficulty: 'easy' });
    await backend.flush();

    assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
    assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncDeadV2:')).length, 1);
  }
});

test('retains network, timeout, rate-limit, and server failures for retry', async () => {
  const errors = [
    new Error('network unavailable'),
    { message: 'request timeout', status: 408 },
    { message: 'rate limited', status: 429 },
    { message: 'service unavailable', status: 503 },
  ];

  const warnings = await captureWarnings(async () => {
    for (const [index, error] of errors.entries()) {
      const { client, setErrors } = fakeSupabase();
      const storage = memoryStorage();
      setErrors(error);
      const backend = createQuizBackend({
        client,
        storage,
        uuid: sequenceUuid(`session-${index}`),
      });

      backend.startSession({ source: 'cameo', difficulty: 'easy' });
      await backend.flush();

      assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 1);
      assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncDeadV2:')).length, 0);
    }
  });
  assert.deepEqual(warnings, errors.map(error => [
    'Quiz results remain queued:',
    error.message,
  ]));
});

test('retries remote client acquisition after a network failure', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, writes } = fakeSupabase();
    const storage = memoryStorage();
    let attempts = 0;
    const backend = initQuizBackend(
      { url: 'https://example.supabase.co', publishableKey: 'sb_publishable_test' },
      {
        createClient: () => {
          attempts++;
          if (attempts === 1) throw new Error('network import failed');
          return client;
        },
        storage,
        uuid: sequenceUuid('session-id'),
      },
    );

    backend.startSession({ source: 'cameo', difficulty: 'easy' });
    await backend.flush();
    await backend.flush();

    assert.equal(attempts, 2);
    assert.equal(writes.length, 1);
    assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
  });
  assert.deepEqual(warnings, [['Quiz results remain queued:', 'network import failed']]);
});

test('ambiguous commit retry keeps one logical row for the stable UUID', async () => {
  const warnings = await captureWarnings(async () => {
    const logicalSessions = new Map();
    let attempts = 0;
    const client = {
      auth: {
        getSession: async () => ({
          data: { session: { user: { id: 'user-1' } } },
          error: null,
        }),
      },
      from(table) {
        assert.equal(table, 'quiz_sessions');
        return {
          upsert(value, options) {
            attempts++;
            assert.deepEqual(options, { onConflict: 'id', ignoreDuplicates: true });
            if (!logicalSessions.has(value.id)) logicalSessions.set(value.id, value);
            return Promise.resolve({
              error: attempts === 1
                ? { message: 'connection lost after commit', status: 503 }
                : null,
            });
          },
        };
      },
    };
    const storage = memoryStorage();
    const backend = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid('stable-session-id'),
    });

    backend.startSession({ source: 'cameo', difficulty: 'easy' });
    await backend.flush();
    await backend.flush();

    assert.equal(attempts, 2);
    assert.equal(logicalSessions.size, 1);
    assert.ok(logicalSessions.has('stable-session-id'));
  });
  assert.deepEqual(warnings, [
    ['Quiz results remain queued:', 'connection lost after commit'],
  ]);
});

test('persists a serializable viewer trace with the answer', async () => {
  const { client, writes } = fakeSupabase();
  const backend = createQuizBackend({
    client,
    storage: memoryStorage(),
    uuid: sequenceUuid('answer-id'),
    now: () => new Date('2026-08-05T18:00:00.000Z'),
  });
  const trace = {
    version: 1,
    molstar_version: '4.6.0',
    duration_ms: 500,
    truncated: false,
    snapshots: [],
  };
  backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: trace }));
  await backend.flush();
  assert.deepEqual(answerWrite(writes).value.viewer_trace, trace);
});

test('retries the same answer without its trace when storage rejects the trace-backed operation', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, writes } = fakeSupabase();
    const storage = memoryStorage();
    const attempts = [];
    const originalSetItem = storage.setItem;
    storage.setItem = (key, value) => {
      const entry = JSON.parse(value);
      attempts.push({ key, entry });
      if (entry.value.viewer_trace !== null) throw new Error('quota exceeded');
      originalSetItem(key, value);
    };
    const backend = createQuizBackend({
      client,
      storage,
      uuid: sequenceUuid('answer-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    });

    backend.recordAnswer('session-id', 0, answerRecord({
      viewer_trace: {
        version: 1,
        molstar_version: '4.6.0',
        snapshots: [{ t_ms: 0, kind: 'state', snapshot: { data: 'large' } }],
      },
    }));
    await backend.flush();

    assert.equal(attempts.length, 2);
    assert.equal(attempts[0].key, attempts[1].key);
    assert.deepEqual(attempts[1].entry.value, {
      ...attempts[0].entry.value,
      viewer_trace: null,
    });
    assert.equal(attempts[1].entry.value.id, 'answer-id');
    assert.equal(attempts[1].entry.value.viewer_trace, null);
    assert.deepEqual(answerWrite(writes).value, attempts[1].entry.value);
  });

  assert.deepEqual(warnings, [
    ['Viewer trace omitted:', 'local queue rejected trace-backed answer'],
  ]);
});

test('proactively omits a viewer trace over the serialized byte budget and queues the answer', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, writes } = fakeSupabase();
    const backend = createQuizBackend({
      client,
      storage: memoryStorage(),
      uuid: sequenceUuid('answer-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    });

    backend.recordAnswer('session-id', 0, answerRecord({
      viewer_trace: {
        version: 1,
        molstar_version: '4.6.0',
        snapshots: [{ t_ms: 0, kind: 'state', snapshot: { data: 'é'.repeat(300_000) } }],
      },
    }));
    await backend.flush();

    assert.equal(answerWrite(writes).value.id, 'answer-id');
    assert.equal(answerWrite(writes).value.viewer_trace, null);
  });

  assert.deepEqual(warnings, [
    ['Viewer trace omitted:', 'exceeds 524288-byte limit'],
  ]);
});

test('retains a JSON-safe viewer trace snapshot after caller mutation before flush', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, writes, setFailing } = fakeSupabase();
    const storage = memoryStorage();
    setFailing(true);
    const trace = {
      version: 1,
      molstar_version: '4.6.0',
      duration_ms: 500,
      truncated: false,
      snapshots: [],
    };
    const expected = structuredClone(trace);
    let stringifyCalls = 0;
    const originalStringify = JSON.stringify;
    JSON.stringify = (value, ...args) => {
      stringifyCalls++;
      const result = originalStringify(value, ...args);
      if (stringifyCalls === 1 && value === trace) trace.self = trace;
      return result;
    };
    try {
      const backend = createQuizBackend({
        client,
        storage,
        uuid: sequenceUuid('answer-id'),
        now: () => new Date('2026-08-05T18:00:00.000Z'),
      });
      backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: trace }));
      await backend.flush();

      const queueKey = storage.keys().find(key => key.startsWith('foldariumSyncOpV2:1:'));
      assert.ok(queueKey, 'expected answer to remain queued');
      const queued = JSON.parse(storage.getItem(queueKey));
      assert.deepEqual(queued.value.viewer_trace, expected);
      assert.notEqual(queued.value.viewer_trace, trace);

      setFailing(false);
      await backend.flush();
      assert.deepEqual(answerWrite(writes).value.viewer_trace, expected);
    } finally {
      JSON.stringify = originalStringify;
    }
  });
  assert.deepEqual(warnings, [['Quiz results remain queued:', 'write failed']]);
});

test('normalizes only JSON-safe version-1 trace objects', () => {
  assert.equal(
    typeof quizBackendModule.normalizeViewerTrace,
    'function',
    'expected a pure viewer trace normalizer export',
  );
  const valid = {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [],
  };
  const normalized = quizBackendModule.normalizeViewerTrace(valid);
  assert.deepEqual(normalized, valid);
  assert.notEqual(normalized, valid);

  assert.equal(quizBackendModule.normalizeViewerTrace({
    version: () => 1,
    snapshots: [],
  }), null);
  assert.equal(quizBackendModule.normalizeViewerTrace({
    version: 1,
    snapshots: Symbol('snapshots'),
  }), null);
  assert.equal(quizBackendModule.normalizeViewerTrace({
    version: '1',
    snapshots: [],
  }), null);
});

for (const [label, viewerTrace] of [
  ['function-valued version', { version: () => 1, snapshots: [] }],
  ['symbol-valued snapshots', { version: 1, snapshots: Symbol('snapshots') }],
]) {
  test(`stores the answer without a trace or dead letter for a nested ${label}`, async () => {
    const warnings = await captureWarnings(async () => {
      const { client, writes } = fakeSupabase();
      const storage = memoryStorage();
      const backend = createQuizBackend({
        client,
        storage,
        uuid: sequenceUuid('answer-id'),
        now: () => new Date('2026-08-05T18:00:00.000Z'),
      });

      backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: viewerTrace }));
      await backend.flush();

      const persisted = answerWrite(writes);
      assert.ok(persisted, 'scientific answer should be persisted');
      assert.equal(persisted.value.id, 'answer-id');
      assert.equal(persisted.value.item_id, 'item-7');
      assert.equal(persisted.value.viewer_trace, null);
      assert.equal(
        storage.keys().filter(key => key.startsWith('foldariumSyncDeadV2:')).length,
        0,
      );
    });
    assert.deepEqual(warnings, [['Viewer trace omitted:', 'invalid version-1 shape']]);
  });
}

test('stores the answer without a cyclic viewer trace', async () => {
  const warnings = await captureWarnings(async () => {
    const { client, writes } = fakeSupabase();
    const backend = createQuizBackend({
      client,
      storage: memoryStorage(),
      uuid: sequenceUuid('answer-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    });
    const trace = {};
    trace.self = trace;
    backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: trace }));
    await backend.flush();
    assert.equal(answerWrite(writes).value.viewer_trace, null);
  });
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0][0], 'Viewer trace omitted:');
  assert.match(warnings[0][1], /Converting circular structure to JSON/);
});

test('stores the answer without a function viewer trace', async () => {
  const { client, writes } = fakeSupabase();
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    const backend = createQuizBackend({
      client,
      storage: memoryStorage(),
      uuid: sequenceUuid('answer-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    });
    backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: () => {} }));
    await backend.flush();
    assert.equal(answerWrite(writes).value.viewer_trace, null);
    assert.equal(answerWrite(writes).value.item_id, 'item-7');
    assert.deepEqual(warnings, [['Viewer trace omitted:', 'not JSON-serializable']]);
  } finally {
    console.warn = originalWarn;
  }
});

test('stores the answer without a symbol viewer trace', async () => {
  const { client, writes } = fakeSupabase();
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    const backend = createQuizBackend({
      client,
      storage: memoryStorage(),
      uuid: sequenceUuid('answer-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    });
    backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: Symbol('trace') }));
    await backend.flush();
    assert.equal(answerWrite(writes).value.viewer_trace, null);
    assert.equal(answerWrite(writes).value.item_id, 'item-7');
    assert.deepEqual(warnings, [['Viewer trace omitted:', 'not JSON-serializable']]);
  } finally {
    console.warn = originalWarn;
  }
});

test('retains queued writes after a Supabase error and retries idempotently', async () => {
  const { client, setFailing } = fakeSupabase();
  const storage = memoryStorage();
  const originalWarn = console.warn;
  const warnings = [];
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('session-id'),
    now: () => new Date('2026-08-05T18:00:00.000Z'),
  });

  console.warn = (...args) => warnings.push(args);
  try {
    setFailing(true);
    backend.startSession({ source: 'rnp', difficulty: 'easy' });
    await backend.flush();
    assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 1);

    setFailing(false);
    await backend.flush();
    assert.equal(storage.keys().filter(key => key.startsWith('foldariumSyncOpV2:')).length, 0);
    assert.deepEqual(warnings, [['Quiz results remain queued:', 'write failed']]);
  } finally {
    console.warn = originalWarn;
  }
});
