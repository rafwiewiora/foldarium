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
    backend.claimUsername('player_one'),
    /leaderboard persistence is unavailable/i,
  );
  await assert.rejects(
    backend.getLeaderboard(),
    /leaderboard persistence is unavailable/i,
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
  assert.match(html, /void initPersistence\(\);\s*await loadScript\('app\.js'\);/);
  assert.doesNotMatch(html, /await initQuizBackend/);
});

test('dev mode disables every remote research lifecycle call', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  assert.match(app, /const researchBackend = \(\) => DEV \? null : window\.foldariumBackend;/);
  assert.equal((app.match(/researchBackend\(\)\?\./g) || []).length, 3);
  assert.doesNotMatch(app, /window\.foldariumBackend\?\./);
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
