# Mol* Snapshot Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save a timestamped Mol* data-tree/camera trace with each quiz answer and provide a password-protected page that replays one answer at a time.

**Architecture:** A dependency-free recorder captures an initial data-tree snapshot, later scene snapshots, and debounced camera endpoints. The trace is stored in `quiz_answers.viewer_trace`. A single Vercel Function verifies a shared environment password and reads traces with a server-only Supabase credential; a static replay page applies entries to Mol* 4.6.0.

**Tech Stack:** Browser JavaScript modules, Mol* 4.6.0, Supabase Postgres/REST, Vercel Node Functions, Node.js built-in test runner.

## Global Constraints

- Store JSON only; do not capture images or video.
- Record from question load through answer lock, before correctness reveal.
- Store at most 100 entries and mark truncated traces.
- Keep quiz play functional when recording or persistence fails.
- Preserve the existing answer retry/dead-letter behavior and `poseQuizLog`.
- Replay only one selected answer at a time.
- Keep `REPLAY_PASSWORD`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` server-only.
- Add no runtime package dependencies.
- Keep Mol* pinned to version `4.6.0`.

---

### Task 1: Mol* trace recorder

**Files:**
- Create: `viewer-trace.js`
- Create: `tests/viewer-trace.test.js`

**Interfaces:**
- Produces: `createViewerTraceRecorder({ plugin, now?, setTimer?, clearTimer?, settleMs?, maxEntries? })`
- Produces: recorder methods `start()`, `captureState()`, and `stop()`
- Produces: version-1 JSON trace with `molstar_version`, `duration_ms`, `truncated`, and `snapshots`

- [ ] **Step 1: Write failing recorder tests**

Create `tests/viewer-trace.test.js` using a fake Mol* plugin. Cover:

```js
test('captures an initial data-tree snapshot and a settled camera endpoint', () => {
  const clock = fakeClock();
  const plugin = fakePlugin();
  const recorder = createViewerTraceRecorder({
    plugin,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    settleMs: 300,
  });

  recorder.start();
  clock.advance(100);
  plugin.cameraChanged();
  clock.advance(299);
  assert.equal(plugin.cameraReads, 0);
  clock.advance(1);
  assert.equal(plugin.cameraReads, 1);

  const trace = recorder.stop();
  assert.equal(trace.snapshots[0].kind, 'state');
  assert.ok(trace.snapshots[0].snapshot.data);
  assert.equal(trace.snapshots[1].kind, 'camera');
  assert.equal(trace.snapshots[1].t_ms, 400);
});

test('captureState records rebuilt scenes and stop prevents later captures', () => {
  recorder.start();
  recorder.captureState();
  plugin.cameraChanged();
  const trace = recorder.stop();
  plugin.cameraChanged();
  clock.advance(300);
  assert.deepEqual(trace.snapshots.map(entry => entry.kind), ['state', 'state', 'camera']);
});

test('marks the trace truncated at 100 entries', () => {
  const recorder = createViewerTraceRecorder({ plugin, maxEntries: 100 });
  recorder.start();
  for (let index = 0; index < 150; index++) recorder.captureState();
  const trace = recorder.stop();
  assert.equal(trace.snapshots.length, 100);
  assert.equal(trace.truncated, true);
});

test('capture failures are skipped without throwing', () => {
  plugin.state.getSnapshot = () => { throw new Error('snapshot failed'); };
  assert.doesNotThrow(() => recorder.start());
  assert.equal(recorder.stop().snapshots.length, 0);
});
```

The test-only fake exposes its snapshot count through the recorder only if needed for the settle assertion; prefer asserting the final trace where possible.

- [ ] **Step 2: Run tests and verify RED**

Run: `node --test tests/viewer-trace.test.js`

Expected: FAIL because `viewer-trace.js` does not exist.

- [ ] **Step 3: Implement the recorder**

Create `viewer-trace.js` with:

```js
const SNAPSHOT_PARAMS = {
  data: true,
  behavior: false,
  componentManager: false,
  animation: false,
  startAnimation: false,
  canvas3d: false,
  canvas3dContext: false,
  interactivity: false,
  structureSelection: false,
  camera: true,
  cameraTransition: { name: 'animate', params: { durationInMs: 250 } },
};

