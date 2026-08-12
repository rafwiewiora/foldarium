const SNAPSHOT_PARAMS = {
  data: true,
  behavior: false,
  componentManager: false,
  animation: false,
  startAnimation: false,
  canvas3d: false,
  canvas3dContext: false,
  interactivity: false,
  structureSelection: true,
  camera: true,
  cameraTransition: { name: 'animate', params: { durationInMs: 250 } },
};

const MAX_SNAPSHOTS = 100;
const MAX_APP_EVENTS = 200;
export const MAX_CAPTURE_BYTES = 480 * 1024;
const MAX_CONTEXT_TAIL_ENTRIES = 24;
const MAX_APP_STATE_DEPTH = 4;
const MAX_APP_STATE_KEYS = 32;
const MAX_APP_STATE_ARRAY = 24;
const MAX_APP_STATE_STRING = 256;
const MAX_APP_STATE_BYTES = 60 * 1024;
const MAX_SUGGESTION_COMPONENT_BYTES = 120 * 1024;

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

function serializedBytes(value) {
  try {
    const serialized = JSON.stringify(value);
    return serialized === undefined ? Infinity : new TextEncoder().encode(serialized).byteLength;
  } catch {
    return Infinity;
  }
}

function compactString(value, limit = MAX_APP_STATE_STRING) {
  if (typeof value !== 'string') return undefined;
  return value.slice(0, limit);
}

function compactValue(value, depth, seen) {
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'string') return compactString(value);
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined;
  if (depth >= MAX_APP_STATE_DEPTH || typeof value !== 'object' || seen.has(value)) {
    return undefined;
  }

  seen.add(value);
  let compacted;
  if (Array.isArray(value)) {
    compacted = [];
    for (const child of value.slice(0, MAX_APP_STATE_ARRAY)) {
      const normalized = compactValue(child, depth + 1, seen);
      if (normalized !== undefined) compacted.push(normalized);
    }
  } else {
    compacted = {};
    let keyCount = 0;
    try {
      for (const [rawKey, child] of Object.entries(value)) {
        if (keyCount >= MAX_APP_STATE_KEYS) break;
        const key = compactString(rawKey, 64);
        const normalized = compactValue(child, depth + 1, seen);
        if (key && normalized !== undefined) {
          compacted[key] = normalized;
          keyCount += 1;
        }
      }
    } catch {
      compacted = {};
    }
  }
  seen.delete(value);
  return compacted;
}

export function compactAppState(value) {
  if (value === undefined) return undefined;
  const compacted = compactValue(value, 0, new WeakSet());
  return compacted !== null
    && typeof compacted === 'object'
    && !Array.isArray(compacted)
    && serializedBytes(compacted) < MAX_APP_STATE_BYTES
    ? compacted
    : undefined;
}

function normalizePaneId(value) {
  if (value === null || value === undefined || value === '') return null;
  return compactString(String(value), 80);
}

function traceEnvelope({ snapshots, appTrace, durationMs, truncated, byteCompacted, appState }) {
  const trace = {
    version: 1,
    molstar_version: '4.6.0',
    duration_ms: durationMs,
    truncated,
    snapshots,
  };
  if (appTrace.length) trace.app_trace = appTrace;
  if (byteCompacted) trace.byte_compacted = true;
  if (appState !== undefined) trace.app_state = appState;
  return trace;
}

function removeOldestIntermediateEntry(snapshots, appTrace) {
  const candidates = [];
  const finalSnapshotIndex = snapshots.length - 1;
  for (let index = 0; index < snapshots.length; index += 1) {
    const entry = snapshots[index];
    const initialState = index === 0 && entry.kind === 'state';
    if (!initialState && index !== finalSnapshotIndex) {
      candidates.push({ collection: snapshots, index, tMs: entry.t_ms, priority: 0 });
    }
  }
  const finalAppIndex = appTrace.length - 1;
  for (let index = 0; index < appTrace.length; index += 1) {
    if (index !== finalAppIndex) {
      candidates.push({ collection: appTrace, index, tMs: appTrace[index].t_ms, priority: 1 });
    }
  }
  candidates.sort((left, right) => left.priority - right.priority || left.tMs - right.tMs);
  if (!candidates.length) return false;
  const selected = candidates[0];
  selected.collection.splice(selected.index, 1);
  return true;
}

