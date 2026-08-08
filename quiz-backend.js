const OPERATION_PREFIX = 'foldariumSyncOpV2:';
const DEAD_LETTER_PREFIX = 'foldariumSyncDeadV2:';
const KIND_ORDER = { session: 0, answer: 1, complete: 2 };
const MAX_VIEWER_TRACE_BYTES = 512 * 1024;
const SUPABASE_ESM = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm';

function normalizeViewerTraceResult(viewerTrace) {
  if (viewerTrace === null || viewerTrace === undefined) {
    return { value: null, warning: null };
  }
  try {
    const serialized = JSON.stringify(viewerTrace);
    if (serialized === undefined) {
      return { value: null, warning: 'not JSON-serializable' };
    }
    if (new TextEncoder().encode(serialized).byteLength > MAX_VIEWER_TRACE_BYTES) {
      return { value: null, warning: `exceeds ${MAX_VIEWER_TRACE_BYTES}-byte limit` };
    }
    const normalized = JSON.parse(serialized);
    if (normalized === null
      || typeof normalized !== 'object'
      || Array.isArray(normalized)
      || normalized.version !== 1
      || !Array.isArray(normalized.snapshots)) {
      return { value: null, warning: 'invalid version-1 shape' };
    }
    return { value: normalized, warning: null };
  } catch (error) {
    return { value: null, warning: error.message };
  }
}

export function normalizeViewerTrace(viewerTrace) {
  return normalizeViewerTraceResult(viewerTrace).value;
}

const disabledBackend = {
  startSession: () => null,
  recordAnswer: () => {},
  completeSession: () => {},
  flush: async ({ strict = false } = {}) => {
    if (strict) throw persistenceUnavailableError();
  },
  claimUsername: async () => {
    throw new Error('Leaderboard persistence is unavailable.');
  },
  getLeaderboard: async () => {
    throw new Error('Leaderboard persistence is unavailable.');
  },
  getWeeklyRound: async () => null,
  getWeeklyVotes: async () => [],
  getWeeklyVoteTotals: async () => [],
  submitWeeklyVote: async () => {
    throw new Error('Weekly quiz persistence is unavailable.');
  },
};

export function createDeferredBackend({
  uuid = () => crypto.randomUUID(),
} = {}) {
  let target = null;
  let failure = null;
  const pending = [];
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });
  void ready.catch(() => {});

  const call = (method, args) => {
    try {
      if (target) return target[method](...args);
      pending.push([method, args]);
    } catch (error) {
      console.warn('Quiz persistence event ignored:', error.message);
    }
  };

  const requireTarget = async () => {
    if (target) return target;
    if (failure) throw failure;
    return ready;
  };

  return {
    startSession(values) {
      try {
        const id = uuid();
        call('startSession', [{ ...values, id }]);
        return id;
      } catch {
        return null;
      }
    },
    recordAnswer: (...args) => { call('recordAnswer', args); },
    completeSession: (...args) => { call('completeSession', args); },
    flush(options = {}) {
      if (!options.strict) return target?.flush(options) || Promise.resolve();
      return requireTarget().then(backend => backend.flush(options));
    },
    async claimUsername(...args) {
      return (await requireTarget()).claimUsername(...args);
    },
    async getLeaderboard() {
      return (await requireTarget()).getLeaderboard();
    },
    async getWeeklyRound() {
      return (await requireTarget()).getWeeklyRound();
    },
    async getWeeklyVotes(...args) {
      return (await requireTarget()).getWeeklyVotes(...args);
    },
    async getWeeklyVoteTotals(...args) {
      return (await requireTarget()).getWeeklyVoteTotals(...args);
    },
    async submitWeeklyVote(...args) {
      return (await requireTarget()).submitWeeklyVote(...args);
    },
    attach(backend) {
      if (target || failure) return;
      target = backend;
      for (const [method, args] of pending.splice(0)) call(method, args);
      resolveReady(backend);
    },
    fail(error) {
      if (target || failure) return;
      failure = persistenceUnavailableError(
        `Quiz persistence initialization failed: ${error.message}`,
        error,
      );
      rejectReady(failure);
    },
  };
}

