# Supabase Quiz Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist anonymous users' quiz sessions and answers in Supabase without blocking the existing static quiz when remote persistence is unavailable.

**Architecture:** A small browser module owns anonymous Supabase authentication, an idempotent local retry queue, and direct RLS-protected writes. The existing quiz calls that module at session start, answer reveal, and completion. A SQL migration defines the two-table Postgres schema and client permissions.

**Tech Stack:** Browser JavaScript modules, Supabase JS v2, Supabase Postgres/Auth/RLS, Node.js built-in test runner, static Vercel deployment.

## Global Constraints

- Keep Foldarium deployable as a static site; do not add a framework or serverless function.
- Use anonymous Supabase Auth and preserve account-linking compatibility.
- Store quiz context only; do not intentionally store personal, device, location, or network metadata.
- Never expose or reference a Supabase service-role key.
- Remote failures must not interrupt quiz play or remove the existing `poseQuizLog`.
- Client answer rows are append-only; basic RLS is the agreed integrity level.

---

### Task 1: Browser persistence module

**Files:**
- Create: `package.json`
- Create: `quiz-backend.js`
- Create: `tests/quiz-backend.test.js`

**Interfaces:**
- Produces: `initQuizBackend(config, dependencies?) -> Promise<QuizBackend>`
- Produces: `QuizBackend.startSession({ source, difficulty }) -> string | null`
- Produces: `QuizBackend.recordAnswer(sessionId, questionIndex, record) -> void`
- Produces: `QuizBackend.completeSession(sessionId) -> void`
- Produces: `QuizBackend.flush() -> Promise<void>`

- [ ] **Step 1: Add the Node test command**

Create `package.json` with no runtime dependencies:

```json
{
  "name": "foldarium-minimal",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "node --test"
  }
}
```

- [ ] **Step 2: Write failing persistence tests**

Create `tests/quiz-backend.test.js`. Use an in-memory Storage implementation and a fake Supabase client. Cover:

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { createQuizBackend, initQuizBackend } from '../quiz-backend.js';

test('empty configuration disables remote persistence without loading Supabase', async () => {
  const backend = await initQuizBackend({}, {
    createClient: () => { throw new Error('must not load'); },
  });
  assert.equal(backend.startSession({ source: 'cameo', difficulty: 'easy' }), null);
  await backend.flush();
});

test('queues a normalized session and answer and removes them after successful writes', async () => {
  const { client, writes } = fakeSupabase();
  const storage = memoryStorage();
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('session-id', 'answer-id'),
    now: () => new Date('2026-08-05T18:00:00.000Z'),
  });

  const sessionId = backend.startSession({ source: 'cameo', difficulty: 'hard' });
  backend.recordAnswer(sessionId, 2, {
    item_id: 'item-7',
    source: 'cameo',
    difficulty: 'hard',
    picked_none: true,
    picked_sample: -1,
    picked_correct: true,
    picked_rmsd: null,
    af3_pick_sample: 3,
    af3_correct: false,
    has_correct: false,
    n_clusters: 4,
    ts: 1785952800,
  });
  await backend.flush();

  assert.equal(writes[0].table, 'quiz_sessions');
  assert.equal(writes[0].value.user_id, 'user-1');
  assert.equal(writes[1].value.picked_sample, null);
  assert.equal(writes[1].value.answered_at, '2026-08-05T18:00:00.000Z');
  assert.equal(storage.getItem('foldariumSyncQueueV1'), '[]');
});

