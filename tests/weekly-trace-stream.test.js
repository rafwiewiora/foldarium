import test from 'node:test';
import assert from 'node:assert/strict';
import { createWeeklyTraceStream } from '../weekly-trace-stream.js';

function memoryStore() {
  const records = new Map();
  return {
    records,
    async put(record) {
      records.set(record.traceBatchId, structuredClone(record));
    },
    async list() {
      return [...records.values()].sort((left, right) => left.queuedAt - right.queuedAt);
    },
    async delete(id) {
      records.delete(id);
    },
  };
}

function uuids(...values) {
  let index = 0;
  return () => values[index++] || `uuid-${index}`;
}

test('flushes one append-only visit batch and removes it only after acknowledgement', async () => {
  const store = memoryStore();
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store,
    uuid: uuids('visit-1', 'batch-1'),
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
    now: () => 1234,
    getAppState: () => ({ item_id: 'item-1', display_mode: 'grid' }),
  });
  stream.startSession({ sessionId: 'session-1', roundId: 'round-1' });
  assert.equal(stream.startVisit({ itemId: 'item-1', questionIndex: 0 }), 'visit-1');
  stream.recordEntry({ seq: 0, t_ms: 0, kind: 'app', action: 'question_start' });
  stream.recordEntry({ seq: 1, t_ms: 100, kind: 'camera', camera: { radius: 12 } });
  await stream.flush('interval');

  assert.equal(store.records.size, 0);
  assert.equal(submitted.length, 1);
  assert.deepEqual(submitted[0], {
    traceBatchId: 'batch-1',
    sessionId: 'session-1',
    roundId: 'round-1',
    itemId: 'item-1',
    questionIndex: 0,
    visitId: 'visit-1',
    firstSequence: 0,
    lastSequence: 1,
    reason: 'interval',
    trace: {
      version: 1,
      stream_schema_version: 2,
      molstar_version: '4.6.0',
      visit_id: 'visit-1',
      visit_started_at: 1234,
      visit_ordinal: 0,
      entries: [
        { seq: 0, t_ms: 0, kind: 'app', action: 'question_start' },
        { seq: 1, t_ms: 100, kind: 'camera', camera: { radius: 12 } },
      ],
    },
    appState: { item_id: 'item-1', display_mode: 'grid' },
  });
});

test('continues visit ordering monotonically after a same-session tab refresh', async () => {
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-resumed', 'batch-resumed'),
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
    now: () => 1000,
  });
  stream.startSession({
    sessionId: 'session-resumed',
    roundId: 'round-resumed',
    nextVisitOrdinal: 7,
    lastVisitStartedAt: 2000,
  });
  stream.startVisit({ itemId: 'item-resumed', questionIndex: 4 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'question_loaded' });
  await stream.flush('navigation');

  assert.equal(submitted[0].trace.visit_ordinal, 7);
  assert.equal(submitted[0].trace.visit_started_at, 2001);
});

test('rejects invalid same-session continuation metadata', () => {
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    submitBatch: async () => {},
    setTimer: () => 1,
    clearTimer: () => {},
  });
  assert.throws(() => stream.startSession({
    sessionId: 'session-invalid', roundId: 'round-invalid', nextVisitOrdinal: -1,
  }), /continuation metadata/);
});

test('retains an identical idempotent batch through retryable submission failure', async () => {
  const store = memoryStore();
  const attempts = [];
  let failing = true;
  const stream = createWeeklyTraceStream({
    store,
    uuid: uuids('visit-2', 'batch-2'),
    submitBatch: async payload => {
      attempts.push(structuredClone(payload));
      if (failing) throw new Error('network timeout');
    },
    setTimer: () => 1,
    clearTimer: () => {},
    onWarning: () => {},
  });
  stream.startSession({ sessionId: 'session-2', roundId: 'round-2' });
  stream.startVisit({ itemId: 'item-2', questionIndex: 2 });
  stream.recordEntry({ seq: 0, t_ms: 90, kind: 'app', action: 'choice_rejected' });
  await stream.flush('navigation');
  assert.equal(store.records.size, 1);

  failing = false;
  await stream.drain();
  assert.equal(store.records.size, 0);
  assert.equal(attempts.length, 2);
  assert.deepEqual(attempts[0], attempts[1]);
  assert.equal(attempts[1].traceBatchId, 'batch-2');
});

