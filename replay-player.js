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

    if (entry.kind === 'state') {
      if (!isObject(entry.snapshot)) return false;
    } else if (entry.kind === 'camera') {
      if (!isObject(entry.camera)) return false;
    } else {
      return false;
    }
    previousTime = entry.t_ms;
  }
  return true;
}

export async function playViewerTrace(plugin, trace, {
  now = () => performance.now(),
  sleep = defaultSleep,
  signal,
} = {}) {
  if (!isSupportedTrace(trace)) throw new Error('Unsupported viewer trace');
  throwIfAborted(signal);

  const startedAt = now();
  for (const entry of trace.snapshots) {
    throwIfAborted(signal);
    const waitMs = entry.t_ms - (now() - startedAt);
    if (waitMs > 0) {
      await sleep(waitMs, signal);
      throwIfAborted(signal);
    }

    if (entry.kind === 'state') {
      await plugin.state.setSnapshot(entry.snapshot);
    } else {
      plugin.canvas3d.camera.setState(entry.camera, 250);
    }
  }
}
