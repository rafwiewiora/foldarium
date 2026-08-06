import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  createRevealAfterIdle,
  createViewerRebuildCoordinator,
  waitForCameraSettled,
} from '../viewer-rebuild-coordinator.js';

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('serializes each state mutation with its rebuild and capture', async () => {
  const releases = [deferred(), deferred()];
  const starts = [deferred(), deferred()];
  const events = [];
  let rebuildIndex = 0;
  let activeRebuilds = 0;
  const coordinator = createViewerRebuildCoordinator({
    async rebuild() {
      const index = rebuildIndex++;
      activeRebuilds += 1;
      assert.equal(activeRebuilds, 1);
      events.push(`rebuild-${index + 1}:start`);
      starts[index].resolve();
      await releases[index].promise;
      events.push(`rebuild-${index + 1}:capture`);
      activeRebuilds -= 1;
    },
    setBusy: busy => events.push(`busy:${busy}`),
  });

  const first = coordinator.enqueue(() => { events.push('mutate-1'); });
  const second = coordinator.enqueue(() => { events.push('mutate-2'); });

  await starts[0].promise;
  assert.deepEqual(events, ['busy:true', 'mutate-1', 'rebuild-1:start']);
  releases[0].resolve();
  await starts[1].promise;
  assert.deepEqual(events, [
    'busy:true',
    'mutate-1',
    'rebuild-1:start',
    'rebuild-1:capture',
    'mutate-2',
    'rebuild-2:start',
  ]);
  releases[1].resolve();

  await Promise.all([first, second]);
  assert.deepEqual(events, [
    'busy:true',
    'mutate-1',
    'rebuild-1:start',
    'rebuild-1:capture',
    'mutate-2',
    'rebuild-2:start',
    'rebuild-2:capture',
    'busy:false',
  ]);
  assert.equal(coordinator.pending, false);
});

test('the original reveal click drains work queued while it waits', async () => {
  const releases = [deferred(), deferred()];
  const starts = [deferred(), deferred()];
  const events = [];
  let rebuildIndex = 0;
  const coordinator = createViewerRebuildCoordinator({
    async rebuild() {
      const index = rebuildIndex++;
      events.push(`rebuild-${index + 1}:start`);
      starts[index].resolve();
      await releases[index].promise;
      events.push(`rebuild-${index + 1}:capture`);
    },
  });
  const reveal = createRevealAfterIdle({
    coordinator,
    reveal() {
      events.push('trace:stop');
      events.push('answer:reveal');
    },
  });

  const first = coordinator.enqueue(() => { events.push('mutate-1'); });
  await starts[0].promise;
  const originalClick = reveal();
  const second = coordinator.enqueue(() => { events.push('mutate-2'); });

  releases[0].resolve();
  await starts[1].promise;
  assert.doesNotMatch(events.join(','), /trace:stop/);
  releases[1].resolve();
  await Promise.all([first, second, originalClick]);

  assert.deepEqual(events, [
    'mutate-1',
    'rebuild-1:start',
    'rebuild-1:capture',
    'mutate-2',
    'rebuild-2:start',
    'rebuild-2:capture',
    'trace:stop',
    'answer:reveal',
  ]);
});

test('serializes question rebuild and finalization before starting the next question', async () => {
  const releases = [deferred(), deferred()];
  const starts = [deferred(), deferred()];
  const events = [];
  let currentQuestion = null;
  let rebuildIndex = 0;
  const coordinator = createViewerRebuildCoordinator({
    async rebuild() {
      const index = rebuildIndex++;
      events.push(`question-${currentQuestion}:rebuild`);
      starts[index].resolve();
      await releases[index].promise;
    },
  });

  const first = coordinator.enqueue(
    () => { currentQuestion = 1; },
    () => { events.push(`question-${currentQuestion}:recorder-start`); },
  );
  const second = coordinator.enqueue(
    () => { currentQuestion = 2; },
    () => { events.push(`question-${currentQuestion}:recorder-start`); },
  );

  await starts[0].promise;
  releases[0].resolve();
  await starts[1].promise;
  assert.deepEqual(events, [
    'question-1:rebuild',
    'question-1:recorder-start',
    'question-2:rebuild',
  ]);

  releases[1].resolve();
  await Promise.all([first, second]);
  assert.deepEqual(events, [
    'question-1:rebuild',
    'question-1:recorder-start',
    'question-2:rebuild',
    'question-2:recorder-start',
  ]);
});

test('waits for a quiet camera after reset before resolving', async () => {
  let cameraChanged;
  let nextTimer = 0;
  const timers = new Map();
  const events = [];
  const settled = waitForCameraSettled({
    cameraChanged: {
      subscribe(callback) {
        cameraChanged = callback;
        return { unsubscribe: () => events.push('unsubscribe') };
      },
    },
    requestReset() {
      events.push('reset');
      cameraChanged();
    },
    settleMs: 300,
    setTimer(callback) {
      const id = ++nextTimer;
      timers.set(id, callback);
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
  }).then(() => events.push('settled'));

  assert.deepEqual(events, ['reset']);
  cameraChanged();
  assert.equal(timers.size, 1);
  [...timers.values()][0]();
  await settled;
  assert.deepEqual(events, ['reset', 'unsubscribe', 'settled']);
});

test('quiz routes every pre-answer viewer mutation through the coordinator', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const initStart = app.indexOf('async function init()');
  const onePosePick = app.slice(
    app.indexOf('async function onPick('),
    app.indexOf('async function reveal()'),
  );
  const controls = app.slice(
    app.indexOf("document.querySelectorAll('#mode button').forEach", initStart),
    app.indexOf("$('#lock').onclick"),
  );
  const keyboard = app.slice(
    app.indexOf("document.addEventListener('keydown'", initStart),
    app.indexOf('if (!POOLS.cameo.length'),
  );

  assert.match(app, /revealAfterIdle = window\.createRevealAfterIdle\(/);
  assert.match(app, /async function reveal\(\)[\s\S]*?await revealAfterIdle\(\);/);
  assert.equal((onePosePick.match(/viewerRebuild\.enqueue\(/g) || []).length, 1);
  assert.equal((controls.match(/viewerRebuild\.enqueue\(/g) || []).length, 4);
  assert.equal((keyboard.match(/viewerRebuild\.enqueue\(/g) || []).length, 2);
  assert.doesNotMatch(onePosePick + controls + keyboard, /onePoseRebuild|await buildLayer\(\)/);
});
