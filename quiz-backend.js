const OPERATION_PREFIX = 'foldariumSyncOpV2:';
const DEAD_LETTER_PREFIX = 'foldariumSyncDeadV2:';
const KIND_ORDER = { session: 0, answer: 1, complete: 2 };
const MAX_VIEWER_TRACE_BYTES = 512 * 1024;
const MAX_SUGGESTION_CONTEXT_BYTES = 512 * 1024;
const SUPABASE_ESM = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.112.4/+esm';

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

function normalizeJsonObject(value, label, maxBytes = MAX_SUGGESTION_CONTEXT_BYTES) {
  try {
    const serialized = JSON.stringify(value);
    if (serialized === undefined || new TextEncoder().encode(serialized).byteLength > maxBytes) {
      throw new Error(`${label} exceeds its ${maxBytes}-byte limit`);
    }
    const normalized = JSON.parse(serialized);
    if (!normalized || typeof normalized !== 'object' || Array.isArray(normalized)) {
      throw new Error(`${label} must be a JSON object`);
    }
    return normalized;
  } catch (error) {
    throw new Error(`${label} is invalid: ${error.message}`, { cause: error });
  }
}

const disabledBackend = {
  startSession: () => null,
  startNamedSession: async () => {
    throw new Error('Named quiz persistence is unavailable.');
  },
  resumeNamedWeeklySession: async () => {
    throw new Error('Named weekly quiz resumption is unavailable.');
  },
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
  getWeeklyResults: async () => {
    throw new Error('Weekly results are unavailable.');
  },
  getWeeklyRetrospectiveArchive: async () => {
    throw new Error('Weekly retrospective archive is unavailable.');
  },
  getWeeklyRetrospectiveDetail: async () => {
    throw new Error('Weekly retrospective detail is unavailable.');
  },
  getWeeklyRetrospectiveAllTime: async () => {
    throw new Error('Weekly retrospective rankings are unavailable.');
  },
  getWeeklyRetrospectiveAdmin: async () => {
    throw new Error('Weekly retrospective admin access is unavailable.');
  },
  submitWeeklyVote: async () => {
    throw new Error('Weekly quiz persistence is unavailable.');
  },
  submitWeeklyVoteAttempt: async () => {
    throw new Error('Weekly quiz replay persistence is unavailable.');
  },
  submitWeeklyTraceBatch: async () => {
    throw new Error('Weekly thinking-trace persistence is unavailable.');
  },
  submitUserSuggestion: async () => {
    throw new Error('Suggestion persistence is unavailable.');
  },
};