function fitTraceToBudget(trace, maxBytes = MAX_CAPTURE_BYTES - 1) {
  while (serializedBytes(trace) > maxBytes) {
    if (removeOldestIntermediateEntry(trace.snapshots, trace.app_trace ?? [])) {
      trace.truncated = true;
      trace.byte_compacted = true;
      continue;
    }
    if (trace.app_state !== undefined) {
      delete trace.app_state;
      trace.truncated = true;
      trace.byte_compacted = true;
      continue;
    }
    if ((trace.app_trace?.length ?? 0) > 0) {
      trace.app_trace.pop();
      if (!trace.app_trace.length) delete trace.app_trace;
      trace.truncated = true;
      trace.byte_compacted = true;
      continue;
    }
    if (trace.snapshots.length > 1) {
      trace.snapshots.pop();
      trace.truncated = true;
      trace.byte_compacted = true;
      continue;
    }
    if (trace.snapshots.length === 1) {
      trace.snapshots.pop();
      trace.truncated = true;
      trace.byte_compacted = true;
      continue;
    }
    break;
  }
  return trace;
}

function subscribe(observable, callback) {
  return observable?.subscribe(callback) ?? { unsubscribe() {} };
}

export function createViewerTraceRecorder({
  plugin,
  onEntry = () => {},
  shouldContinueSemanticStream = () => false,
  now = () => performance.now(),
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  settleMs = 100,
  maxEntries = MAX_SNAPSHOTS,
  maxBytes = MAX_CAPTURE_BYTES,
}) {
  const entryLimit = normalizeEntryLimit(maxEntries);
  const byteLimit = Math.min(MAX_CAPTURE_BYTES, Math.max(1024, Number(maxBytes) || MAX_CAPTURE_BYTES));
  let active = false;
  let startedAt = 0;
  let snapshots = [];
  let appTrace = [];
  let truncated = false;
  let byteCompacted = false;
  let visualCaptureActive = false;
  let visualOmissionReported = false;
  let activePaneId = null;
  let sequence = 0;
  let cameraTimer = null;
  const stateTimers = new Map();
  const paneAttachments = new Set();

  const elapsed = () => Math.max(0, Math.round(now() - startedAt));

  const clearPendingStateTimer = key => {
    const pending = stateTimers.get(key);
    if (!pending) return null;
    clearTimer(pending.timer);
    stateTimers.delete(key);
    return pending;
  };

  const stopVisualCaptureWork = () => {
    visualCaptureActive = false;
    if (cameraTimer !== null) clearTimer(cameraTimer);
    cameraTimer = null;
    for (const pending of stateTimers.values()) clearTimer(pending.timer);
    stateTimers.clear();
  };

  const streamOmission = ({ reason, omittedKind, omittedBytes = null }) => {
    if (!active) return false;
    const omission = {
      kind: 'omitted',
      t_ms: elapsed(),
      seq: sequence++,
      omitted_kind: omittedKind,
      omitted_entry_count: 1,
      reason,
    };
    if (Number.isInteger(omittedBytes) && omittedBytes >= 0) omission.omitted_bytes = omittedBytes;
    try { onEntry(omission); } catch (error) {
      console.warn('Viewer trace omission marker skipped:', error.message);
    }
    return true;
  };

  const stopVisualCaptureWithMarker = reason => {
    truncated = true;
    let keepSemanticStream = false;
    try { keepSemanticStream = !!shouldContinueSemanticStream(); } catch {}
    if (keepSemanticStream && !visualOmissionReported) {
      visualOmissionReported = true;
      streamOmission({ reason, omittedKind: 'visual_capture' });
    }
    stopVisualCaptureWork();
    if (!keepSemanticStream) active = false;
  };

  const compactRecordedEntries = () => {
    const envelope = traceEnvelope({
      snapshots,
      appTrace,
      durationMs: elapsed(),
      truncated,
      byteCompacted,
    });
    fitTraceToBudget(envelope, byteLimit - 1);
    snapshots = envelope.snapshots;
    appTrace = envelope.app_trace ?? [];
    if (envelope.truncated) truncated = true;
    if (envelope.byte_compacted) byteCompacted = true;
  };

  const appendSnapshot = entry => {
    if (!active || !visualCaptureActive) return false;
    if (snapshots.length >= entryLimit) {
      stopVisualCaptureWithMarker('snapshot_limit');
      return false;
    }
    const candidate = { t_ms: elapsed(), seq: sequence, ...entry };
    snapshots.push(candidate);
    if (serializedBytes(candidate) >= byteLimit - 256) {
      snapshots.pop();
      truncated = true;
      byteCompacted = true;
      streamOmission({
        reason: 'single_entry_byte_budget',
        omittedKind: typeof entry?.kind === 'string' ? entry.kind : 'unknown',
        omittedBytes: serializedBytes(candidate),
      });
      return false;
    }
    sequence += 1;
    compactRecordedEntries();
    try { onEntry(candidate); } catch (error) {
      console.warn('Viewer trace stream entry skipped:', error.message);
    }
    if (snapshots.length === entryLimit) {
      stopVisualCaptureWithMarker('snapshot_limit');
    }
    return true;
  };

  const appendAppEvent = entry => {
    if (!active) return false;
    if (appTrace.length >= MAX_APP_EVENTS) {
      appTrace.shift();
      truncated = true;
    }
    const candidate = { t_ms: elapsed(), seq: sequence++, ...entry };
    appTrace.push(candidate);
    compactRecordedEntries();
    try { onEntry(candidate); } catch (error) {
      console.warn('Viewer trace stream entry skipped:', error.message);
    }
    return true;
  };

  const captureStateFrom = ({
    targetPlugin = plugin,
    sourcePaneId = null,
    scope = sourcePaneId ? 'pane' : 'viewer',
  } = {}) => {
    if (!active || !visualCaptureActive) return false;
    try {
      const snapshot = targetPlugin.state.getSnapshot(SNAPSHOT_PARAMS);
      const entry = { kind: 'state', snapshot };
      const normalizedPane = normalizePaneId(sourcePaneId);
      if (normalizedPane) entry.source_pane_id = normalizedPane;
      if (scope !== 'viewer') entry.scope = compactString(String(scope), 32);
      return appendSnapshot(entry);
    } catch (error) {
      console.warn('Viewer snapshot skipped:', error.message);
      return false;
    }
  };

  const captureCameraFrom = (cameraSnapshot, {
    targetPlugin = plugin,
    sourcePaneId = activePaneId,
  } = {}) => {
    if (!active || !visualCaptureActive) return false;
    try {
      const camera = cameraSnapshot ?? targetPlugin.canvas3d.camera.getSnapshot();
      const entry = { kind: 'camera', camera };
      const normalizedPane = normalizePaneId(sourcePaneId);
      if (normalizedPane) entry.source_pane_id = normalizedPane;
      return appendSnapshot(entry);
    } catch (error) {
      console.warn('Viewer camera snapshot skipped:', error.message);
      return false;
    }
  };

  const scheduleStateCapture = (key, options) => {
    if (!active || !visualCaptureActive) return;
    clearPendingStateTimer(key);
    const timer = setTimer(() => {
      stateTimers.delete(key);
      captureStateFrom(options);
    }, settleMs);
    stateTimers.set(key, { timer, options });
  };

  const cameraChanges = plugin.canvas3d.camera.changed ?? plugin.canvas3d.camera.stateChanged;
  const cameraSubscription = subscribe(cameraChanges, () => {
    if (!active || !visualCaptureActive) return;
    if (cameraTimer !== null) clearTimer(cameraTimer);
    cameraTimer = setTimer(() => {
      cameraTimer = null;
      captureCameraFrom();
    }, settleMs);
  });
  const canonicalCapture = { targetPlugin: plugin, sourcePaneId: null, scope: 'viewer' };
  const focusSubscription = subscribe(
    plugin.managers?.structure?.focus?.behaviors?.current,
    () => scheduleStateCapture('viewer', canonicalCapture),
  );
  const selectionSubscription = subscribe(
    plugin.managers?.structure?.selection?.events?.changed,
    () => scheduleStateCapture('viewer', canonicalCapture),
  );

  const buildCurrentTrace = appState => {
    const trace = traceEnvelope({
      snapshots: snapshots.slice(),
      appTrace: appTrace.slice(),
      durationMs: elapsed(),
      truncated,
      byteCompacted,
      appState: compactAppState(appState),
    });
    fitTraceToBudget(trace, byteLimit - 1);
    return deepFreeze(trace);
  };

  const recorder = {
    start({ appState, activePaneId: initialPaneId } = {}) {
      active = entryLimit > 0;
      visualCaptureActive = entryLimit > 0;
      startedAt = now();
      snapshots = [];
      appTrace = [];
      truncated = entryLimit === 0;
      byteCompacted = false;
      visualOmissionReported = false;
      activePaneId = normalizePaneId(initialPaneId);
      sequence = 0;
      if (cameraTimer !== null) clearTimer(cameraTimer);
      cameraTimer = null;
      for (const pending of stateTimers.values()) clearTimer(pending.timer);
      stateTimers.clear();
      captureStateFrom();
      if (appState !== undefined) recorder.recordAppEvent('question_start', appState);
    },

    captureState(options = {}) {
      return captureStateFrom({
        targetPlugin: options.plugin ?? plugin,
        sourcePaneId: options.sourcePaneId ?? null,
        scope: options.scope ?? (options.sourcePaneId ? 'pane' : 'viewer'),
      });
    },

    captureCamera(cameraSnapshot = null, options = {}) {
      if (cameraTimer !== null) {
        clearTimer(cameraTimer);
        cameraTimer = null;
      }
      return captureCameraFrom(cameraSnapshot, {
        targetPlugin: options.plugin ?? plugin,
        sourcePaneId: options.sourcePaneId === undefined ? activePaneId : options.sourcePaneId,
      });
    },

    recordAppEvent(action, state) {
      const normalizedAction = compactString(action, 64);
      if (!normalizedAction) return false;
      const entry = { kind: 'app', action: normalizedAction };
      const normalizedState = compactAppState(state);
      if (normalizedState !== undefined) entry.state = normalizedState;
      if (activePaneId) entry.active_pane_id = activePaneId;
      return appendAppEvent(entry);
    },

    setActivePane(paneId, reason = 'interaction') {
      const normalizedPane = normalizePaneId(paneId);
      if (normalizedPane === activePaneId) return false;
      activePaneId = normalizedPane;
      return appendAppEvent({
        kind: 'active_pane',
        pane_id: normalizedPane,
        reason: compactString(String(reason), 48) || 'interaction',
      });
    },

    attachPane({ paneId, plugin: panePlugin, element = null }) {
      if (!panePlugin) throw new Error('Pane plugin is required');
      const normalizedPane = normalizePaneId(paneId);
      if (!normalizedPane) throw new Error('Pane id is required');
      const key = `pane:${normalizedPane}`;
      const options = { targetPlugin: panePlugin, sourcePaneId: normalizedPane, scope: 'pane' };
      const subscriptions = [
        subscribe(
          panePlugin.managers?.structure?.focus?.behaviors?.current,
          () => scheduleStateCapture(key, options),
        ),
        subscribe(
          panePlugin.managers?.structure?.selection?.events?.changed,
          () => scheduleStateCapture(key, options),
        ),
      ];
      const domEvents = ['pointerenter', 'pointerdown', 'focusin', 'wheel', 'touchstart'];
      const noteInteraction = event => recorder.setActivePane(normalizedPane, event.type);
      for (const eventName of domEvents) element?.addEventListener?.(eventName, noteInteraction, {
        passive: eventName === 'wheel' || eventName === 'touchstart',
      });

      let disposed = false;
      const dispose = () => {
        if (disposed) return;
        disposed = true;
        clearPendingStateTimer(key);
        for (const subscription of subscriptions) subscription.unsubscribe();
        for (const eventName of domEvents) element?.removeEventListener?.(eventName, noteInteraction);
        paneAttachments.delete(dispose);
      };
      paneAttachments.add(dispose);
      return dispose;
    },

    snapshot(appState) {
      return buildCurrentTrace(appState);
    },

    captureContext(appState) {
      const context = {
        schema_version: 1,
        captured_at_ms: elapsed(),
        app_state: compactAppState(appState) ?? {},
        viewer_snapshot: {
          schema_version: 1,
          active_pane_id: activePaneId,
          shared_camera: null,
          viewer_state: null,
          viewer_state_omitted: false,
        },
        viewer_trace_tail: traceEnvelope({
          snapshots: snapshots.slice(-MAX_CONTEXT_TAIL_ENTRIES),
          appTrace: appTrace.slice(-MAX_CONTEXT_TAIL_ENTRIES),
          durationMs: elapsed(),
          truncated: truncated || byteCompacted,
          byteCompacted,
        }),
      };
      try {
        context.viewer_snapshot.shared_camera = plugin.canvas3d.camera.getSnapshot();
      } catch {
        context.viewer_snapshot.shared_camera = null;
      }

      let viewerState;
      try {
        viewerState = plugin.state.getSnapshot(SNAPSHOT_PARAMS);
      } catch {
        context.viewer_snapshot.viewer_state_omitted = 'capture_failed';
      }
      if (viewerState !== undefined) {
        context.viewer_snapshot.viewer_state = viewerState;
        if (serializedBytes(context.viewer_snapshot) >= MAX_SUGGESTION_COMPONENT_BYTES) {
          context.viewer_snapshot.viewer_state = null;
          context.viewer_snapshot.viewer_state_omitted = 'byte_budget';
        }
      }

      fitTraceToBudget(context.viewer_trace_tail, MAX_SUGGESTION_COMPONENT_BYTES - 1);
      if (serializedBytes(context.viewer_snapshot) >= MAX_SUGGESTION_COMPONENT_BYTES) {
        context.viewer_snapshot.shared_camera = null;
        context.viewer_snapshot.camera_omitted = 'byte_budget';
      }
      if (serializedBytes(context) >= byteLimit) {
        context.viewer_trace_tail = traceEnvelope({
          snapshots: [], appTrace: [], durationMs: elapsed(), truncated: true, byteCompacted: true,
        });
      }
      if (serializedBytes(context) >= byteLimit) {
        context.viewer_snapshot.viewer_state = null;
        context.viewer_snapshot.viewer_state_omitted = 'byte_budget';
      }
      if (serializedBytes(context) >= byteLimit) {
        context.app_state = {};
        context.app_state_omitted = 'byte_budget';
      }
      return deepFreeze(context);
    },

    stop({ appState } = {}) {
      for (const key of [...stateTimers.keys()]) {
        const pending = clearPendingStateTimer(key);
        if (pending) captureStateFrom(pending.options);
      }
      if (cameraTimer !== null) {
        clearTimer(cameraTimer);
        cameraTimer = null;
        captureCameraFrom();
      }
      active = false;
      visualCaptureActive = false;
      return buildCurrentTrace(appState);
    },

    dispose() {
      active = false;
      stopVisualCaptureWork();
      cameraSubscription.unsubscribe();
      focusSubscription.unsubscribe();
      selectionSubscription.unsubscribe();
      for (const dispose of [...paneAttachments]) dispose();
    },
  };

  return recorder;
}