export function createQuizBackend({
  client,
  getClient = async () => client,
  storage = window.localStorage,
  uuid = () => crypto.randomUUID(),
  now = () => new Date(),
}) {
  let flushing = null;
  let flushOutcome = null;
  let flushAgain = false;
  const enqueueFailures = new Map();

  const enqueue = (kind, value, { warnOnFailure = true } = {}) => {
    const entry = { kind, value };
    const key = operationKey(entry);
    try {
      storage.setItem(key, JSON.stringify(entry));
      enqueueFailures.delete(key);
      if (flushing) flushAgain = true;
    } catch (error) {
      enqueueFailures.set(key, error);
      if (warnOnFailure) console.warn('Quiz result queue could not be saved:', error.message);
      return false;
    }
    void flush();
    return true;
  };

  async function userId(remoteClient) {
    const current = await remoteClient.auth.getSession();
    if (current.error) throw current.error;
    if (current.data.session) return current.data.session.user.id;
    const created = await remoteClient.auth.signInAnonymously();
    if (created.error) throw created.error;
    return created.data.user.id;
  }

  async function write(remoteClient, entry, uid) {
    if (entry.kind === 'session') {
      return remoteClient.from('quiz_sessions').upsert(
        { ...entry.value, user_id: uid },
        { onConflict: 'id', ignoreDuplicates: true },
      );
    }
    if (entry.kind === 'answer') {
      return remoteClient.from('quiz_answers').upsert(
        entry.value,
        { onConflict: 'id', ignoreDuplicates: true },
      );
    }
    return remoteClient.from('quiz_sessions')
      .update({ completed_at: entry.value.completed_at })
      .eq('id', entry.value.id)
      .eq('user_id', uid);
  }

  async function drain(outcome) {
    if (!readOperations(storage).length) return;
    let remoteClient;
    let uid;
    try {
      remoteClient = await getClient();
      uid = await userId(remoteClient);
    } catch (error) {
      if (isRetryable(error)) {
        console.warn('Quiz results remain queued:', error.message);
        return;
      }
      for (const stored of readOperations(storage)) {
        deadLetter(storage, stored, error, now);
        outcome.deadLettered++;
      }
      return;
    }

    for (const stored of readOperations(storage)) {
      try {
        const result = await write(remoteClient, stored.entry, uid);
        if (result.error) throw result.error;
        if (storage.getItem(stored.key) === stored.raw) storage.removeItem(stored.key);
      } catch (error) {
        if (isRetryable(error)) {
          console.warn('Quiz results remain queued:', error.message);
          return;
        }
        deadLetter(storage, stored, error, now);
        outcome.deadLettered++;
      }
    }
  }

  async function drainRequested(outcome) {
    do {
      flushAgain = false;
      await drain(outcome);
    } while (flushAgain);
  }

  function flush({ strict = false } = {}) {
    if (!flushing) {
      const outcome = { deadLettered: 0 };
      flushOutcome = outcome;
      const finalized = drainRequested(outcome).finally(() => {
        if (flushing === finalized) {
          flushing = null;
          flushOutcome = null;
        }
      });
      flushing = finalized;
    }
    const currentFlush = flushing;
    const currentOutcome = flushOutcome;
    if (!strict) return currentFlush;
    return currentFlush.then(() => {
      if (enqueueFailures.size) {
        const firstFailure = enqueueFailures.values().next().value;
        throw persistenceIncompleteError(
          `${enqueueFailures.size} quiz operation(s) could not be queued in browser storage`
          + ` (${firstFailure.message}). Free browser storage space and try saving again.`,
        );
      }
      const deadLettered = currentOutcome.deadLettered;
      if (deadLettered) {
        throw persistenceIncompleteError(
          `${deadLettered} quiz operation(s) were dead-lettered.`,
        );
      }
      const queued = readOperations(storage).length;
      if (queued) {
        throw persistenceIncompleteError(`${queued} quiz operation(s) remain queued.`);
      }
    });
  }

  async function leaderboardRpc(name, args, authenticate = false) {
    let remoteClient;
    try {
      remoteClient = await getClient();
    } catch (error) {
      throw new Error(`Leaderboard persistence is unavailable: ${error.message}`, {
        cause: error,
      });
    }
    if (authenticate) await userId(remoteClient);
    const result = await remoteClient.rpc(name, args);
    if (result.error) throw result.error;
    return result.data;
  }

  return {
    startSession({ id = uuid(), source, difficulty }) {
      enqueue('session', {
        id,
        source,
        difficulty,
        started_at: now().toISOString(),
      });
      return id;
    },
    recordAnswer(sessionId, questionIndex, record) {
      if (!sessionId) return;
      const normalizedTrace = normalizeViewerTraceResult(record.viewer_trace);
      const viewerTrace = normalizedTrace.value;
      if (normalizedTrace.warning) console.warn('Viewer trace omitted:', normalizedTrace.warning);
      const answer = {
        id: uuid(),
        session_id: sessionId,
        question_index: questionIndex,
        item_id: record.item_id,
        source: record.source,
        difficulty: record.difficulty,
        picked_none: record.picked_none,
        picked_sample: record.picked_none ? null : record.picked_sample,
        picked_correct: record.picked_correct,
        picked_rmsd: record.picked_rmsd,
        af3_pick_sample: record.af3_pick_sample < 0 ? null : record.af3_pick_sample,
        af3_correct: record.af3_correct,
        has_correct: record.has_correct,
        n_clusters: record.n_clusters,
        answered_at: new Date(record.ts * 1000).toISOString(),
        viewer_trace: viewerTrace,
      };
      if (!enqueue('answer', answer, { warnOnFailure: viewerTrace === null }) && viewerTrace !== null) {
        console.warn('Viewer trace omitted:', 'local queue rejected trace-backed answer');
        enqueue('answer', { ...answer, viewer_trace: null });
      }
    },
    completeSession(sessionId) {
      if (sessionId) enqueue('complete', { id: sessionId, completed_at: now().toISOString() });
    },
    flush,
    async claimUsername(username) {
      return leaderboardRpc('claim_leaderboard_username', {
        p_username: username,
      }, true);
    },
    async getLeaderboard() {
      return (await leaderboardRpc('get_leaderboard')) ?? [];
    },
    async getWeeklyRound() {
      const rows = await leaderboardRpc('get_current_weekly_quiz_round');
      if (rows == null) return null;
      if (!Array.isArray(rows)) throw new Error('Weekly quiz round response is invalid.');
      return rows[0] ?? null;
    },
    async getWeeklyVotes(roundId) {
      if (!roundId) throw new Error('Weekly round identity is invalid.');
      return (await leaderboardRpc('get_my_weekly_quiz_votes', {
        p_round_id: roundId,
      }, true)) ?? [];
    },
    async getWeeklyVoteTotals(roundId) {
      if (!roundId) throw new Error('Weekly round identity is invalid.');
      return (await leaderboardRpc('get_weekly_quiz_vote_totals', {
        p_round_id: roundId,
      })) ?? [];
    },
    async submitWeeklyVote(roundId, itemId, choiceId, pickedNone) {
      if (!roundId || !itemId || typeof pickedNone !== 'boolean') {
        throw new Error('Weekly vote identity is invalid.');
      }
      if ((pickedNone && choiceId != null) || (!pickedNone && !choiceId)) {
        throw new Error('Weekly vote choice is invalid.');
      }
      let remoteClient;
      try {
        remoteClient = await getClient();
      } catch (error) {
        throw new Error(`Weekly quiz persistence is unavailable: ${error.message}`, {
          cause: error,
        });
      }
      await userId(remoteClient);
      const result = await remoteClient.rpc('submit_weekly_quiz_vote', {
        p_vote_id: uuid(),
        p_round_id: roundId,
        p_item_id: itemId,
        p_choice_id: pickedNone ? null : choiceId,
        p_picked_none: pickedNone,
      });
      if (result.error) throw result.error;
      return result.data;
    },
  };
}