export function createViewerTraceRecorder({
  plugin,
  now = () => performance.now(),
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  settleMs = 300,
  maxEntries = 100,
}) {
  let active = false;
  let startedAt = 0;
  let snapshots = [];
  let truncated = false;
  let cameraTimer = null;

  const append = entry => {
    if (!active) return;
    if (snapshots.length >= maxEntries) {
      truncated = true;
      return;
    }
    snapshots.push({ t_ms: Math.max(0, Math.round(now() - startedAt)), ...entry });
  };

  const captureState = () => {
    try {
      const snapshot = plugin.state.getSnapshot(SNAPSHOT_PARAMS);
      delete snapshot.structureFocus;
      append({ kind: 'state', snapshot });
    } catch (error) {
      console.warn('Viewer snapshot skipped:', error.message);
    }
  };

  const captureCamera = () => {
    try {
      append({ kind: 'camera', camera: plugin.canvas3d.camera.getSnapshot() });
    } catch (error) {
      console.warn('Viewer camera snapshot skipped:', error.message);
    }
  };

  const cameraSubscription = plugin.canvas3d.camera.changed.subscribe(() => {
    if (!active) return;
    if (cameraTimer !== null) clearTimer(cameraTimer);
    cameraTimer = setTimer(() => {
      cameraTimer = null;
      captureCamera();
    }, settleMs);
  });

  return {
    start() {
      active = true;
      startedAt = now();
      snapshots = [];
      truncated = false;
      if (cameraTimer !== null) clearTimer(cameraTimer);
      cameraTimer = null;
      captureState();
    },
    captureState,
    stop() {
      if (cameraTimer !== null) {
        clearTimer(cameraTimer);
        cameraTimer = null;
        captureCamera();
      }
      active = false;
      return Object.freeze({
        version: 1,
        molstar_version: '4.6.0',
        duration_ms: Math.max(0, Math.round(now() - startedAt)),
        truncated,
        snapshots: snapshots.slice(),
      });
    },
    dispose() {
      cameraSubscription.unsubscribe();
    },
  };
}
```

Adjust the exact implementation to satisfy tests without adding semantic event logging or image capture.

- [ ] **Step 4: Run recorder tests**

Run: `node --test tests/viewer-trace.test.js`

Expected: all recorder tests PASS with no unexpected output.

- [ ] **Step 5: Commit**

```bash
git add viewer-trace.js tests/viewer-trace.test.js
git commit -m "Add Molstar viewer trace recorder"
```

---

### Task 2: Persist traces with answers

**Files:**
- Create: `supabase/migrations/20260805230000_add_viewer_trace.sql`
- Modify: `quiz-backend.js`
- Modify: `tests/quiz-backend.test.js`

**Interfaces:**
- Consumes: `record.viewer_trace`
- Produces: nullable `quiz_answers.viewer_trace jsonb`

- [ ] **Step 1: Add failing backend tests**

Extend `tests/quiz-backend.test.js`:

```js
test('persists a serializable viewer trace with the answer', async () => {
  const trace = {
    version: 1,
    molstar_version: '4.6.0',
    duration_ms: 500,
    truncated: false,
    snapshots: [],
  };
  backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: trace }));
  await backend.flush();
  assert.deepEqual(answerWrite(writes).value.viewer_trace, trace);
});

test('stores the answer without a cyclic viewer trace', async () => {
  const trace = {};
  trace.self = trace;
  backend.recordAnswer('session-id', 0, answerRecord({ viewer_trace: trace }));
  await backend.flush();
  assert.equal(answerWrite(writes).value.viewer_trace, null);
});
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `node --test tests/quiz-backend.test.js`