test('retains queued writes after a Supabase error and retries idempotently', async () => {
  const { client, setFailing } = fakeSupabase();
  const storage = memoryStorage();
  const backend = createQuizBackend({
    client,
    storage,
    uuid: sequenceUuid('session-id'),
    now: () => new Date('2026-08-05T18:00:00.000Z'),
  });

  setFailing(true);
  backend.startSession({ source: 'rnp', difficulty: 'easy' });
  await backend.flush();
  assert.notEqual(storage.getItem('foldariumSyncQueueV1'), '[]');

  setFailing(false);
  await backend.flush();
  assert.equal(storage.getItem('foldariumSyncQueueV1'), '[]');
});
```

The helper implementations in the same test file must mimic `localStorage`, deterministic UUID generation, anonymous `auth.getSession()`, and the `from().upsert()` / `from().update().eq()` calls used by the module.

- [ ] **Step 3: Run tests and verify the expected failure**

Run: `npm test`

Expected: FAIL because `quiz-backend.js` does not exist.

- [ ] **Step 4: Implement the minimal persistence module**

Create `quiz-backend.js` with:

```js
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
```

Keep the implementation dependency-free and do not expose queue internals.

- [ ] **Step 5: Run the tests**

Run: `npm test`

Expected: all persistence tests PASS.

- [ ] **Step 6: Commit**

```bash
git add package.json quiz-backend.js tests/quiz-backend.test.js
git commit -m "Add anonymous quiz persistence client"
```

---

### Task 2: Supabase schema and RLS

**Files:**
- Create: `supabase/migrations/20260805180000_create_quiz_results.sql`

**Interfaces:**
- Consumes: the `quiz_sessions` and `quiz_answers` payloads produced by `quiz-backend.js`
- Produces: append-only, owner-scoped Postgres storage available to the Supabase `authenticated` role

- [ ] **Step 1: Create the schema migration**

Create the migration with two tables, data checks, ownership indexes, and RLS:

```sql
create table public.quiz_sessions (
  id uuid primary key,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  source text not null check (source in ('cameo', 'rnp')),
  difficulty text not null check (difficulty in ('easy', 'hard')),
  started_at timestamptz not null,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  check (completed_at is null or completed_at >= started_at)
);

create table public.quiz_answers (
  id uuid primary key,
  session_id uuid not null references public.quiz_sessions(id) on delete cascade,
  question_index integer not null check (question_index >= 0),
  item_id text not null check (length(item_id) between 1 and 200),
  source text not null check (source in ('cameo', 'rnp')),
  difficulty text not null check (difficulty in ('easy', 'hard')),
  picked_none boolean not null,
  picked_sample integer,
  picked_correct boolean not null,
  picked_rmsd double precision check (picked_rmsd is null or picked_rmsd >= 0),
  af3_pick_sample integer,
  af3_correct boolean not null,
  has_correct boolean not null,
  n_clusters integer not null check (n_clusters > 0),
  answered_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (session_id, question_index),
  check (
    (picked_none and picked_sample is null and picked_rmsd is null)
    or (not picked_none and picked_sample is not null)
  )
);

create index quiz_sessions_user_started_idx
  on public.quiz_sessions (user_id, started_at desc);

alter table public.quiz_sessions enable row level security;
alter table public.quiz_answers enable row level security;

revoke all on public.quiz_sessions from anon, authenticated;
revoke all on public.quiz_answers from anon, authenticated;
grant select, insert on public.quiz_sessions to authenticated;
grant update (completed_at) on public.quiz_sessions to authenticated;
grant select, insert on public.quiz_answers to authenticated;

create policy "users select own sessions"
  on public.quiz_sessions for select to authenticated
  using (user_id = auth.uid());
create policy "users insert own sessions"
  on public.quiz_sessions for insert to authenticated
  with check (user_id = auth.uid());
create policy "users complete own sessions"
  on public.quiz_sessions for update to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

create policy "users select answers from own sessions"
  on public.quiz_answers for select to authenticated
  using (exists (
    select 1 from public.quiz_sessions
    where quiz_sessions.id = quiz_answers.session_id
      and quiz_sessions.user_id = auth.uid()
  ));
create policy "users insert answers into own sessions"
  on public.quiz_answers for insert to authenticated
  with check (exists (
    select 1 from public.quiz_sessions
    where quiz_sessions.id = quiz_answers.session_id
      and quiz_sessions.user_id = auth.uid()
  ));
