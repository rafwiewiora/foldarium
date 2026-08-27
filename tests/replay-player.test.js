import test from 'node:test';
import assert from 'node:assert/strict';
import { playViewerTrace, validateViewerTrace } from '../replay-player.js';

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
        getSnapshot() {
          return { position: 'current' };
        },
        setState(_camera, duration) {
          calls.push(duration === 0 ? 'camera:pin' : 'camera');
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
  assert.deepEqual(clock.waits, [100, 250]);
});

test('subtracts time spent restoring state from later waits', async () => {
  const calls = [];
  const clock = fakeAsyncClock();
  const plugin = fakeReplayPlugin(calls, () => clock.advance(40));

  await playViewerTrace(plugin, trace, clock.options);

  assert.deepEqual(clock.waits, [60, 250]);
});

test('keeps playback active through an animated camera embedded in a state snapshot', async () => {
  const calls = [];
  const clock = fakeAsyncClock();
  const plugin = fakeReplayPlugin(calls);
  const stateCameraTrace = {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [{
      t_ms: 0,
      kind: 'state',
      snapshot: {
        data: {},
        camera: {
          transitionStyle: 'animate',
          transitionDurationInMs: 400,
        },
      },
    }],
  };

  await playViewerTrace(plugin, stateCameraTrace, clock.options);

  assert.deepEqual(calls, ['state']);
  assert.deepEqual(clock.waits, [400]);
});

test('pins an animated state-snapshot camera when aborted after restoration', async () => {
  const controller = new AbortController();
  const calls = [];
  let transitionStarted = false;
  const plugin = fakeReplayPlugin(calls);
  const stateCameraTrace = {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [{
      t_ms: 0,
      kind: 'state',
      snapshot: {
        data: {},
        camera: {
          transitionStyle: 'animate',
          transitionDurationInMs: 400,
        },
      },
    }],
  };
  const playback = playViewerTrace(plugin, stateCameraTrace, {
    signal: controller.signal,
    now: () => 0,
    sleep: (_ms, signal) => new Promise((_resolve, reject) => {
      transitionStarted = true;
      signal.addEventListener('abort', () => {
        reject(new DOMException('Viewer replay aborted', 'AbortError'));
      }, { once: true });
    }),
  });

  await new Promise(resolve => setImmediate(resolve));
  assert.equal(transitionStarted, true);
  controller.abort();

  await assert.rejects(playback, error => error.name === 'AbortError');
  assert.deepEqual(calls, ['state', 'camera:pin']);
});

test('does not delay later entries while keeping the final camera transition active', async () => {
  const calls = [];
  const clock = fakeAsyncClock();
  const plugin = fakeReplayPlugin(calls);
  const overlappingTrace = {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [
      { t_ms: 0, kind: 'state', snapshot: { data: {} } },
      { t_ms: 100, kind: 'camera', camera: { zoom: 2 } },
      { t_ms: 150, kind: 'state', snapshot: { data: { later: true } } },
    ],
  };

  await playViewerTrace(plugin, overlappingTrace, clock.options);

  assert.deepEqual(calls, ['state', 'camera', 'state']);
  assert.deepEqual(clock.waits, [100, 50, 200]);
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

test('pins an animated state camera when aborted before pending restoration resolves', async () => {
  const stateRestore = deferred();
  const stateStarted = deferred();
  const controller = new AbortController();
  const calls = [];
  let cameraMoving = false;
  const plugin = {
    state: {
      setSnapshot() {
        calls.push('state');
        stateStarted.resolve();
        return stateRestore.promise.then(() => {
          cameraMoving = true;
        });
      },
    },
    canvas3d: {
      camera: {
        getSnapshot() {
          return { position: 'mid-transition' };
        },
        setState(_camera, duration) {
          if (duration === 0) {
            calls.push('camera:pin');
            cameraMoving = false;
          }
        },
      },
    },
  };
  const animatedStateTrace = {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [{
      t_ms: 0,
      kind: 'state',
      snapshot: {
        data: {},
        camera: {
          transitionStyle: 'animate',
          transitionDurationInMs: 400,
        },
      },
    }],
  };

  const playback = playViewerTrace(plugin, animatedStateTrace, {
    signal: controller.signal,
    now: () => 0,
  });
  await stateStarted.promise;
  controller.abort();
  stateRestore.resolve();

  await assert.rejects(playback, error => error.name === 'AbortError');
  assert.deepEqual(calls, ['state', 'camera:pin']);
  assert.equal(cameraMoving, false);
});

test('keeps camera-only playback active and pins the camera when aborted mid-transition', async () => {
  const controller = new AbortController();
  const calls = [];
  let transitionStarted = false;
  const plugin = fakeReplayPlugin(calls);
  const cameraTrace = {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [{ t_ms: 0, kind: 'camera', camera: { position: 'target' } }],
  };
  const playback = playViewerTrace(plugin, cameraTrace, {
    signal: controller.signal,
    now: () => 0,
    sleep: (_ms, signal) => new Promise((_resolve, reject) => {
      transitionStarted = true;
      signal.addEventListener('abort', () => {
        reject(new DOMException('Viewer replay aborted', 'AbortError'));
      }, { once: true });
    }),
  });

  await new Promise(resolve => setImmediate(resolve));
  assert.equal(transitionStarted, true);
  assert.deepEqual(calls, ['camera']);
  controller.abort();

  await assert.rejects(playback, error => error.name === 'AbortError');
  assert.deepEqual(calls, ['camera', 'camera:pin']);
});

test('replays semantic app state and active-pane callbacks alongside old viewer entries', async () => {
  const calls = [];
  const clock = fakeAsyncClock();
  const plugin = fakeReplayPlugin(calls);
  const semanticTrace = {
    version: 1,
    molstar_version: '4.6.0',
    app_state: { display_mode: 'all' },
    snapshots: [
      { t_ms: 0, seq: 0, kind: 'state', snapshot: { data: {} } },
      { t_ms: 100, seq: 3, kind: 'camera', camera: {}, source_pane_id: 'pose-B' },
    ],
    app_trace: [
      { t_ms: 50, seq: 1, kind: 'app', action: 'display_mode_changed', state: { display_mode: 'grid' } },
      { t_ms: 75, seq: 2, kind: 'active_pane', pane_id: 'pose-B' },
    ],
  };

  await playViewerTrace(plugin, semanticTrace, {
    ...clock.options,
    onAppEvent: event => calls.push(`app:${event.action || event.kind}`),
    onAppStateChange: state => calls.push(`mode:${state.display_mode}`),
    onActivePaneChange: paneId => calls.push(`pane:${paneId}`),
  });

  assert.deepEqual(calls, [
    'mode:all',
    'state',
    'app:display_mode_changed',
    'mode:grid',
    'app:active_pane',
    'pane:pose-B',
    'camera',
    'pane:pose-B',
  ]);
  assert.deepEqual(clock.waits, [50, 25, 25, 250]);
});

test('keeps legacy v1 traces valid and rejects malformed semantic extensions', () => {
  assert.equal(validateViewerTrace(trace), trace);
  for (const invalid of [
    { ...trace, app_trace: {} },
    { ...trace, app_trace: [{ t_ms: 0, kind: 'app', action: '' }] },
    { ...trace, app_trace: [{ t_ms: 0, kind: 'active_pane', pane_id: 2 }] },
    { ...trace, app_state: [] },
  ]) {
    assert.throws(() => validateViewerTrace(invalid), /Unsupported viewer trace/);
  }
});
