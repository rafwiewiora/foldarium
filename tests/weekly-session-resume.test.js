import test from 'node:test';
import assert from 'node:assert/strict';
import { createWeeklySessionResumeStore } from '../weekly-session-resume.js';

function memoryStorage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    raw: key => values.get(key),
  };
}

test('same-tab weekly resume stores only opaque identity and question position', () => {
  const storage = memoryStorage();
  const store = createWeeklySessionResumeStore(storage);
  store.save({ sessionId: 'session-1', roundId: 'round-1', questionIndex: 7 });

  assert.deepEqual(store.read(), {
    version: 2,
    session_id: 'session-1',
    round_id: 'round-1',
    question_index: 7,
    phase: 'blind',
  });
  assert.equal(/rafal|display|name/i.test(storage.raw('foldariumWeeklyResumeV1')), false);
});

test('post-reveal resume tokens remain explicitly separated from blind sessions', () => {
  const storage = memoryStorage();
  const store = createWeeklySessionResumeStore(storage);
  store.save({
    sessionId: 'session-2',
    roundId: 'round-1',
    questionIndex: 3,
    phase: 'post_reveal',
  });
  assert.equal(store.read().phase, 'post_reveal');

  storage.setItem('foldariumWeeklyResumeV1', JSON.stringify({
    version: 1,
    session_id: 'legacy-session',
    round_id: 'round-1',
    question_index: 1,
  }));
  assert.equal(store.read().phase, 'blind');
});

test('malformed or cleared weekly resume tokens fail closed', () => {
  const storage = memoryStorage();
  const store = createWeeklySessionResumeStore(storage);
  storage.setItem('foldariumWeeklyResumeV1', '{bad json');
  assert.equal(store.read(), null);
  storage.setItem('foldariumWeeklyResumeV1', JSON.stringify({
    version: 1, session_id: 'session-1', round_id: 'round-1', question_index: -1,
  }));
  assert.equal(store.read(), null);
  store.clear();
  assert.equal(store.hasToken(), false);
});
