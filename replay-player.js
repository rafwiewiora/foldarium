const TRACE_VERSION = 1;
const MOLSTAR_VERSION = '4.6.0';

function abortError() {
  return new DOMException('Viewer replay aborted', 'AbortError');
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw abortError();
}

function defaultSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(finish, ms);

    function finish() {
      signal?.removeEventListener('abort', cancel);
      resolve();
    }

    function cancel() {
      clearTimeout(timer);
      signal.removeEventListener('abort', cancel);
      reject(abortError());
    }

    signal?.addEventListener('abort', cancel, { once: true });
  });
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isSupportedTrace(trace) {
  if (!isObject(trace)
    || trace.version !== TRACE_VERSION
    || trace.molstar_version !== MOLSTAR_VERSION
    || !Array.isArray(trace.snapshots)) return false;

  let previousTime = -1;
  for (const entry of trace.snapshots) {
    if (!isObject(entry)
      || !Number.isFinite(entry.t_ms)
      || entry.t_ms < 0
      || entry.t_ms < previousTime) return false;
    if (entry.seq !== undefined && (!Number.isFinite(entry.seq) || entry.seq < 0)) return false;
    if (entry.source_pane_id !== undefined && typeof entry.source_pane_id !== 'string') return false;

    if (entry.kind === 'state') {
      if (!isObject(entry.snapshot)) return false;
    } else if (entry.kind === 'camera') {
      if (!isObject(entry.camera)) return false;
    } else {
      return false;
    }
    previousTime = entry.t_ms;
  }

  if (trace.app_trace !== undefined) {
    if (!Array.isArray(trace.app_trace)) return false;
    previousTime = -1;
    for (const event of trace.app_trace) {
      if (!isObject(event)
        || !Number.isFinite(event.t_ms)
        || event.t_ms < 0
        || event.t_ms < previousTime) return false;
      if (event.seq !== undefined && (!Number.isFinite(event.seq) || event.seq < 0)) return false;
      if (event.kind === 'app') {
        if (typeof event.action !== 'string' || !event.action) return false;
        if (event.state !== undefined && !isObject(event.state)) return false;
      } else if (event.kind === 'active_pane') {
        if (event.pane_id !== null && typeof event.pane_id !== 'string') return false;
      } else {
        return false;
      }
      previousTime = event.t_ms;
    }
  }
  if (trace.app_state !== undefined && !isObject(trace.app_state)) return false;
  return true;
}

export function validateViewerTrace(trace) {
  if (!isSupportedTrace(trace)) throw new Error('Unsupported viewer trace');
  return trace;
}

function pinCamera(plugin) {
  try {
    const camera = plugin.canvas3d.camera;
    const current = camera.getSnapshot?.();
    if (current) camera.setState(current, 0);
  } catch {
    // Cancellation must still reject even when Mol* cannot expose the in-flight camera.
  }
}

function stateCameraTransitionDuration(snapshot) {
  const camera = snapshot.camera;
  if (!isObject(camera)) return null;
  if (camera.transitionStyle !== 'animate') return 0;
  const duration = camera.transitionDurationInMs;
  return Number.isFinite(duration) && duration > 0 ? duration : 0;
}

export async function playViewerTrace(plugin, trace, {
  now = () => performance.now(),
  sleep = defaultSleep,
  signal,
  onAppEvent = () => {},
  onAppStateChange = () => {},
  onActivePaneChange = () => {},
} = {}) {
  validateViewerTrace(trace);
  throwIfAborted(signal);

  const startedAt = now();
  let cameraTransitionEndsAt = -Infinity;
  const timeline = [
    ...trace.snapshots.map((entry, index) => ({ entry, type: 'viewer', index })),
    ...(trace.app_trace ?? []).map((entry, index) => ({ entry, type: 'app', index })),
  ].sort((left, right) => left.entry.t_ms - right.entry.t_ms
    || (Number(left.entry.seq) || 0) - (Number(right.entry.seq) || 0)
    || (left.type === right.type ? left.index - right.index : left.type.localeCompare(right.type)));
  try {
    if (trace.app_state !== undefined) await onAppStateChange(trace.app_state, null);
    for (const { entry, type } of timeline) {
      throwIfAborted(signal);
      const waitMs = entry.t_ms - (now() - startedAt);
      if (waitMs > 0) {
        await sleep(waitMs, signal);
        throwIfAborted(signal);
      }

      if (type === 'app') {
        await onAppEvent(entry);
        if (entry.kind === 'active_pane') {
          await onActivePaneChange(entry.pane_id, entry);
        } else {
          if (entry.state !== undefined) await onAppStateChange(entry.state, entry);
          if (entry.active_pane_id !== undefined) {
            await onActivePaneChange(entry.active_pane_id, entry);
          }
        }
        throwIfAborted(signal);
      } else if (entry.kind === 'state') {
        await plugin.state.setSnapshot(entry.snapshot);
        if (entry.source_pane_id !== undefined) {
          await onActivePaneChange(entry.source_pane_id, entry);
        }
        const transitionDuration = stateCameraTransitionDuration(entry.snapshot);
        if (transitionDuration !== null) {
          cameraTransitionEndsAt = transitionDuration > 0
            ? now() + transitionDuration
            : -Infinity;
        }
        throwIfAborted(signal);
      } else {
        plugin.canvas3d.camera.setState(entry.camera, 250);
        if (entry.source_pane_id !== undefined) {
          await onActivePaneChange(entry.source_pane_id, entry);
        }
        cameraTransitionEndsAt = now() + 250;
      }
    }

    const transitionWait = cameraTransitionEndsAt - now();
    if (transitionWait > 0) {
      await sleep(transitionWait, signal);
      throwIfAborted(signal);
    }
  } catch (error) {
    if ((signal?.aborted || error?.name === 'AbortError') && cameraTransitionEndsAt > now()) {
      pinCamera(plugin);
    }
    throw error;
  }
  throwIfAborted(signal);
}