Expected: viewer trace assertion fails because the payload omits `viewer_trace`.

- [ ] **Step 3: Normalize the trace in `quiz-backend.js`**

Before building the answer payload:

```js
let viewerTrace = record.viewer_trace ?? null;
try {
  if (viewerTrace !== null) JSON.stringify(viewerTrace);
} catch (error) {
  console.warn('Viewer trace omitted:', error.message);
  viewerTrace = null;
}
```

Include `viewer_trace: viewerTrace` in the queued answer object. Do not add traces to session or completion operations.

- [ ] **Step 4: Add the migration**

Create `supabase/migrations/20260805230000_add_viewer_trace.sql`:

```sql
alter table public.quiz_answers
  add column viewer_trace jsonb;

alter table public.quiz_answers
  add constraint quiz_answers_viewer_trace_shape
  check (
    viewer_trace is null
    or (
      jsonb_typeof(viewer_trace) = 'object'
      and viewer_trace ->> 'version' = '1'
      and jsonb_typeof(viewer_trace -> 'snapshots') = 'array'
    )
  );
```

Do not change RLS grants or policies.

- [ ] **Step 5: Run tests and migration checks**

Run:

```bash
npm test
git diff --check
```

Expected: all tests PASS and no whitespace errors. Run `npx supabase db lint --local` only if a local Supabase stack is already available; otherwise validate this migration in the connected Supabase SQL editor before deployment.

- [ ] **Step 6: Commit**

```bash
git add quiz-backend.js tests/quiz-backend.test.js supabase/migrations/20260805230000_add_viewer_trace.sql
git commit -m "Persist viewer traces with quiz answers"
```

---

### Task 3: Password-checked replay API

**Files:**
- Create: `api/replay.js`
- Create: `tests/replay-api.test.js`

**Interfaces:**
- Produces: Vercel handler `default async function handler(request, response)`
- Accepts: POST body `{ password, action: 'sessions' }`
- Accepts: POST body `{ password, action: 'answers', session_id }`

- [ ] **Step 1: Write failing API tests**

Create `tests/replay-api.test.js` around an exported `createReplayHandler({ env, fetchImpl })`. Cover:

```js
test('rejects an invalid replay password without calling Supabase', async () => {
  const fetchImpl = failIfCalled();
  const response = await invoke(handler, {
    password: 'wrong',
    action: 'sessions',
  });
  assert.equal(response.statusCode, 401);
  assert.equal(response.headers['Cache-Control'], 'no-store');
});

test('lists recent sessions with the server credential', async () => {
  const fetchImpl = recordingFetch([{ id: 'session-1' }]);
  const response = await invoke(handler, {
    password: 'correct horse',
    action: 'sessions',
  });
  assert.equal(response.statusCode, 200);
  assert.match(fetchImpl.url, /quiz_sessions/);
  assert.equal(fetchImpl.headers.apikey, 'service-key');
  assert.doesNotMatch(response.body, /service-key|correct horse/);
});

test('validates the session UUID before requesting answers', async () => {
  const response = await invoke(handler, {
    password: 'correct horse',
    action: 'answers',
    session_id: 'not-a-uuid',
  });
  assert.equal(response.statusCode, 400);
});

test('returns traced answers ordered by question index', async () => {
  const response = await invoke(handler, {
    password: 'correct horse',
    action: 'answers',
    session_id: '00000000-0000-4000-8000-000000000001',
  });
  assert.equal(response.statusCode, 200);
  assert.match(fetchImpl.url, /viewer_trace=not\\.is\\.null/);
  assert.match(fetchImpl.url, /order=question_index\\.asc/);
});
```

Also cover non-POST requests, malformed bodies, unsupported actions, missing environment variables, and sanitized upstream errors.

- [ ] **Step 2: Run API tests and verify RED**

Run: `node --test tests/replay-api.test.js`

Expected: FAIL because `api/replay.js` does not exist.

- [ ] **Step 3: Implement the Vercel handler**

Create `api/replay.js` with:

