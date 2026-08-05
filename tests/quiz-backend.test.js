import test from 'node:test';
import assert from 'node:assert/strict';
import { createQuizBackend, initQuizBackend } from '../quiz-backend.js';

function memoryStorage() {
  const data = new Map();
  return {
    getItem(key) {
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      data.set(key, String(value));
    },
    removeItem(key) {
      data.delete(key);
    },
  };
}

function sequenceUuid(...ids) {
  let index = 0;
  return () => ids[index++] ?? `uuid-${index}`;
}

function fakeSupabase() {
  const writes = [];
  let failing = false;

  function from(table) {
    const chain = {
      upsert(value, _options) {
        if (failing) return Promise.resolve({ error: new Error('write failed') });
        writes.push({ table, value, op: 'upsert' });
        return Promise.resolve({ error: null });
      },
      update(value) {
        return {
          eq(column, eqValue) {
            return {
              eq(column2, eqValue2) {
                if (failing) return Promise.resolve({ error: new Error('write failed') });
                writes.push({
                  table,
                  value: { ...value, [column]: eqValue, [column2]: eqValue2 },
                  op: 'update',
                });
                return Promise.resolve({ error: null });
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
  };

  return {
    client,
    writes,
    setFailing(value) {
      failing = value;
    },
  };
}

test('empty configuration disables remote persistence without loading Supabase', async () => {
  const backend = await initQuizBackend({}, {
    createClient: () => { throw new Error('must not load'); },
  });
  assert.equal(backend.startSession({ source: 'cameo', difficulty: 'easy' }), null);
  await backend.flush();
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
  assert.deepEqual(received.slice(0, 2), [
    'https://example.supabase.co',
    'sb_publishable_test',
  ]);
  assert.equal(backend.startSession({ source: 'cameo', difficulty: 'easy' }), 'session-id');
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
  assert.equal(storage.getItem('foldariumSyncQueueV1'), '[]');
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
    assert.notEqual(storage.getItem('foldariumSyncQueueV1'), '[]');

    setFailing(false);
    await backend.flush();
    assert.equal(storage.getItem('foldariumSyncQueueV1'), '[]');
    assert.deepEqual(warnings, [['Quiz results remain queued:', 'write failed']]);
  } finally {
    console.warn = originalWarn;
  }
});
