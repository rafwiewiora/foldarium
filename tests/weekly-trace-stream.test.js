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
      visit_id: 'visit-1',
      entries: [
        { seq: 0, t_ms: 0, kind: 'app', action: 'question_start' },
        { seq: 1, t_ms: 100, kind: 'camera', camera: { radius: 12 } },
      ],
    },
    appState: { item_id: 'item-1', display_mode: 'grid' },
  });
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
  stream.recordEntry({ seq: 4, t_ms: 90, kind: 'app', action: 'choice_rejected' });
  await stream.flush('navigation');
  assert.equal(store.records.size, 1);

  failing = false;
  await stream.drain();
  assert.equal(store.records.size, 0);
  assert.equal(attempts.length, 2);
  assert.deepEqual(attempts[0], attempts[1]);
  assert.equal(attempts[1].traceBatchId, 'batch-2');
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