export function initQuizBackend(config = {}, dependencies = {}) {
  if (!config.url || !config.publishableKey) return disabledBackend;
  let clientPromise;
  const getClient = () => {
    if (!clientPromise) {
      const attempt = Promise.resolve().then(async () => {
        const createClient = dependencies.createClient
          || (await import(SUPABASE_ESM)).createClient;
        return createClient(config.url, config.publishableKey, {
          auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
        });
      });
      clientPromise = attempt.catch(error => {
        clientPromise = null;
        throw error;
      });
    }
    return clientPromise;
  };
  const backend = createQuizBackend({ ...dependencies, getClient });
  queueMicrotask(() => { void backend.flush(); });
  return backend;
}

function operationKey(entry) {
  return `${OPERATION_PREFIX}${KIND_ORDER[entry.kind]}:${entry.value.id}`;
}

function persistenceUnavailableError(
  message = 'Quiz persistence is unavailable.',
  cause,
) {
  const error = new Error(message, cause ? { cause } : undefined);
  error.code = 'QUIZ_PERSISTENCE_UNAVAILABLE';
  return error;
}

function persistenceIncompleteError(message) {
  const error = new Error(`Quiz persistence is incomplete: ${message}`);
  error.code = 'QUIZ_PERSISTENCE_INCOMPLETE';
  return error;
}

function deadLetter(storage, stored, error, now) {
  const key = `${DEAD_LETTER_PREFIX}${stored.key.slice(OPERATION_PREFIX.length)}`;
  try {
    storage.setItem(key, JSON.stringify({
      ...stored.entry,
      failed_at: now().toISOString(),
      error: { message: error.message, status: error.status, code: error.code },
    }));
    if (storage.getItem(stored.key) === stored.raw) storage.removeItem(stored.key);
  } catch (storageError) {
    console.warn('Quiz result dead letter could not be saved:', storageError.message);
  }
}

function isRetryable(error = {}) {
  const status = Number(error.status || error.statusCode || 0);
  const message = String(error.message || '').toLowerCase();
  if (String(error.code || '').startsWith('23') || error.code === '42501') return false;
  return !status
    || status === 408
    || status === 425
    || status === 429
    || status >= 500
    || error.name === 'AbortError'
    || error.name === 'TimeoutError'
    || /network|fetch|timeout|timed out/.test(message);
}

function readOperations(storage) {
  try {
    const entries = [];
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index);
      if (!key?.startsWith(OPERATION_PREFIX)) continue;
      const raw = storage.getItem(key);
      if (!raw) continue;
      entries.push({ key, raw, entry: JSON.parse(raw) });
    }
    return entries.sort((left, right) => left.key.localeCompare(right.key));
  } catch {
    return [];
  }
}
