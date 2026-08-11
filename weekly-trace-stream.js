const DATABASE_NAME = 'foldarium-research-v1';
const DATABASE_VERSION = 1;
const STORE_NAME = 'weekly-trace-batches';
const MAX_TRACE_BYTES = 480 * 1024;
const MAX_BATCH_ENTRIES = 500;
const DEFAULT_FLUSH_INTERVAL_MS = 5_000;

function jsonBytes(value) {
  try {
    const serialized = JSON.stringify(value);
    return serialized === undefined ? Infinity : new TextEncoder().encode(serialized).byteLength;
  } catch {
    return Infinity;
  }
}

function jsonClone(value) {
  return JSON.parse(JSON.stringify(value));
}

const IDENTITY_KEYS = new Set([
  'display_name', 'participant_name', 'player_name', 'username',
]);

function stripIdentityFields(value) {
  if (Array.isArray(value)) return value.map(stripIdentityFields);
  if (!value || typeof value !== 'object') return value;
  const result = {};
  for (const [key, child] of Object.entries(value)) {
    if (!IDENTITY_KEYS.has(key.toLowerCase())) result[key] = stripIdentityFields(child);
  }
  return result;
}

function openDatabase(indexedDb) {
  return new Promise((resolve, reject) => {
    const request = indexedDb.open(DATABASE_NAME, DATABASE_VERSION);
    request.onerror = () => reject(request.error || new Error('Trace database could not be opened.'));
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        const store = database.createObjectStore(STORE_NAME, { keyPath: 'traceBatchId' });
        store.createIndex('queuedAt', 'queuedAt');
      }
    };
    request.onsuccess = () => resolve(request.result);
  });
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error || new Error('Trace queue operation failed.'));
    request.onsuccess = () => resolve(request.result);
  });
}

export function createIndexedDbTraceStore({ indexedDb = globalThis.indexedDB } = {}) {
  if (!indexedDb) throw new Error('IndexedDB is unavailable.');
  let databasePromise;
  const database = () => {
    databasePromise ||= openDatabase(indexedDb);
    return databasePromise;
  };
  return {
    async put(record) {
      const db = await database();
      const transaction = db.transaction(STORE_NAME, 'readwrite');
      await requestResult(transaction.objectStore(STORE_NAME).put(jsonClone(record)));
    },
    async list() {
      const db = await database();
      const transaction = db.transaction(STORE_NAME, 'readonly');
      const records = await requestResult(transaction.objectStore(STORE_NAME).getAll());
      return records.sort((left, right) => (
        left.queuedAt - right.queuedAt || left.traceBatchId.localeCompare(right.traceBatchId)
      ));
    },
    async delete(traceBatchId) {
      const db = await database();
      const transaction = db.transaction(STORE_NAME, 'readwrite');
      await requestResult(transaction.objectStore(STORE_NAME).delete(traceBatchId));
    },
  };
}

function compactOversizedEntry(entry) {
  return {
    kind: 'omitted',
    seq: Number.isInteger(entry?.seq) ? entry.seq : null,
    t_ms: Number.isFinite(entry?.t_ms) ? entry.t_ms : null,
    omitted_kind: typeof entry?.kind === 'string' ? entry.kind.slice(0, 32) : 'unknown',
    omitted_bytes: jsonBytes(entry),
    reason: 'single_entry_byte_budget',
  };
}

