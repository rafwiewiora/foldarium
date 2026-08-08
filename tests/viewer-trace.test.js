import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createViewerTraceRecorder, MAX_CAPTURE_BYTES } from '../viewer-trace.js';

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
  let focusCallback = null;
  let selectionCallback = null;
  let cameraReads = 0;
  let stateReads = 0;

  return {
    get cameraReads() {
      return cameraReads;
    },
    get stateReads() {
      return stateReads;
    },
    state: {
      getSnapshot: (params) => {
        stateReads += 1;
        return {
          data: { tree: { id: 'mock' } },
          structureFocus: { current: 'focused-residue' },
          structureSelection: params.structureSelection ? { entries: ['selected-residue'] } : undefined,
        };
      },
    },
    managers: {
      structure: {
        focus: {
          behaviors: {
            current: {
              subscribe(callback) {
                focusCallback = callback;
                return { unsubscribe: () => { focusCallback = null; } };
              },
            },
          },
        },
        selection: {
          events: {
            changed: {
              subscribe(callback) {
                selectionCallback = callback;
                return { unsubscribe: () => { selectionCallback = null; } };
              },
            },
          },
        },
      },
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
    focusChanged() {
      if (focusCallback) focusCallback();
    },
    selectionChanged() {
      if (selectionCallback) selectionCallback();
    },
  };
}

test('records focus and structure selection in state snapshots', () => {
  const recorder = createViewerTraceRecorder({ plugin: fakePlugin() });
  recorder.start();
  const trace = recorder.stop();
  const snapshot = trace.snapshots[0].snapshot;

  assert.deepEqual(snapshot.structureFocus, { current: 'focused-residue' });
  assert.deepEqual(snapshot.structureSelection, { entries: ['selected-residue'] });
});

test('captures debounced focus and selection changes after 100 ms', () => {
  const clock = fakeClock();
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({
    plugin,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  recorder.start();
  plugin.focusChanged();
  clock.advance(50);
  plugin.selectionChanged();
  clock.advance(99);
  assert.equal(plugin.stateReads, 1);
  clock.advance(1);
  assert.equal(plugin.stateReads, 2);

  const trace = recorder.stop();
  assert.deepEqual(trace.snapshots.map(entry => entry.kind), ['state', 'state']);
  assert.equal(trace.snapshots[1].t_ms, 150);
});

test('uses the Molstar stateChanged camera observable when changed is unavailable', () => {
  const plugin = fakePlugin();
  plugin.canvas3d.camera.stateChanged = plugin.canvas3d.camera.changed;
  delete plugin.canvas3d.camera.changed;

  let recorder;
  assert.doesNotThrow(() => {
    recorder = createViewerTraceRecorder({ plugin });
  });
  recorder.start();
  plugin.cameraChanged();
  const trace = recorder.stop();
  assert.deepEqual(trace.snapshots.map(entry => entry.kind), ['state', 'camera']);
});

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
  for (let index = 1; index < 100; index += 1) recorder.captureState();
  const trace = recorder.stop();
  assert.equal(trace.snapshots.length, 100);
  assert.equal(trace.truncated, true);
  assert.equal(plugin.stateReads, 100);

  recorder.captureState();
  plugin.cameraChanged();
  assert.equal(plugin.stateReads, 100);
  assert.equal(plugin.cameraReads, 0);
});

test('caps snapshots at 100 even when maxEntries exceeds 100', () => {
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({ plugin, maxEntries: 200 });
  recorder.start();
  for (let index = 0; index < 150; index += 1) recorder.captureState();
  const trace = recorder.stop();
  assert.equal(trace.snapshots.length, 100);
  assert.equal(trace.truncated, true);
});

test('caps snapshots at 100 when maxEntries is NaN', () => {
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({ plugin, maxEntries: NaN });
  recorder.start();
  for (let index = 0; index < 150; index += 1) recorder.captureState();
  const trace = recorder.stop();
  assert.equal(trace.snapshots.length, 100);
  assert.equal(trace.truncated, true);
});

test('caps snapshots at 100 when maxEntries is non-finite', () => {
  for (const maxEntries of [Infinity, -Infinity]) {
    const plugin = fakePlugin();
    const recorder = createViewerTraceRecorder({ plugin, maxEntries });
    recorder.start();
    for (let index = 0; index < 150; index += 1) recorder.captureState();
    const trace = recorder.stop();
    assert.equal(trace.snapshots.length, 100, `expected cap for maxEntries=${maxEntries}`);
    assert.equal(trace.truncated, true, `expected truncated for maxEntries=${maxEntries}`);
  }
});