```js
import { createHash, timingSafeEqual } from 'node:crypto';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function createReplayHandler({ env = process.env, fetchImpl = fetch } = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');
    if (request.method !== 'POST') return send(response, 405, { error: 'Method not allowed' });

    const body = typeof request.body === 'string' ? safeJson(request.body) : request.body;
    if (!body || typeof body.password !== 'string') {
      return send(response, 400, { error: 'Invalid request' });
    }

    const { REPLAY_PASSWORD, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY } = env;
    if (!REPLAY_PASSWORD || !SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
      return send(response, 500, { error: 'Replay service is not configured' });
    }
    if (!secureEqual(body.password, REPLAY_PASSWORD)) {
      return send(response, 401, { error: 'Invalid password' });
    }

    let path;
    if (body.action === 'sessions') {
      path = '/rest/v1/quiz_sessions?select=id,user_id,source,difficulty,started_at,completed_at'
        + '&order=started_at.desc&limit=100';
    } else if (body.action === 'answers' && UUID.test(body.session_id || '')) {
      const id = encodeURIComponent(body.session_id);
      path = '/rest/v1/quiz_answers?select=id,session_id,question_index,item_id,picked_none,'
        + 'picked_sample,picked_correct,answered_at,viewer_trace'
        + `&session_id=eq.${id}&viewer_trace=not.is.null&order=question_index.asc`;
    } else {
      return send(response, 400, { error: 'Invalid action' });
    }

    try {
      const upstream = await fetchImpl(`${SUPABASE_URL}${path}`, {
        headers: {
          apikey: SUPABASE_SERVICE_ROLE_KEY,
          Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
        },
      });
      if (!upstream.ok) return send(response, 502, { error: 'Replay data unavailable' });
      return send(response, 200, await upstream.json());
    } catch {
      return send(response, 502, { error: 'Replay data unavailable' });
    }
  };
}

function secureEqual(left, right) {
  const digest = value => createHash('sha256').update(value).digest();
  return timingSafeEqual(digest(left), digest(right));
}

function safeJson(value) {
  try { return JSON.parse(value); } catch { return null; }
}

function send(response, status, value) {
  return response.status(status).json(value);
}

export default createReplayHandler();
```

Keep error responses generic and never echo request bodies or environment values.

- [ ] **Step 4: Run API tests**

Run: `node --test tests/replay-api.test.js`

Expected: all API tests PASS.

- [ ] **Step 5: Commit**

```bash
git add api/replay.js tests/replay-api.test.js
git commit -m "Add password-protected replay API"
```

---

### Task 4: Quiz capture and single-answer replay UI

**Files:**
- Create: `replay-player.js`
- Create: `replay.js`
- Create: `replay.html`
- Create: `tests/replay-player.test.js`
- Modify: `index.html`
- Modify: `app.js`
- Modify: `README.md`
- Test: `tests/viewer-trace.test.js`

**Interfaces:**
- Consumes: `window.createViewerTraceRecorder`
- Produces: `playViewerTrace(plugin, trace, options?)`
- Consumes: `/api/replay` actions `sessions` and `answers`

- [ ] **Step 1: Write failing player tests**

Create `tests/replay-player.test.js`:

```js
test('applies state and camera entries in timestamp order', async () => {
  const calls = [];
  const plugin = fakeReplayPlugin(calls);
  const clock = fakeAsyncClock();
  await playViewerTrace(plugin, {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [
      { t_ms: 0, kind: 'state', snapshot: { data: {} } },
      { t_ms: 100, kind: 'camera', camera: { zoom: 2 } },
    ],
  }, clock.options);
  assert.deepEqual(calls, ['state', 'camera']);
  assert.deepEqual(clock.waits, [100]);
});

test('rejects unsupported traces before mutating the viewer', async () => {
  await assert.rejects(
    playViewerTrace(plugin, { version: 2, molstar_version: '4.6.0', snapshots: [] }),
    /Unsupported viewer trace/,
  );
  assert.deepEqual(calls, []);
});

test('stops playback when aborted', async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(playViewerTrace(plugin, trace, { signal: controller.signal }), /aborted/i);
});
```

