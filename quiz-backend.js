const QUEUE_KEY = 'foldariumSyncQueueV1';
const SUPABASE_ESM = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm';

const disabledBackend = {
  startSession: () => null,
  recordAnswer: () => {},
  completeSession: () => {},
  flush: async () => {},
};

export function createQuizBackend({
  client,
  storage = window.localStorage,
  uuid = () => crypto.randomUUID(),
  now = () => new Date(),
}) {
  let queue = readQueue(storage);
  let flushing = null;

  const save = () => {
    try {
      storage.setItem(QUEUE_KEY, JSON.stringify(queue));
    } catch (error) {
      console.warn('Quiz result queue could not be saved:', error.message);
    }
  };
  const enqueue = (kind, value) => {
    queue.push({ kind, value });
    save();
    void flush();
  };

  async function userId() {
    const current = await client.auth.getSession();
    if (current.error) throw current.error;
    if (current.data.session) return current.data.session.user.id;
    const created = await client.auth.signInAnonymously();
    if (created.error) throw created.error;
    return created.data.user.id;
  }

  async function write(entry, uid) {
    if (entry.kind === 'session') {
      return client.from('quiz_sessions').upsert(
        { ...entry.value, user_id: uid },
        { onConflict: 'id', ignoreDuplicates: true },
      );
    }
    if (entry.kind === 'answer') {
      return client.from('quiz_answers').upsert(
        entry.value,
        { onConflict: 'id', ignoreDuplicates: true },
      );
    }
    return client.from('quiz_sessions')
      .update({ completed_at: entry.value.completed_at })
      .eq('id', entry.value.id)
      .eq('user_id', uid);
  }

  async function drain() {
    try {
      const uid = await userId();
      while (queue.length) {
        const result = await write(queue[0], uid);
        if (result.error) throw result.error;
        queue.shift();
        save();
      }
    } catch (error) {
      console.warn('Quiz results remain queued:', error.message);
    }
  }

  function flush() {
    if (!flushing) flushing = drain().finally(() => { flushing = null; });
    return flushing;
  }

  return {
    startSession({ source, difficulty }) {
      const id = uuid();
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
      enqueue('answer', {
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
      });
    },
    completeSession(sessionId) {
      if (sessionId) enqueue('complete', { id: sessionId, completed_at: now().toISOString() });
    },
    flush,
  };
}

export async function initQuizBackend(config = {}, dependencies = {}) {
  if (!config.url || !config.publishableKey) return disabledBackend;
  try {
    const createClient = dependencies.createClient
      || (await import(SUPABASE_ESM)).createClient;
    const client = createClient(config.url, config.publishableKey, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });
    const backend = createQuizBackend({ client, ...dependencies });
    void backend.flush();
    return backend;
  } catch (error) {
    console.warn('Remote quiz persistence disabled:', error.message);
    return disabledBackend;
  }
}

function readQueue(storage) {
  try {
    const value = JSON.parse(storage.getItem(QUEUE_KEY) || '[]');
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}
