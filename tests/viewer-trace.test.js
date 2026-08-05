import test from 'node:test';
import assert from 'node:assert/strict';
import { createViewerTraceRecorder } from '../viewer-trace.js';

function fakeClock() {
  let time = 0;
  let nextId = 1;
  const timers = new Map();

  return {
    now: () => time,
    setTimer: (fn, ms) => {
      const id = nextId++;
      timers.set(id, { fn, at: time + ms });
      return id;
    },
    clearTimer: (id) => {
      timers.delete(id);
    },
    advance(ms) {
      time += ms;
      const due = [...timers.entries()]
        .filter(([, entry]) => entry.at <= time)
        .sort((a, b) => a[1].at - b[1].at);
      for (const [id, entry] of due) {
        timers.delete(id);
        entry.fn();
      }
    },
  };
}

function fakePlugin() {
  let cameraCallback = null;
  let cameraReads = 0;

  return {
    get cameraReads() {
      return cameraReads;
    },
    state: {
      getSnapshot: (_params) => ({
        data: { tree: 'mock' },
        structureFocus: 'remove-me',
      }),
    },
    canvas3d: {
      camera: {
        getSnapshot: () => {
          cameraReads += 1;
          return { position: [0, 0, 0] };
        },
        changed: {
          subscribe(callback) {
            cameraCallback = callback;
            return { unsubscribe: () => { cameraCallback = null; } };
          },
        },
      },
    },
    cameraChanged() {
      if (cameraCallback) cameraCallback();
    },
  };
}

test('captures an initial data-tree snapshot and a settled camera endpoint', () => {
  const clock = fakeClock();
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({
    plugin,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    settleMs: 300,
  });

  recorder.start();
  clock.advance(100);
  plugin.cameraChanged();
  clock.advance(299);
  assert.equal(plugin.cameraReads, 0);
  clock.advance(1);
  assert.equal(plugin.cameraReads, 1);

  const trace = recorder.stop();
  assert.equal(trace.snapshots[0].kind, 'state');
  assert.ok(trace.snapshots[0].snapshot.data);
  assert.equal(trace.snapshots[1].kind, 'camera');
  assert.equal(trace.snapshots[1].t_ms, 400);
});

test('captureState records rebuilt scenes and stop prevents later captures', () => {
  const clock = fakeClock();
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({
    plugin,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    settleMs: 300,
  });

  recorder.start();
  recorder.captureState();
  plugin.cameraChanged();
  const trace = recorder.stop();
  plugin.cameraChanged();
  clock.advance(300);
  assert.deepEqual(trace.snapshots.map(entry => entry.kind), ['state', 'state', 'camera']);
});

test('marks the trace truncated at 100 entries', () => {
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({ plugin, maxEntries: 100 });
  recorder.start();
  for (let index = 0; index < 150; index += 1) recorder.captureState();
  const trace = recorder.stop();
  assert.equal(trace.snapshots.length, 100);
  assert.equal(trace.truncated, true);
});

test('capture failures are skipped without throwing', () => {
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({ plugin });
  plugin.state.getSnapshot = () => { throw new Error('snapshot failed'); };
  assert.doesNotThrow(() => recorder.start());
  assert.equal(recorder.stop().snapshots.length, 0);
});