test('vote checkpoint returns the exact durable replay boundary while offline', async () => {
  const store = memoryStore();
  const stream = createWeeklyTraceStream({
    store,
    uuid: uuids('visit-checkpoint', 'batch-checkpoint'),
    submitBatch: async () => { throw new Error('offline'); },
    setTimer: () => 1,
    clearTimer: () => {},
    onWarning: () => {},
  });
  stream.startSession({ sessionId: 'session-checkpoint', roundId: 'round-checkpoint' });
  stream.startVisit({ itemId: 'item-checkpoint', questionIndex: 3 });
  stream.recordEntry({ seq: 0, t_ms: 0, kind: 'state', snapshot: {} });
  stream.recordEntry({ seq: 1, t_ms: 10, kind: 'app', action: 'vote_submitted' });

  assert.deepEqual(await stream.checkpoint('vote'), {
    visitId: 'visit-checkpoint', throughSequence: 1, durable: true,
  });
  assert.equal(store.records.size, 1, 'offline batches remain durable in IndexedDB');
});

test('vote checkpoint requests the legacy safety snapshot if durable storage fails', async () => {
  const stream = createWeeklyTraceStream({
    store: {
      async put() { throw new Error('quota exceeded'); },
      async list() { return []; },
      async delete() {},
    },
    uuid: uuids('visit-volatile', 'batch-volatile'),
    submitBatch: async () => { throw new Error('offline'); },
    setTimer: () => 1,
    clearTimer: () => {},
    onWarning: () => {},
  });
  stream.startSession({ sessionId: 'session-volatile', roundId: 'round-volatile' });
  stream.startVisit({ itemId: 'item-volatile', questionIndex: 2 });
  stream.recordEntry({ seq: 0, kind: 'state', snapshot: {} });
  assert.equal((await stream.checkpoint('vote')).durable, false);
});

test('vote checkpoint waits only for local durability, not a trace network round trip', async () => {
  const store = memoryStore();
  let release;
  const submitting = new Promise(resolve => { release = resolve; });
  const stream = createWeeklyTraceStream({
    store,
    uuid: uuids('visit-fast', 'batch-fast'),
    submitBatch: async () => submitting,
    setTimer: () => 1,
    clearTimer: () => {},
  });
  stream.startSession({ sessionId: 'session-fast', roundId: 'round-fast' });
  stream.startVisit({ itemId: 'item-fast', questionIndex: 0 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'vote_submitted' });

  const checkpoint = await stream.checkpoint('vote');
  assert.deepEqual(checkpoint, { visitId: 'visit-fast', throughSequence: 0, durable: true });
  assert.equal(store.records.size, 1);
  release();
  await stream.drain();
});

test('dead-letters a permanent poison batch and continues draining later rows', async () => {
  const store = memoryStore();
  const accepted = [];
  const warnings = [];
  const stream = createWeeklyTraceStream({
    store,
    uuid: uuids('visit-poison', 'batch-poison', 'visit-good', 'batch-good'),
    submitBatch: async payload => {
      if (payload.itemId === 'item-poison') throw Object.assign(new Error('invalid row'), { code: '22023' });
      accepted.push(payload.traceBatchId);
    },
    setTimer: () => 1,
    clearTimer: () => {},
    onWarning: warning => warnings.push(warning),
  });
  stream.startSession({ sessionId: 'session-poison', roundId: 'round-poison' });
  stream.startVisit({ itemId: 'item-poison', questionIndex: 0 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'question_start' });
  await stream.flush('navigation');
  stream.startVisit({ itemId: 'item-good', questionIndex: 1 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'question_start' });
  await stream.flush('navigation');

  assert.deepEqual(accepted, ['batch-good']);
  assert.equal(store.records.get('batch-poison').deadLetter.reason, 'permanent_submission_error');
  assert.deepEqual(await stream.queueStatus(), { queued: 0, deadLettered: 1 });
  assert.match(warnings.join(' '), /needs attention/);
});

test('ends visits independently so unsubmitted question exploration is retained', async () => {
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-a', 'batch-a', 'visit-b', 'batch-b'),
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
  });
  stream.startSession({ sessionId: 'session-3', roundId: 'round-3' });
  stream.startVisit({ itemId: 'item-a', questionIndex: 0 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'choice_selected' });
  await stream.endVisit('navigation');
  stream.startVisit({ itemId: 'item-b', questionIndex: 1 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'choice_rejected' });
  await stream.endVisit('vote');

  assert.deepEqual(submitted.map(batch => [batch.itemId, batch.reason]), [
    ['item-a', 'navigation'],
    ['item-b', 'vote'],
  ]);
  assert.notEqual(submitted[0].visitId, submitted[1].visitId);
});