```

- [ ] **Step 2: Validate the migration**

Run `npx supabase db lint --local` if a local Supabase stack is available.

Expected: no schema or policy errors. If no local stack is configured, run `git diff --check` and validate the migration in the Supabase SQL editor before production configuration.

- [ ] **Step 3: Commit**

```bash
git add supabase/migrations/20260805180000_create_quiz_results.sql
git commit -m "Add Supabase quiz result schema"
```

---

### Task 3: Quiz integration and setup documentation

**Files:**
- Create: `supabase-config.js`
- Modify: `index.html:130-184`
- Modify: `app.js:29-43,294-304,396-405,445-456`
- Modify: `README.md`
- Test: `tests/quiz-backend.test.js`

**Interfaces:**
- Consumes: `window.FOLDARIUM_SUPABASE`
- Consumes: `window.foldariumBackend`
- Produces: remote session and answer events without changing quiz behavior

- [ ] **Step 1: Add a configuration-shape test**

Extend `tests/quiz-backend.test.js` to verify `initQuizBackend` calls `createClient` with only public configuration and does not require a service key:

```js
test('configured initialization uses the publishable key', async () => {
  let received;
  const { client } = fakeSupabase();
  const backend = await initQuizBackend(
    { url: 'https://example.supabase.co', publishableKey: 'sb_publishable_test' },
    {
      createClient: (...args) => { received = args; return client; },
      storage: memoryStorage(),
      uuid: sequenceUuid('session-id'),
      now: () => new Date('2026-08-05T18:00:00.000Z'),
    },
  );
  assert.deepEqual(received.slice(0, 2), [
    'https://example.supabase.co',
    'sb_publishable_test',
  ]);
  assert.equal(backend.startSession({ source: 'cameo', difficulty: 'easy' }), 'session-id');
});
```

- [ ] **Step 2: Run the focused test**

Run: `npm test`

Expected: PASS after Task 1; this locks the runtime configuration contract before integration.

- [ ] **Step 3: Add public runtime configuration**

Create `supabase-config.js`:

```js
window.FOLDARIUM_SUPABASE = {
  url: '',
  publishableKey: '',
};
```

The empty defaults intentionally disable remote persistence. Supabase publishable keys are public browser credentials protected by RLS.

- [ ] **Step 4: Load the backend before the quiz application**

In `index.html`, after Mol* loads and before `app.js` loads:

```js
await loadScript('supabase-config.js');
const { initQuizBackend } = await import('./quiz-backend.js');
window.foldariumBackend = await initQuizBackend(window.FOLDARIUM_SUPABASE);
await loadScript('app.js');
```

Keep the existing error boundary so a missing local application asset is visible.

- [ ] **Step 5: Emit lifecycle events from the quiz**

In `app.js`, add one module-level session ID:

```js
let remoteSessionId = null;
```

At the start of `startQuiz()`:

```js
remoteSessionId = window.foldariumBackend?.startSession({
  source: quizSource,
  difficulty,
}) ?? null;
```

At the end of `logAnswer()`:

```js
window.foldariumBackend?.recordAnswer(remoteSessionId, idx, rec);
```

At the start of `finish()`:

```js
window.foldariumBackend?.completeSession(remoteSessionId);
```

Do not change dev mode, scoring, answer reveal, or the existing local log.

- [ ] **Step 6: Document Supabase setup**

Add a concise README section that instructs an operator to:

1. Create a Supabase project.
2. Enable anonymous sign-ins under Auth providers.
3. Apply `supabase/migrations/20260805180000_create_quiz_results.sql`.
4. Put the project URL and publishable key—not the service-role key—in `supabase-config.js`.
5. Deploy through the existing Vercel Git integration.

State that an empty config leaves the quiz local-only and that the browser identity is lost when site data is cleared.

- [ ] **Step 7: Verify**

Run:

```bash
npm test
git diff --check
curl -fsS http://127.0.0.1:8000/ >/dev/null
curl -fsS http://127.0.0.1:8000/quiz-backend.js >/dev/null
curl -fsS http://127.0.0.1:8000/supabase-config.js >/dev/null
```

Expected: tests pass, no whitespace errors, and all three local HTTP requests succeed. Open the local app with empty configuration and complete one answer; quiz behavior and `poseQuizLog` must remain unchanged.

- [ ] **Step 8: Commit**

```bash
git add app.js index.html README.md supabase-config.js tests/quiz-backend.test.js
git commit -m "Connect quiz lifecycle to Supabase persistence"
```
