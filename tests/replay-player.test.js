import test from 'node:test';
import assert from 'node:assert/strict';
import { playViewerTrace } from '../replay-player.js';

function fakeReplayPlugin(calls, onState) {
  return {
    state: {
      async setSnapshot() {
        calls.push('state');
        onState?.();
      },
    },
    canvas3d: {
      camera: {
        setState() {
          calls.push('camera');
        },
      },
    },
  };
}

function fakeAsyncClock() {
  let time = 0;
  const waits = [];
  return {
    waits,
    advance: ms => { time += ms; },
    options: {
      now: () => time,
      sleep: async ms => {
        waits.push(ms);
        time += ms;
      },
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

const trace = {
  version: 1,
  molstar_version: '4.6.0',
  snapshots: [
    { t_ms: 0, kind: 'state', snapshot: { data: {} } },
    { t_ms: 100, kind: 'camera', camera: { zoom: 2 } },
  ],
};

test('applies state and camera entries in timestamp order', async () => {
  const calls = [];
  const plugin = fakeReplayPlugin(calls);
  const clock = fakeAsyncClock();

  await playViewerTrace(plugin, trace, clock.options);

  assert.deepEqual(calls, ['state', 'camera']);
  assert.deepEqual(clock.waits, [100]);
});

test('subtracts time spent restoring state from later waits', async () => {
  const calls = [];
  const clock = fakeAsyncClock();
  const plugin = fakeReplayPlugin(calls, () => clock.advance(40));

  await playViewerTrace(plugin, trace, clock.options);

  assert.deepEqual(clock.waits, [60]);
});

test('rejects unsupported traces before mutating the viewer', async () => {
  for (const invalidTrace of [
    { version: 2, molstar_version: '4.6.0', snapshots: [] },
    { version: 1, molstar_version: '4.5.0', snapshots: [] },
    {
      version: 1,
      molstar_version: '4.6.0',
      snapshots: [
        { t_ms: 10, kind: 'camera', camera: {} },
        { t_ms: 5, kind: 'camera', camera: {} },
      ],
    },
  ]) {
    const calls = [];
    await assert.rejects(
      playViewerTrace(fakeReplayPlugin(calls), invalidTrace),
      /Unsupported viewer trace/,
    );
    assert.deepEqual(calls, []);
  }
});

test('stops playback when aborted', async () => {
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(
    playViewerTrace(fakeReplayPlugin([]), trace, { signal: controller.signal }),
    error => error.name === 'AbortError' && /aborted/i.test(error.message),
  );
});

test('stops playback when aborted during the final state restore', async () => {
  const stateRestore = deferred();
  const stateStarted = deferred();
  const controller = new AbortController();
  const plugin = {
    state: {
      setSnapshot() {
        stateStarted.resolve();
        return stateRestore.promise;
      },
    },
    canvas3d: { camera: { setState() {} } },
  };
  const finalStateTrace = {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [{ t_ms: 0, kind: 'state', snapshot: { data: {} } }],
  };

  const playback = playViewerTrace(plugin, finalStateTrace, { signal: controller.signal });
  await stateStarted.promise;
  controller.abort();
  stateRestore.resolve();

  await assert.rejects(
    playback,
    error => error.name === 'AbortError' && /aborted/i.test(error.message),
  );
});