export function createWeeklyTraceStream({
  submitBatch,
  store = createIndexedDbTraceStore(),
  uuid = () => crypto.randomUUID(),
  now = () => Date.now(),
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  flushIntervalMs = DEFAULT_FLUSH_INTERVAL_MS,
  maxTraceBytes = MAX_TRACE_BYTES,
  getAppState = () => null,
  onWarning = message => console.warn(message),
} = {}) {
  if (typeof submitBatch !== 'function') throw new Error('Trace batch submitter is required.');
  let session = null;
  let visit = null;
  let entries = [];
  let timer = null;
  let persistence = Promise.resolve();
  let draining = null;
  let drainRequested = false;
  const volatileRecords = new Map();
  let disposed = false;

  const schedule = () => {
    if (disposed || !session || timer !== null) return;
    timer = setTimer(() => {
      timer = null;
      void stream.flush('interval');
      schedule();
    }, flushIntervalMs);
  };

  const normalizedState = () => {
    try {
      const value = getAppState();
      if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
      const cloned = stripIdentityFields(jsonClone(value));
      return jsonBytes(cloned) <= 64 * 1024 ? cloned : null;
    } catch {
      return null;
    }
  };

  const drain = async () => {
    await persistence;
    if (draining) {
      drainRequested = true;
      return draining;
    }
    const run = (async () => {
      let blocked = false;
      do {
        drainRequested = false;
        await persistence;
        for (const record of [...volatileRecords.values()]) {
          try {
            await submitBatch(record.payload);
            volatileRecords.delete(record.traceBatchId);
          } catch (error) {
            onWarning(`Thinking trace remains queued in memory: ${error.message}`);
            blocked = true;
            break;
          }
        }
        if (blocked) break;
        for (const record of await store.list()) {
          try {
            await submitBatch(record.payload);
            await store.delete(record.traceBatchId);
          } catch (error) {
            onWarning(`Thinking trace remains queued: ${error.message}`);
            blocked = true;
            break;
          }
        }
      } while (!blocked && drainRequested);
    })();
    const finalized = run.finally(() => {
      if (draining === finalized) draining = null;
    });
    draining = finalized;
    return finalized;
  };

  const persist = record => {
    persistence = persistence.catch(() => {}).then(() => store.put(record)).catch(error => {
      volatileRecords.set(record.traceBatchId, record);
      onWarning(`Thinking trace could not be queued: ${error.message}`);
    });
    void persistence.then(drain).catch(() => {});
    return persistence;
  };

  const takeBatch = reason => {
    if (!session || !visit || !entries.length) return null;
    const taken = entries;
    entries = [];
    const traceBatchId = uuid();
    const firstSequence = taken.find(entry => Number.isInteger(entry.seq))?.seq ?? 0;
    const lastSequence = [...taken].reverse().find(entry => Number.isInteger(entry.seq))?.seq
      ?? firstSequence;
    const payload = {
      traceBatchId,
      sessionId: session.sessionId,
      roundId: session.roundId,
      itemId: visit.itemId,
      questionIndex: visit.questionIndex,
      visitId: visit.visitId,
      firstSequence,
      lastSequence,
      reason,
      trace: {
        version: 1,
        visit_id: visit.visitId,
        entries: taken,
      },
      appState: normalizedState(),
    };
    return { traceBatchId, queuedAt: now(), payload };
  };

  const stream = {
    startSession({ sessionId, roundId }) {
      if (!sessionId || !roundId) throw new Error('Trace session identity is invalid.');
      session = { sessionId, roundId };
      disposed = false;
      schedule();
      void drain();
    },

    startVisit({ itemId, questionIndex, visitId = uuid() }) {
      if (!session || !itemId || !Number.isInteger(questionIndex) || questionIndex < 0) {
        throw new Error('Trace visit identity is invalid.');
      }
      if (visit && entries.length) void stream.flush('navigation');
      visit = { itemId, questionIndex, visitId };
      entries = [];
      schedule();
      return visitId;
    },

    recordEntry(rawEntry) {
      if (!session || !visit || !rawEntry || typeof rawEntry !== 'object') return false;
      let entry;
      try { entry = stripIdentityFields(jsonClone(rawEntry)); } catch { return false; }
      if (jsonBytes(entry) > maxTraceBytes - 256) entry = compactOversizedEntry(entry);
      if (entries.length >= MAX_BATCH_ENTRIES) {
        const record = takeBatch('byte_budget');
        if (record) void persist(record);
      }
      const candidate = { version: 1, visit_id: visit.visitId, entries: [...entries, entry] };
      if (entries.length && jsonBytes(candidate) > maxTraceBytes) {
        const record = takeBatch('byte_budget');
        if (record) void persist(record);
      }
      entries.push(entry);
      return true;
    },

    flush(reason = 'interval') {
      const record = takeBatch(reason);
      if (!record) return drain();
      return persist(record).then(drain);
    },

    endVisit(reason = 'navigation') {
      const result = stream.flush(reason);
      visit = null;
      entries = [];
      return result;
    },

    async drain() {
      return drain();
    },

    dispose() {
      disposed = true;
      if (timer !== null) clearTimer(timer);
      timer = null;
      return stream.endVisit('completion');
    },
  };

  return stream;
}