test('strips participant identity fields from queued entries and app state', async () => {
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-4', 'batch-4'),
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
    getAppState: () => ({ display_name: 'Rafal', item_id: 'item-4' }),
  });
  stream.startSession({ sessionId: 'session-4', roundId: 'round-4' });
  stream.startVisit({ itemId: 'item-4', questionIndex: 4 });
  stream.recordEntry({
    seq: 0,
    kind: 'app',
    state: { participant_name: 'Rafal', selected_choice_id: 'choice-1' },
  });
  await stream.flush('visibility');

  const serialized = JSON.stringify(submitted[0]);
  assert.doesNotMatch(serialized, /Rafal/);
  assert.deepEqual(submitted[0].appState, { item_id: 'item-4' });
  assert.deepEqual(submitted[0].trace.entries[0].state, { selected_choice_id: 'choice-1' });
});

test('strips camelCase identity and dedicated comment text from every streamed location', async () => {
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-private', 'batch-private'),
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
    getAppState: () => ({ displayName: 'Rafal', voteComment: 'private note', item_id: 'item-private' }),
  });
  stream.startSession({ sessionId: 'session-private', roundId: 'round-private' });
  stream.startVisit({ itemId: 'item-private', questionIndex: 0 });
  stream.recordEntry({
    seq: 0,
    kind: 'app',
    state: { participantName: 'Rafal', vote_comment: 'private note', selected_choice_id: 'a' },
  });
  await stream.flush('vote');

  assert.doesNotMatch(JSON.stringify(submitted), /Rafal|private note/);
  assert.deepEqual(submitted[0].appState, { item_id: 'item-private' });
});

test('splits batches before exceeding the configured byte budget', async () => {
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-5', 'batch-5a', 'batch-5b'),
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
    maxTraceBytes: 900,
  });
  stream.startSession({ sessionId: 'session-5', roundId: 'round-5' });
  stream.startVisit({ itemId: 'item-5', questionIndex: 5 });
  stream.recordEntry({ seq: 0, kind: 'app', state: { text: 'a'.repeat(500) } });
  stream.recordEntry({ seq: 1, kind: 'app', state: { text: 'b'.repeat(500) } });
  await stream.flush('vote');

  assert.equal(submitted.length, 2);
  assert.equal(submitted[0].reason, 'byte_budget');
  assert.equal(submitted[1].reason, 'vote');
  assert.deepEqual(submitted.map(batch => batch.trace.entries.length), [1, 1]);
});

test('never places more than 500 entries in one trace batch', async () => {
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-6', 'batch-6a', 'batch-6b'),
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
  });
  stream.startSession({ sessionId: 'session-6', roundId: 'round-6' });
  stream.startVisit({ itemId: 'item-6', questionIndex: 6 });
  for (let sequence = 0; sequence < 501; sequence++) {
    stream.recordEntry({ seq: sequence, kind: 'app', action: 'camera_moved' });
  }
  await stream.flush('vote');

  assert.deepEqual(submitted.map(batch => batch.trace.entries.length), [500, 1]);
  assert.deepEqual(submitted.map(batch => [batch.firstSequence, batch.lastSequence]), [
    [0, 499], [500, 500],
  ]);
});

test('fails closed to a legacy vote snapshot on an unexplained sequence gap', async () => {
  const warnings = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-gap', 'batch-gap'),
    submitBatch: async () => {},
    setTimer: () => 1,
    clearTimer: () => {},
    onWarning: warning => warnings.push(warning),
  });
  stream.startSession({ sessionId: 'session-gap', roundId: 'round-gap' });
  stream.startVisit({ itemId: 'item-gap', questionIndex: 0 });
  assert.equal(stream.recordEntry({ seq: 0, kind: 'state', snapshot: {} }), true);
  assert.equal(stream.recordEntry({ seq: 2, kind: 'app', action: 'vote_submitted' }), false);
  assert.equal((await stream.checkpoint('vote')).durable, false);
  assert.match(warnings.join(' '), /discontinuous/);
});

test('visit ordering remains deterministic when wall-clock timestamps tie', async () => {
  const submitted = [];
  const stream = createWeeklyTraceStream({
    store: memoryStore(),
    uuid: uuids('visit-order-a', 'batch-order-a', 'visit-order-b', 'batch-order-b'),
    now: () => 5000,
    submitBatch: async payload => { submitted.push(payload); },
    setTimer: () => 1,
    clearTimer: () => {},
  });
  stream.startSession({ sessionId: 'session-order', roundId: 'round-order' });
  stream.startVisit({ itemId: 'item-a', questionIndex: 0 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'question_start' });
  await stream.endVisit('navigation');
  stream.startVisit({ itemId: 'item-b', questionIndex: 1 });
  stream.recordEntry({ seq: 0, kind: 'app', action: 'question_start' });
  await stream.endVisit('navigation');

  assert.deepEqual(submitted.map(batch => [
    batch.trace.visit_ordinal, batch.trace.visit_started_at,
  ]), [[0, 5000], [1, 5001]]);
});