function readOnlyBackend(readBackend) {
  const unavailable = () => {
    throw new Error('This Preview is read-only; recorded quiz persistence is unavailable.');
  };
  return {
    startSession: () => null,
    startNamedSession: async () => unavailable(),
    resumeNamedWeeklySession: async () => unavailable(),
    recordAnswer: () => {},
    completeSession: () => {},
    flush: async ({ strict = false } = {}) => {
      if (strict) unavailable();
    },
    claimUsername: async () => unavailable(),
    getLeaderboard: (...args) => readBackend.getLeaderboard(...args),
    getWeeklyRound: (...args) => readBackend.getWeeklyRound(...args),
    // Avoid anonymous-auth account creation in a read-only Preview. The public
    // totals and round remain available; viewer inspection uses ?dev=1.
    getWeeklyVotes: async () => [],
    getWeeklyVoteTotals: (...args) => readBackend.getWeeklyVoteTotals(...args),
    getWeeklyResults: (...args) => readBackend.getWeeklyResults(...args),
    getWeeklyRetrospectiveArchive: (...args) => (
      readBackend.getWeeklyRetrospectiveArchive(...args)
    ),
    getWeeklyRetrospectiveDetail: (...args) => (
      readBackend.getWeeklyRetrospectiveDetail(...args)
    ),
    getWeeklyRetrospectiveAllTime: (...args) => (
      readBackend.getWeeklyRetrospectiveAllTime(...args)
    ),
    getWeeklyRetrospectiveAdmin: (...args) => (
      readBackend.getWeeklyRetrospectiveAdmin(...args)
    ),
    submitWeeklyVote: async () => unavailable(),
    submitWeeklyVoteAttempt: async () => unavailable(),
    submitWeeklyTraceBatch: async () => unavailable(),
    submitUserSuggestion: async () => unavailable(),
  };
}

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
    async startNamedSession(...args) {
      return (await requireTarget()).startNamedSession(...args);
    },
    async resumeNamedWeeklySession(...args) {
      return (await requireTarget()).resumeNamedWeeklySession(...args);
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
    async getWeeklyResults(...args) {
      return (await requireTarget()).getWeeklyResults(...args);
    },
    async getWeeklyRetrospectiveArchive(...args) {
      return (await requireTarget()).getWeeklyRetrospectiveArchive(...args);
    },
    async getWeeklyRetrospectiveDetail(...args) {
      return (await requireTarget()).getWeeklyRetrospectiveDetail(...args);
    },
    async getWeeklyRetrospectiveAllTime(...args) {
      return (await requireTarget()).getWeeklyRetrospectiveAllTime(...args);
    },
    async getWeeklyRetrospectiveAdmin(...args) {
      return (await requireTarget()).getWeeklyRetrospectiveAdmin(...args);
    },
    async submitWeeklyVote(...args) {
      return (await requireTarget()).submitWeeklyVote(...args);
    },
    async submitWeeklyVoteAttempt(...args) {
      return (await requireTarget()).submitWeeklyVoteAttempt(...args);
    },
    async submitWeeklyTraceBatch(...args) {
      return (await requireTarget()).submitWeeklyTraceBatch(...args);
    },
    async submitUserSuggestion(...args) {
      return (await requireTarget()).submitUserSuggestion(...args);
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
  pagePath = globalThis.location?.pathname || '/',
  weeklyEnvironment = 'production',
}) {
  if (!['production', 'preview', 'development'].includes(weeklyEnvironment)) {
    throw new Error('Weekly quiz deployment environment is invalid.');
  }
  let flushing = null;
  let flushOutcome = null;
  let flushAgain = false;
  const enqueueFailures = new Map();
  const weeklyNamedSessionIds = new Set();
  const weeklyPostRevealSessionIds = new Set();

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

  async function retrospectiveRequest(parameters = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(parameters)) {
      if (value !== null && value !== undefined && value !== false) {
        query.set(key, value === true ? '1' : String(value));
      }
    }
    let response;
    try {
      response = await fetch(`/api/weekly-retrospectives${query.size ? `?${query}` : ''}`);
    } catch (error) {
      throw new Error(`Weekly retrospectives are unavailable: ${error.message}`, {
        cause: error,
      });
    }
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(payload?.error || 'Weekly retrospective request failed');
    }
    return payload;
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
    async startNamedSession({
      id = uuid(), source, difficulty, weeklyRoundId = null, displayName,
      initialAppState = null, postReveal = false,
    }) {
      const normalizedName = String(displayName || '').trim().replace(/\s+/g, ' ');
      if (!id || !['cameo', 'rnp', 'weekly'].includes(source)
        || !['easy', 'hard'].includes(difficulty)
        || !normalizedName || normalizedName.length > 80) {
        throw new Error('Named quiz session values are invalid.');
      }
      if ((source === 'weekly') !== !!weeklyRoundId) {
        throw new Error('Named weekly session identity is invalid.');
      }
      if (source === 'weekly') {
        const appState = initialAppState == null
          ? null : normalizeJsonObject(initialAppState, 'Initial app state');
        await leaderboardRpc(postReveal
          ? 'start_named_weekly_post_reveal_session'
          : 'start_named_weekly_quiz_session', {
          p_session_id: id,
          p_round_id: weeklyRoundId,
          p_display_name: normalizedName,
          p_initial_app_state: appState,
        }, true);
        (postReveal ? weeklyPostRevealSessionIds : weeklyNamedSessionIds).add(id);
      } else {
        await leaderboardRpc('start_named_quiz_session', {
          p_session_id: id,
          p_source: source,
          p_difficulty: difficulty,
          p_display_name: normalizedName,
        }, true);
      }
      return id;
    },
    async resumeNamedWeeklySession({ sessionId, roundId, postReveal = false }) {
      if (!sessionId || !roundId) {
        throw new Error('Named weekly session resumption identity is invalid.');
      }
      const rows = await leaderboardRpc(postReveal
        ? 'resume_named_weekly_post_reveal_session'
        : 'resume_named_weekly_quiz_session', {
        p_session_id: sessionId,
        p_round_id: roundId,
      }, true);
      if (!Array.isArray(rows) || rows.length !== 1
        || rows[0]?.session_id !== sessionId || rows[0]?.round_id !== roundId
        || !Number.isSafeInteger(Number(rows[0]?.next_visit_ordinal))
        || Number(rows[0]?.next_visit_ordinal) < 0
        || !Number.isSafeInteger(Number(rows[0]?.last_visit_started_at))
        || Number(rows[0]?.last_visit_started_at) < -1) {
        throw new Error('Named weekly session resumption response is invalid.');
      }
      (postReveal ? weeklyPostRevealSessionIds : weeklyNamedSessionIds).add(sessionId);
      return {
        sessionId,
        nextVisitOrdinal: Number(rows[0].next_visit_ordinal),
        lastVisitStartedAt: Number(rows[0].last_visit_started_at),
      };
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
      if (!sessionId) return;
      if (weeklyPostRevealSessionIds.has(sessionId)) {
        void leaderboardRpc('complete_named_weekly_post_reveal_session', {
          p_session_id: sessionId,
        }, true).catch(error => {
          console.warn('Post-reveal session completion was not saved:', error.message);
        });
        return;
      }
      if (weeklyNamedSessionIds.has(sessionId)) {
        void leaderboardRpc('complete_named_weekly_quiz_session', {
          p_session_id: sessionId,
        }, true).catch(error => {
          console.warn('Weekly session completion was not saved:', error.message);
        });
        return;
      }
      enqueue('complete', { id: sessionId, completed_at: now().toISOString() });
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
      const rows = await leaderboardRpc('get_current_weekly_quiz_round', {
        p_environment: weeklyEnvironment,
      });
      if (rows == null) return null;
      if (!Array.isArray(rows)) throw new Error('Weekly quiz round response is invalid.');
      return rows[0] ?? null;
    },
    async getWeeklyVotes(roundId, { postReveal = false } = {}) {
      if (!roundId) throw new Error('Weekly round identity is invalid.');
      return (await leaderboardRpc(postReveal
        ? 'get_my_weekly_post_reveal_votes'
        : 'get_my_weekly_quiz_votes', {
        p_round_id: roundId,
      }, true)) ?? [];
    },
    async getWeeklyVoteTotals(roundId) {
      if (!roundId) throw new Error('Weekly round identity is invalid.');
      return (await leaderboardRpc('get_weekly_quiz_vote_totals', {
        p_round_id: roundId,
      })) ?? [];
    },
    async getWeeklyResults(roundId) {
      if (!roundId) throw new Error('Weekly round identity is invalid.');
      let response;
      try {
        response = await fetch(`/api/weekly-results?round_id=${encodeURIComponent(roundId)}`);
      } catch (error) {
        throw new Error(`Weekly results are unavailable: ${error.message}`, { cause: error });
      }
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.error || 'Weekly results request failed');
      }
      return payload;
    },
    async getWeeklyRetrospectiveArchive({ limit = 20, cursor = null } = {}) {
      return retrospectiveRequest({ limit, cursor });
    },
    async getWeeklyRetrospectiveDetail(roundId) {
      if (!roundId) throw new Error('Weekly retrospective round identity is invalid.');
      return retrospectiveRequest({ round_id: roundId });
    },
    async getWeeklyRetrospectiveAllTime({
      ranking = 'total_correct',
      participantKind = null,
    } = {}) {
      return retrospectiveRequest({
        all_time: true,
        ranking,
        participant_kind: participantKind,
      });
    },
    async getWeeklyRetrospectiveAdmin({
      roundId = null,
      allTime = false,
      ranking = 'total_correct',
      participantKind = null,
    } = {}) {
      if ((!roundId && !allTime) || (roundId && allTime)) {
        throw new Error('Weekly retrospective admin request is invalid.');
      }
      return retrospectiveRequest({
        admin: true,
        all_time: allTime,
        round_id: roundId,
        ranking: allTime ? ranking : null,
        participant_kind: allTime ? participantKind : null,
      });
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
    async submitWeeklyVoteAttempt({
      voteAttemptId = uuid(), sessionId, roundId, itemId, questionIndex, choiceId, pickedNone,
      viewerTrace = null, appState = null, voteComment = null, postReveal = false,
    }) {
      if (!voteAttemptId || !sessionId || !roundId || !itemId || !Number.isInteger(questionIndex)
        || questionIndex < 0 || typeof pickedNone !== 'boolean') {
        throw new Error('Weekly vote-attempt identity is invalid.');
      }
      if ((pickedNone && choiceId != null) || (!pickedNone && !choiceId)) {
        throw new Error('Weekly vote-attempt choice is invalid.');
      }
      const normalizedTrace = normalizeViewerTraceResult(viewerTrace);
      if (normalizedTrace.warning) {
        console.warn(`Weekly viewer trace omitted: ${normalizedTrace.warning}`);
      }
      const normalizedState = appState == null
        ? null : normalizeJsonObject(appState, 'Weekly app state');
      const normalizedComment = voteComment == null || String(voteComment).trim() === ''
        ? null : String(voteComment).trim();
      if (normalizedComment && (normalizedComment.length > 4000
        || new TextEncoder().encode(normalizedComment).byteLength > 16000)) {
        throw new Error('Weekly vote comment is invalid or too large.');
      }
      const stateFromTrace = normalizedTrace.value?.app_state;
      const submittedState = normalizedState || (stateFromTrace && typeof stateFromTrace === 'object'
        ? normalizeJsonObject(stateFromTrace, 'Weekly app state') : null);
      return leaderboardRpc(postReveal
        ? 'submit_weekly_post_reveal_vote_attempt'
        : 'submit_weekly_quiz_vote_attempt', {
        p_vote_attempt_id: voteAttemptId,
        p_session_id: sessionId,
        p_round_id: roundId,
        p_item_id: itemId,
        p_question_index: questionIndex,
        p_choice_id: pickedNone ? null : choiceId,
        p_picked_none: pickedNone,
        p_viewer_trace: normalizedTrace.value,
        p_app_state: submittedState,
        p_active_pane_id: submittedState?.active_pane_id || null,
        p_vote_comment: normalizedComment,
      }, true);
    },
    async submitWeeklyTraceBatch({
      traceBatchId, sessionId, roundId, itemId, questionIndex, visitId,
      firstSequence, lastSequence, reason, trace, appState = null,
    }) {
      const reasons = new Set([
        'interval', 'byte_budget', 'navigation', 'vote', 'visibility', 'completion',
      ]);
      if (!traceBatchId || !sessionId || !roundId || !itemId || !visitId
        || !Number.isInteger(questionIndex) || questionIndex < 0
        || !Number.isInteger(firstSequence) || firstSequence < 0
        || !Number.isInteger(lastSequence) || lastSequence < firstSequence
        || !reasons.has(reason)) {
        throw new Error('Weekly trace-batch identity is invalid.');
      }
      const normalizedTrace = normalizeJsonObject(trace, 'Weekly trace batch');
      if (normalizedTrace.version !== 1 || !Array.isArray(normalizedTrace.entries)
        || !normalizedTrace.entries.length || normalizedTrace.entries.length > 500) {
        throw new Error('Weekly trace batch is invalid.');
      }
      const sequences = normalizedTrace.entries.map(entry => entry?.seq);
      if (normalizedTrace.visit_id !== visitId
        || normalizedTrace.stream_schema_version !== 2
        || typeof normalizedTrace.molstar_version !== 'string'
        || !normalizedTrace.molstar_version
        || !Number.isInteger(normalizedTrace.visit_started_at)
        || normalizedTrace.visit_started_at < 0
        || !Number.isInteger(normalizedTrace.visit_ordinal)
        || normalizedTrace.visit_ordinal < 0
        || sequences.some(sequence => !Number.isInteger(sequence) || sequence < 0)
        || sequences.some((sequence, index) => sequence !== firstSequence + index)
        || sequences[0] !== firstSequence || sequences.at(-1) !== lastSequence) {
        throw new Error('Weekly trace batch sequence binding is invalid.');
      }
      if (new TextEncoder().encode(JSON.stringify(normalizedTrace)).byteLength > 300 * 1024) {
        throw new Error('Weekly trace batch exceeds its 307200-byte client limit.');
      }
      const normalizedState = appState == null
        ? null : normalizeJsonObject(appState, 'Weekly trace app state', 64 * 1024);
      return leaderboardRpc('append_weekly_quiz_trace_batch', {
        p_trace_batch_id: traceBatchId,
        p_session_id: sessionId,
        p_round_id: roundId,
        p_item_id: itemId,
        p_question_index: questionIndex,
        p_visit_id: visitId,
        p_first_sequence: firstSequence,
        p_last_sequence: lastSequence,
        p_flush_reason: reason,
        p_trace: normalizedTrace,
        p_app_state: normalizedState,
      }, true);
    },
    async submitUserSuggestion({
      sessionId, roundId = null, itemId = null, suggestionText, contextSnapshot,
    }) {
      const text = String(suggestionText || '').trim();
      if (!sessionId || !text || text.length > 4000) {
        throw new Error('Suggestion values are invalid.');
      }
      const context = normalizeJsonObject(contextSnapshot, 'Suggestion context');
      const isWeekly = weeklyNamedSessionIds.has(sessionId) || !!roundId;
      const appState = context.app_state && typeof context.app_state === 'object'
        ? normalizeJsonObject(context.app_state, 'Suggestion app state') : null;
      const viewerSnapshot = context.viewer_snapshot && typeof context.viewer_snapshot === 'object'
        ? normalizeJsonObject(context.viewer_snapshot, 'Suggestion viewer snapshot') : null;
      const traceTail = context.viewer_trace_tail && typeof context.viewer_trace_tail === 'object'
        ? normalizeJsonObject(context.viewer_trace_tail, 'Suggestion viewer trace tail') : null;
      return leaderboardRpc('submit_user_suggestion', {
        p_suggestion_id: uuid(),
        p_suggestion_text: text,
        p_context: isWeekly ? 'weekly-quiz' : 'pose-quiz',
        p_quiz_session_id: isWeekly ? null : sessionId,
        p_weekly_session_id: isWeekly ? sessionId : null,
        p_item_id: itemId,
        p_page_path: pagePath,
        p_app_state: appState,
        p_viewer_snapshot: viewerSnapshot,
        p_viewer_trace_tail: traceTail,
      }, true);
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
  const weeklyEnvironment = ['production', 'preview', 'development'].includes(
    config.deploymentEnvironment,
  ) ? config.deploymentEnvironment : 'production';
  const writableBackend = createQuizBackend({
    ...dependencies,
    getClient,
    weeklyEnvironment,
  });
  const backend = config.writable === false
    ? readOnlyBackend(writableBackend)
    : writableBackend;
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