- [ ] **Step 2: Run player tests and verify RED**

Run: `node --test tests/replay-player.test.js`

Expected: FAIL because `replay-player.js` does not exist.

- [ ] **Step 3: Implement `replay-player.js`**

Export `playViewerTrace(plugin, trace, { now, sleep, signal } = {})`. Validate version `1`, Mol* version `4.6.0`, sorted non-negative timestamps, and entry shapes. For each entry:

- wait until its relative `t_ms`, subtracting time already spent loading earlier states;
- call `await plugin.state.setSnapshot(entry.snapshot)` for `state`;
- call `plugin.canvas3d.camera.setState(entry.camera, 250)` for `camera`;
- throw an `AbortError` when cancelled.

Do not evaluate code from trace data or fetch anything outside Mol* snapshot restoration.

- [ ] **Step 4: Integrate trace capture into the quiz**

In `index.html`, import `viewer-trace.js` before loading `app.js`, but catch import failure so quiz startup remains non-blocking:

```js
try {
  const module = await import('./viewer-trace.js');
  window.createViewerTraceRecorder = module.createViewerTraceRecorder;
} catch (error) {
  console.warn('Viewer recording disabled:', error.message);
}
await loadScript('app.js');
```

In `app.js`:

- create one recorder after Mol* plugin initialization when the factory exists and `DEV` is false;
- call `start()` after each new question finishes its initial `buildLayer()`;
- call `captureState()` after every awaited scene rebuild caused by display mode, pose navigation, clustering, protein mode, or H-bond visibility;
- at the first line of `reveal()`, call `stop()` before setting `cur.revealed`;
- pass the frozen trace separately to `logAnswer`;
- keep `poseQuizLog` unchanged and pass `{ ...rec, viewer_trace: trace }` only to `recordAnswer`;
- do not record dev mode or post-reveal actions.

Prevent recorder capture during answer reveal by stopping before the reveal rebuild.

- [ ] **Step 5: Create the replay page**

`replay.html` loads Mol* 4.6.0 CSS/JS and `replay.js`. It contains:

- password input and Connect button;
- recent-session selector;
- traced-answer selector;
- Play and Stop buttons;
- status/error text;
- a Mol* viewer container.

`replay.js`:

- stores the password in a module variable only;
- POSTs JSON to `/api/replay`;
- renders option text with `textContent`, never `innerHTML`;
- creates one Mol* viewer using the same minimal control options as the quiz;
- aborts current playback before starting another;
- clears the plugin before applying a selected trace;
- imports and calls `playViewerTrace`.

- [ ] **Step 6: Document setup**

Extend `README.md` with:

1. Apply `supabase/migrations/20260805230000_add_viewer_trace.sql`.
2. Set Vercel variables `REPLAY_PASSWORD`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY`.
3. Use a strong replay password; do not put it or the server credential in browser files.
4. Open `/replay.html`, enter the password, select a session, then select and play one answer.
5. Note the deliberately minimal shared-password limitations.

- [ ] **Step 7: Run full verification**

Run:

```bash
npm test
node --check app.js
node --check replay.js
node --check replay-player.js
node --check viewer-trace.js
git diff --check
curl -fsS http://127.0.0.1:8000/ >/dev/null
curl -fsS http://127.0.0.1:8000/replay.html >/dev/null
curl -fsS http://127.0.0.1:8000/viewer-trace.js >/dev/null
```

Expected: all tests and syntax checks pass, no whitespace errors, and static assets return HTTP 200. Manually verify with empty replay environment values that the quiz remains playable and the replay page shows a generic configuration error.

- [ ] **Step 8: Commit**

```bash
git add app.js index.html README.md replay.html replay.js replay-player.js viewer-trace.js tests
git commit -m "Record and replay Molstar answer traces"
```
