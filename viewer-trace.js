const SNAPSHOT_PARAMS = {
  data: true,
  behavior: false,
  componentManager: false,
  animation: false,
  startAnimation: false,
  canvas3d: false,
  canvas3dContext: false,
  interactivity: false,
  structureSelection: false,
  camera: true,
  cameraTransition: { name: 'animate', params: { durationInMs: 250 } },
};

const MAX_SNAPSHOTS = 100;

function normalizeEntryLimit(maxEntries) {
  const numeric = Number(maxEntries);
  if (!Number.isFinite(numeric)) return MAX_SNAPSHOTS;
  return Math.min(MAX_SNAPSHOTS, Math.max(0, Math.floor(numeric)));
}

function deepFreeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) deepFreeze(child, seen);
  return Object.freeze(value);
}

export function createViewerTraceRecorder({
  plugin,
  now = () => performance.now(),
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  settleMs = 300,
  maxEntries = MAX_SNAPSHOTS,
}) {
  const entryLimit = normalizeEntryLimit(maxEntries);
  let active = false;
  let startedAt = 0;
  let snapshots = [];
  let truncated = false;
  let cameraTimer = null;

  const stopCaptureWork = () => {
    active = false;
    if (cameraTimer !== null) clearTimer(cameraTimer);
    cameraTimer = null;
  };

  const append = entry => {
    if (!active) return;
    if (snapshots.length >= entryLimit) {
      truncated = true;
      stopCaptureWork();
      return;
    }
    snapshots.push({ t_ms: Math.max(0, Math.round(now() - startedAt)), ...entry });
    if (snapshots.length === entryLimit) {
      truncated = true;
      stopCaptureWork();
    }
  };

  const captureState = () => {
    if (!active) return;
    try {
      const snapshot = plugin.state.getSnapshot(SNAPSHOT_PARAMS);
      delete snapshot.structureFocus;
      append({ kind: 'state', snapshot });
    } catch (error) {
      console.warn('Viewer snapshot skipped:', error.message);
    }
  };

  const captureCamera = () => {
    if (!active) return;
    try {
      append({ kind: 'camera', camera: plugin.canvas3d.camera.getSnapshot() });
    } catch (error) {
      console.warn('Viewer camera snapshot skipped:', error.message);
    }
  };

  const cameraChanges = plugin.canvas3d.camera.changed ?? plugin.canvas3d.camera.stateChanged;
  const cameraSubscription = cameraChanges?.subscribe(() => {
    if (!active) return;
    if (cameraTimer !== null) clearTimer(cameraTimer);
    cameraTimer = setTimer(() => {
      cameraTimer = null;
      captureCamera();
    }, settleMs);
  }) ?? { unsubscribe() {} };

  return {
    start() {
      active = entryLimit > 0;
      startedAt = now();
      snapshots = [];
      truncated = entryLimit === 0;
      if (cameraTimer !== null) clearTimer(cameraTimer);
      cameraTimer = null;
      captureState();
    },
    captureState,
    stop() {
      if (cameraTimer !== null) {
        clearTimer(cameraTimer);
        cameraTimer = null;
        captureCamera();
      }
      active = false;
      return deepFreeze({
        version: 1,
        molstar_version: '4.6.0',
        duration_ms: Math.max(0, Math.round(now() - startedAt)),
        truncated,
        snapshots: snapshots.slice(),
      });
    },
    dispose() {
      cameraSubscription.unsubscribe();
    },
  };
}
