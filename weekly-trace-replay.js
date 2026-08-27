const FALLBACK_MOLSTAR_VERSION = '4.6.0';

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function bindingFromAttempt(attempt) {
  const binding = attempt?.app_state?.continuous_trace;
  if (!isObject(binding)
    || typeof binding.visit_id !== 'string' || !binding.visit_id
    || !Number.isInteger(binding.through_sequence) || binding.through_sequence < 0) return null;
  return binding;
}

function replayAppState(attempt) {
  if (!isObject(attempt?.app_state)) return undefined;
  const state = { ...attempt.app_state };
  delete state.continuous_trace;
  return state;
}

export function reconstructWeeklyViewerTrace(attempt, batches = []) {
  if (isObject(attempt?.viewer_trace) && Array.isArray(attempt.viewer_trace.snapshots)) {
    return attempt.viewer_trace;
  }
  const binding = bindingFromAttempt(attempt);
  if (!binding) return null;

  const relevant = batches.filter(batch => (
    batch?.session_id === attempt.session_id
    && batch?.item_id === attempt.item_id
    && batch?.visit_id === binding.visit_id
    && Number.isInteger(batch.first_sequence)
    && batch.first_sequence <= binding.through_sequence
    && isObject(batch.trace)
    && Array.isArray(batch.trace.entries)
  )).sort((left, right) => (
    left.first_sequence - right.first_sequence
    || String(left.submitted_at || '').localeCompare(String(right.submitted_at || ''))
    || String(left.trace_batch_id || '').localeCompare(String(right.trace_batch_id || ''))
  ));

  const bySequence = new Map();
  let incomplete = false;
  let byteCompacted = false;
  let molstarVersion = null;
  for (const batch of relevant) {
    if (batch.trace.visit_id !== binding.visit_id) {
      incomplete = true;
      continue;
    }
    if (typeof batch.trace.molstar_version === 'string' && batch.trace.molstar_version) {
      if (molstarVersion && molstarVersion !== batch.trace.molstar_version) incomplete = true;
      molstarVersion ||= batch.trace.molstar_version;
    }
    for (const entry of batch.trace.entries) {
      if (!Number.isInteger(entry?.seq) || entry.seq < 0
        || entry.seq > binding.through_sequence) continue;
      const prior = bySequence.get(entry.seq);
      if (prior && JSON.stringify(prior) !== JSON.stringify(entry)) incomplete = true;
      else if (!prior) bySequence.set(entry.seq, entry);
    }
  }

  const entries = [...bySequence.values()].sort((left, right) => left.seq - right.seq);
  if (!entries.length) return null;
  if (entries[0].seq !== 0) incomplete = true;
  for (let sequence = entries[0].seq; sequence <= binding.through_sequence; sequence += 1) {
    if (!bySequence.has(sequence)) incomplete = true;
  }
  const snapshots = [];
  const appTrace = [];
  for (const entry of entries) {
    if (entry.kind === 'state' || entry.kind === 'camera') snapshots.push(entry);
    else if (entry.kind === 'app' || entry.kind === 'active_pane') appTrace.push(entry);
    else if (entry.kind === 'omitted') {
      incomplete = true;
      if (entry.reason === 'single_entry_byte_budget') byteCompacted = true;
    } else incomplete = true;
  }
  const trace = {
    version: 1,
    molstar_version: molstarVersion || FALLBACK_MOLSTAR_VERSION,
    duration_ms: Math.max(0, ...entries.map(entry => Number(entry.t_ms) || 0)),
    truncated: incomplete,
    snapshots,
  };
  if (appTrace.length) trace.app_trace = appTrace;
  if (byteCompacted) trace.byte_compacted = true;
  const appState = replayAppState(attempt);
  if (appState !== undefined) trace.app_state = appState;
  if (incomplete) trace.continuous_trace_incomplete = true;
  return trace;
}

export function reconstructWeeklyAttempts(attempts = [], batches = []) {
  return attempts.map(attempt => ({
    ...attempt,
    viewer_trace: reconstructWeeklyViewerTrace(attempt, batches),
  }));
}
