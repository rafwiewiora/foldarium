const DATABASE_NAME = 'foldarium-research-v1';
const DATABASE_VERSION = 1;
const STORE_NAME = 'weekly-trace-batches';
// PostgreSQL validates jsonb::text after parsing. Keep substantial headroom for
// its normalized textual representation instead of targeting the 480 KiB DB cap.
export const MAX_WEEKLY_TRACE_CLIENT_BYTES = 300 * 1024;
const MAX_BATCH_ENTRIES = 500;
const MAX_QUEUED_BATCHES = 256;
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
  'displayname', 'participantname', 'playername', 'username', 'votecomment',
]);

function normalizedKey(key) {
  return String(key).replace(/[^a-z0-9]/gi, '').toLowerCase();
}

function stripIdentityFields(value) {
  if (Array.isArray(value)) return value.map(stripIdentityFields);
  if (!value || typeof value !== 'object') return value;
  const result = {};
  for (const [key, child] of Object.entries(value)) {
    if (!IDENTITY_KEYS.has(normalizedKey(key))) result[key] = stripIdentityFields(child);
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
  maxTraceBytes = MAX_WEEKLY_TRACE_CLIENT_BYTES,
  getAppState = () => null,
  onWarning = message => console.warn(message),
  classifyError = error => {
    const rawCode = String(error?.code || '');
    const status = Number(error?.status ?? error?.statusCode);
    if (status === 400 || status === 401 || status === 403 || status === 404
      || status === 409 || status === 413 || status === 422) return 'permanent';
    if (/^(22|23|42|P0|PGRST)/.test(rawCode)) return 'permanent';
    return 'retryable';
  },
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
  const acknowledgedThrough = new Map();
  const undurableVisits = new Set();
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
            acknowledgedThrough.set(record.payload.visitId, Math.max(
              acknowledgedThrough.get(record.payload.visitId) ?? -1,
              record.payload.lastSequence,
            ));
          } catch (error) {
            onWarning(`Thinking trace remains queued in memory: ${error.message}`);
            blocked = true;
            break;
          }
        }
        if (blocked) break;
        for (const record of await store.list()) {
          if (record.deadLetter) continue;
          try {
            await submitBatch(record.payload);
            await store.delete(record.traceBatchId);
            acknowledgedThrough.set(record.payload.visitId, Math.max(
              acknowledgedThrough.get(record.payload.visitId) ?? -1,
              record.payload.lastSequence,
            ));
          } catch (error) {
            const permanent = classifyError(error) === 'permanent';
            const attempts = (record.attempts ?? 0) + 1;
            if (permanent) {
              const deadLetter = {
                reason: 'permanent_submission_error',
                attempts,
                failedAt: now(),
                message: String(error?.message || 'unknown error').slice(0, 240),
              };
              await store.put({ ...record, attempts, deadLetter });
              undurableVisits.add(record.payload.visitId);
              onWarning(`Thinking trace batch needs attention (${deadLetter.reason}); later batches will continue.`);
              continue;
            }
            await store.put({ ...record, attempts });
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
    const queued = persistence.catch(() => {}).then(async () => {
      try {
        const queuedRecords = await store.list();
        if (queuedRecords.length >= MAX_QUEUED_BATCHES) {
          undurableVisits.add(record.payload.visitId);
          onWarning('Thinking trace queue is full; this visit will retain the legacy vote snapshot.');
          return false;
        }
        await store.put(record);
        return true;
      } catch (error) {
        volatileRecords.set(record.traceBatchId, record);
        onWarning(`Thinking trace could not be queued: ${error.message}`);
        return false;
      }
    });
    persistence = queued;
    void queued.then(drain).catch(() => {});
    return queued;
  };

  const visitHasVolatileRecords = (visitId, throughSequence) => (
    [...volatileRecords.values()].some(candidate => (
      candidate.payload.visitId === visitId
      && candidate.payload.firstSequence <= throughSequence
    ))
  );

  const storeHasCoverage = async (visitId, throughSequence) => {
    if (undurableVisits.has(visitId)) return false;
    const records = await store.list();
    const intervals = records.filter(record => (
      !record.deadLetter
      &&
      record.payload.visitId === visitId
      && record.payload.firstSequence <= throughSequence
    )).map(record => [record.payload.firstSequence, record.payload.lastSequence])
      .sort((left, right) => left[0] - right[0] || left[1] - right[1]);
    let covered = acknowledgedThrough.get(visitId) ?? -1;
    for (const [first, last] of intervals) {
      if (first > covered + 1) return false;
      covered = Math.max(covered, last);
      if (covered >= throughSequence) return true;
    }
    return covered >= throughSequence;
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
        stream_schema_version: 2,
        molstar_version: '4.6.0',
        visit_id: visit.visitId,
        visit_started_at: visit.startedAt,
        visit_ordinal: visit.ordinal,
        entries: taken,
      },
      appState: normalizedState(),
    };
    return { traceBatchId, queuedAt: now(), payload };
  };

  const stream = {
    startSession({
      sessionId,
      roundId,
      nextVisitOrdinal = 0,
      lastVisitStartedAt = -1,
    }) {
      if (!sessionId || !roundId) throw new Error('Trace session identity is invalid.');
      if (!Number.isSafeInteger(nextVisitOrdinal) || nextVisitOrdinal < 0
          || !Number.isSafeInteger(lastVisitStartedAt) || lastVisitStartedAt < -1) {
        throw new Error('Trace session continuation metadata is invalid.');
      }
      session = { sessionId, roundId, nextVisitOrdinal, lastVisitStartedAt };
      disposed = false;
      schedule();
      void drain();
    },

    startVisit({ itemId, questionIndex, visitId = uuid() }) {
      if (!session || !itemId || !Number.isInteger(questionIndex) || questionIndex < 0) {
        throw new Error('Trace visit identity is invalid.');
      }
      if (visit && entries.length) void stream.flush('navigation');
      const startedAt = Math.max(now(), session.lastVisitStartedAt + 1);
      session.lastVisitStartedAt = startedAt;
      visit = {
        itemId,
        questionIndex,
        visitId,
        lastSequence: null,
        startedAt,
        ordinal: session.nextVisitOrdinal++,
      };
      entries = [];
      schedule();
      return visitId;
    },

    recordEntry(rawEntry) {
      if (!session || !visit || !rawEntry || typeof rawEntry !== 'object') return false;
      let entry;
      try { entry = stripIdentityFields(jsonClone(rawEntry)); } catch { return false; }
      const expectedSequence = visit.lastSequence === null ? 0 : visit.lastSequence + 1;
      if (!Number.isInteger(entry.seq) || entry.seq !== expectedSequence) {
        undurableVisits.add(visit.visitId);
        onWarning('Thinking trace sequence is discontinuous; this visit will retain the legacy vote snapshot.');
        return false;
      }
      visit.lastSequence = entry.seq;
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

    async checkpoint(reason = 'vote') {
      if (!visit || !Number.isInteger(visit.lastSequence)) return null;
      const binding = {
        visitId: visit.visitId,
        throughSequence: visit.lastSequence,
      };
      const record = takeBatch(reason);
      let durable = true;
      if (record) {
        durable = await persist(record);
      }
      if (visitHasVolatileRecords(binding.visitId, binding.throughSequence)) durable = false;
      else if (durable) durable = await storeHasCoverage(binding.visitId, binding.throughSequence);
      return { ...binding, durable };
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

    async queueStatus() {
      await persistence;
      const stored = await store.list();
      return {
        queued: stored.filter(record => !record.deadLetter).length + volatileRecords.size,
        deadLettered: stored.filter(record => !!record.deadLetter).length,
      };
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