test('normalizes negative maxEntries to zero snapshots', () => {
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({ plugin, maxEntries: -5 });
  recorder.start();
  const trace = recorder.stop();
  assert.equal(trace.snapshots.length, 0);
  assert.equal(trace.truncated, true);
});

test('state capture failures are skipped without throwing', () => {
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({ plugin });
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    plugin.state.getSnapshot = () => { throw new Error('snapshot failed'); };
    assert.doesNotThrow(() => recorder.start());
    assert.equal(recorder.stop().snapshots.length, 0);
    assert.deepEqual(warnings, [['Viewer snapshot skipped:', 'snapshot failed']]);
  } finally {
    console.warn = originalWarn;
  }
});

test('camera capture failures are skipped without throwing', () => {
  const clock = fakeClock();
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({
    plugin,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    settleMs: 300,
  });
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    recorder.start();
    plugin.canvas3d.camera.getSnapshot = () => { throw new Error('camera failed'); };
    plugin.cameraChanged();
    const trace = recorder.stop();
    assert.equal(trace.snapshots.length, 1);
    assert.equal(trace.snapshots[0].kind, 'state');
    assert.deepEqual(warnings, [['Viewer camera snapshot skipped:', 'camera failed']]);
  } finally {
    console.warn = originalWarn;
  }
});

test('deeply freezes the returned JSON trace', () => {
  const recorder = createViewerTraceRecorder({ plugin: fakePlugin() });
  recorder.start();
  const trace = recorder.stop();

  assert.equal(Object.isFrozen(trace), true);
  assert.equal(Object.isFrozen(trace.snapshots), true);
  assert.equal(Object.isFrozen(trace.snapshots[0]), true);
  assert.equal(Object.isFrozen(trace.snapshots[0].snapshot), true);
  assert.equal(Object.isFrozen(trace.snapshots[0].snapshot.data), true);
  assert.equal(Object.isFrozen(trace.snapshots[0].snapshot.data.tree), true);
  assert.throws(() => { trace.snapshots[0].snapshot.data.tree.id = 'mutated'; }, TypeError);
  assert.throws(() => { trace.snapshots.push({}); }, TypeError);
});

