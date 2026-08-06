import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
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
  assert.match(app, /await viewerRebuild\.enqueue\([\s\S]*?viewerTraceRecorder\?\.start\(\);/);
  assert.match(app, /restoreCam\(\);\s*viewerTraceRecorder\?\.captureState\(\);/);
  assert.match(app, /const viewerTrace = viewerTraceRecorder\?\.stop\(\) \?\? null;\s*cur\.revealed = true/);
  assert.match(app, /function logAnswer\(picked, af3, viewerTrace\)/);
  assert.match(app, /log\.push\(rec\);[\s\S]*?recordAnswer\(remoteSessionId, idx, \{ \.\.\.rec, viewer_trace: viewerTrace \}\)/);

  const localRecord = app.slice(app.indexOf('function logAnswer('), app.indexOf('const log =', app.indexOf('function logAnswer(')));
  assert.doesNotMatch(localRecord, /viewer_trace/);
});