test('records compact semantic app state and active pane attribution without changing v1', () => {
  const clock = fakeClock();
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({
    plugin,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  recorder.start({ appState: { display_mode: 'grid', ignored: () => 'nope' } });
  clock.advance(10);
  recorder.setActivePane('pose-B', 'pointerenter');
  clock.advance(5);
  recorder.recordAppEvent('hbonds_toggled', {
    display_mode: 'grid',
    show_hbonds: true,
    cyclic: null,
  });
  recorder.captureCamera({ position: [1, 2, 3] }, { sourcePaneId: 'pose-B' });

  const trace = recorder.stop({ appState: { display_mode: 'grid', show_hbonds: true } });
  assert.equal(trace.version, 1);
  assert.deepEqual(trace.app_state, { display_mode: 'grid', show_hbonds: true });
  assert.deepEqual(trace.app_trace.map(event => event.kind), [
    'app', 'active_pane', 'app',
  ]);
  assert.equal(trace.app_trace[0].action, 'question_start');
  assert.deepEqual(trace.app_trace[0].state, { display_mode: 'grid' });
  assert.equal(trace.app_trace[2].active_pane_id, 'pose-B');
  assert.equal(trace.snapshots.at(-1).source_pane_id, 'pose-B');
});

test('snapshot is non-stopping and returns immutable history for a retryable vote', () => {
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({ plugin });
  recorder.start();

  const pendingVoteTrace = recorder.snapshot({ question_index: 2 });
  recorder.recordAppEvent('vote_retry', { question_index: 2 });
  const completedTrace = recorder.stop();

  assert.equal(pendingVoteTrace.snapshots.length, 1);
  assert.equal(pendingVoteTrace.app_trace, undefined);
  assert.deepEqual(pendingVoteTrace.app_state, { question_index: 2 });
  assert.equal(completedTrace.app_trace[0].action, 'vote_retry');
  assert.equal(Object.isFrozen(pendingVoteTrace), true);
});

test('attaches pane focus and selection capture with interaction attribution and disposal', () => {
  const clock = fakeClock();
  const canonical = fakePlugin();
  const pane = fakePlugin();
  const listeners = new Map();
  const removed = [];
  const element = {
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { removed.push(name); listeners.delete(name); },
  };
  const recorder = createViewerTraceRecorder({
    plugin: canonical,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  const detach = recorder.attachPane({ plugin: pane, paneId: 'pose-C', element });
  recorder.start();

  listeners.get('pointerenter')({ type: 'pointerenter' });
  pane.focusChanged();
  clock.advance(100);
  const trace = recorder.snapshot();
  assert.equal(trace.app_trace[0].pane_id, 'pose-C');
  assert.equal(trace.snapshots.at(-1).scope, 'pane');
  assert.equal(trace.snapshots.at(-1).source_pane_id, 'pose-C');

  detach();
  pane.selectionChanged();
  clock.advance(100);
  assert.equal(recorder.stop().snapshots.length, 2);
  assert.deepEqual(removed.sort(), [
    'focusin', 'pointerdown', 'pointerenter', 'touchstart', 'wheel',
  ]);
});

test('compacts oversized traces below the 480 KiB persistence budget', () => {
  const plugin = fakePlugin();
  plugin.state.getSnapshot = () => ({ data: { text: 'x'.repeat(MAX_CAPTURE_BYTES) } });
  const recorder = createViewerTraceRecorder({ plugin });
  recorder.start();
  for (let index = 0; index < 50; index += 1) {
    recorder.recordAppEvent('view_changed', { index, label: 'é'.repeat(500) });
  }

  const trace = recorder.stop();
  const size = new TextEncoder().encode(JSON.stringify(trace)).byteLength;
  assert.ok(size < MAX_CAPTURE_BYTES, `expected ${size} bytes below ${MAX_CAPTURE_BYTES}`);
  assert.equal(trace.truncated, true);
  assert.equal(trace.byte_compacted, true);
});

test('captures a bounded non-stopping suggestion context and omits an oversized viewer state', () => {
  const plugin = fakePlugin();
  plugin.state.getSnapshot = () => ({ data: { text: 'x'.repeat(140 * 1024) } });
  const recorder = createViewerTraceRecorder({ plugin });
  recorder.start();
  recorder.recordAppEvent('display_mode_changed', { display_mode: 'grid' });

  const context = recorder.captureContext({ display_mode: 'grid', active_pane_id: 'pose-A' });
  assert.deepEqual(context.app_state, { display_mode: 'grid', active_pane_id: 'pose-A' });
  assert.equal(context.viewer_snapshot.viewer_state, null);
  assert.equal(context.viewer_snapshot.viewer_state_omitted, 'byte_budget');
  assert.deepEqual(context.viewer_snapshot.shared_camera, { position: [0, 0, 0] });
  assert.equal(context.viewer_trace_tail.version, 1);
  assert.ok(new TextEncoder().encode(JSON.stringify(context.viewer_snapshot)).byteLength < 128 * 1024);
  assert.ok(new TextEncoder().encode(JSON.stringify(context.viewer_trace_tail)).byteLength < 128 * 1024);
  assert.ok(new TextEncoder().encode(JSON.stringify(context)).byteLength < MAX_CAPTURE_BYTES);

  recorder.recordAppEvent('suggestion_saved', { display_mode: 'grid' });
  assert.equal(recorder.stop().app_trace.at(-1).action, 'suggestion_saved');
});

test('recorder import failure does not block quiz application startup', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const recorderImport = html.indexOf("await import('./viewer-trace.js')");
  const warning = html.indexOf("console.warn('Viewer recording disabled:'", recorderImport);
  const appLoad = html.indexOf("await loadScript('app.js')", recorderImport);

  assert.notEqual(recorderImport, -1);
  assert.ok(warning > recorderImport);
  assert.ok(appLoad > warning);
});

test('quiz records only pre-reveal viewer interactions and keeps the local log lean', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');

  assert.match(app, /if \(!DEV && typeof window\.createViewerTraceRecorder === 'function'\)/);
  assert.match(app, /await viewerRebuild\.enqueue\([\s\S]*?viewerTraceRecorder\?\.start\(\{ appState: currentReplayableAppState\(\) \}\);/);
  assert.match(app, /await pinCameraSnapshot\(plugin, preservedCamera\);\s*viewerTraceRecorder\?\.captureState\(\);/);
  assert.match(app, /const viewerTrace = viewerTraceRecorder\?\.stop\(\{ appState: currentReplayableAppState\(\) \}\) \?\? null;[\s\S]*?await viewerRebuild\.enqueue\([\s\S]*?cur\.revealed = true/);
  assert.match(app, /function logAnswer\(picked, af3, viewerTrace\)/);
  assert.match(app, /log\.push\(rec\);[\s\S]*?recordAnswer\(remoteSessionId, idx, \{ \.\.\.rec, viewer_trace: viewerTrace \}\)/);

  const localRecord = app.slice(app.indexOf('function logAnswer('), app.indexOf('const log =', app.indexOf('function logAnswer(')));
  assert.doesNotMatch(localRecord, /viewer_trace/);
});
