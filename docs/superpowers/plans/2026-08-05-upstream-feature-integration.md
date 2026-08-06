# Upstream Feature Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the first six upstream feature groups to `dev` without regressing the local Supabase, Storage, password-gate, rebuild, or replay behavior.

**Architecture:** Copy upstream `f26254f` by subsystem. Reconcile the root quiz manually with local behavior, implement the leaderboard against Supabase, keep benchmark molecular files in Supabase Storage, and copy scientific pipelines unchanged.

**Tech Stack:** Vanilla JavaScript, Mol* 4.6, Node's built-in test runner, Supabase Postgres/Auth/Storage, Python preparation scripts.

## Global Constraints

- Keep implementation minimal and close to upstream.
- Preserve the password gate, queued Supabase writes, Storage resolver, serialized viewer rebuilds, and trace replay.
- Use a shared Supabase leaderboard with case-insensitive unique usernames and no local fallback.
- Keep benchmark manifests in Git and molecular PDB/CIF files out of Git.
- Do not modify upstream scientific thresholds or algorithms.
- Do not include `data_rnp_aligned/`, the GitHub Pages workflow, or legacy `app/`.

---

### Task 1: Quiz Grid and Session Rules

**Files:**
- Modify: `app.js`
- Modify: `index.html`
- Modify: `tests/viewer-trace.test.js`
- Create: `tests/quiz-upstream-features.test.js`

**Interfaces:**
- Produces: `drawSession(): object[]`
- Produces: `gridEntriesFor(method: string|null): GridEntry[]`
- Produces: Grid display mode through `button[data-m="grid"]`
- Preserves: `researchBackend()`, `viewerRebuild`, `revealAfterIdle`, and `viewerTraceRecorder`

- [ ] **Step 1: Add focused failing source-contract tests**

Create tests that read `app.js` and `index.html` and assert the presence of:

```js
const HARD_MIX = { 'game-able': 0.40, 'all-wrong': 0.45, 'all-correct': 0.15 };
function drawSession()
function gridEntriesFor(method)
const LABELS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
data-m="grid"
```

Also assert that local integration points remain:

```js
researchBackend()?.recordAnswer
viewerTraceRecorder?.stop()
window.foldariumAssetUrl
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
node --test tests/quiz-upstream-features.test.js tests/viewer-trace.test.js
```

Expected: the new upstream-feature assertions fail because Grid and `HARD_MIX` are absent.

- [ ] **Step 3: Port upstream quiz behavior**

Use `FETCH_HEAD:app.js` and `FETCH_HEAD:index.html` as the source for:

- expanded palette and A-Z labels;
- `HARD_MIX`, `drawSession`, and `easyPlayable`;
- Grid layout, method paging, viewer creation/disposal, synchronized cameras,
  loading states, and exact-choice selection;
- stage-filling Grid markup and CSS.

Retain local asset loading:

```js
const assetUrl = path => window.foldariumAssetUrl?.(path) || path;
```

Retain the local singleton rebuild path for non-Grid modes. Mirror the active
Grid camera snapshot into `plugin.canvas3d.camera` so the existing recorder sees
camera movement. Keep reveal serialized through `revealAfterIdle`; stop the
recorder before answer coloring and pass `viewer_trace` to `recordAnswer`.

- [ ] **Step 4: Update replay integration assertions**

Adjust only assertions invalidated by the Grid merge. Continue checking that
recording starts after a question is built, stops before reveal, excludes traces
from the local log, and sends traces only to Supabase.

- [ ] **Step 5: Run quiz and replay tests**

Run:

```bash
npm test
```

Expected: all tests pass.

- [ ] **Step 6: Commit quiz parity**

```bash
git add app.js index.html tests/viewer-trace.test.js tests/quiz-upstream-features.test.js
git commit -m "Add upstream grid and balanced quiz sessions"
```

---

### Task 2: Shared Supabase Leaderboard

**Files:**
- Create: `supabase/migrations/20260806040000_add_shared_leaderboard.sql`
- Modify: `quiz-backend.js`
- Modify: `index.html`
- Modify: `app.js`
- Create: `leaderboard.html`
- Modify: `tests/quiz-backend.test.js`

**Interfaces:**
- Produces: `backend.claimUsername(username): Promise<string>`
- Produces: `backend.getLeaderboard(): Promise<LeaderboardRow[]>`
- SQL RPC: `claim_leaderboard_username(p_username text)`
- SQL RPC: `get_leaderboard()`

- [ ] **Step 1: Add failing backend tests**

Extend the fake Supabase client tests to require:

```js
await backend.claimUsername('player_one');
const rows = await backend.getLeaderboard();
```

Verify that `claimUsername` calls `claim_leaderboard_username`, that
`getLeaderboard` calls `get_leaderboard`, and that RPC errors reject without a
local fallback.

- [ ] **Step 2: Run the focused backend tests and confirm failure**

Run:

```bash
node --test tests/quiz-backend.test.js
```

Expected: failure because the two methods do not exist.

- [ ] **Step 3: Add the database migration**

Create `leaderboard_profiles` keyed by `auth.users.id`, with a unique
case-insensitive username key. Add authenticated claim and public aggregate-read
RPCs. The aggregate must use the latest answer per `(user_id, source, item_id)`
from completed sessions and return only:

```text
username, items, sessions, accuracy, af3_accuracy, beat_af3_by
```

Set fixed `search_path` values on security-definer functions. Revoke direct
anonymous/authenticated access to profile rows and grant only RPC execution.

- [ ] **Step 4: Extend the quiz backend**

Add `claimUsername` and `getLeaderboard` to `createQuizBackend`,
`disabledBackend`, and the deferred facade in `index.html`. Reuse the existing
lazy Supabase client and throw clear errors when persistence is unavailable.

- [ ] **Step 5: Add completion and leaderboard UI**

Port the upstream completion form and standalone page, replacing localStorage
aggregation and `api/*` fetches with the two backend methods. On Save:

```js
researchBackend()?.completeSession(remoteSessionId);
await researchBackend().flush();
await researchBackend().claimUsername(username);
const rows = await researchBackend().getLeaderboard();
```

Keep the username editable after a uniqueness conflict. The standalone page
loads `supabase-config.js`, initializes the same backend, and renders shared
rows only.

- [ ] **Step 6: Run tests**

Run:

```bash
npm test
```

Expected: all tests pass.

- [ ] **Step 7: Commit the leaderboard**

```bash
git add supabase/migrations/20260806040000_add_shared_leaderboard.sql quiz-backend.js index.html app.js leaderboard.html tests/quiz-backend.test.js
git commit -m "Add shared Supabase leaderboard"
```

---

### Task 3: Benchmark Viewer and Storage Adapter

**Files:**
- Copy: `benchmark/README.md`
- Copy: `benchmark/app/**`
- Copy: `benchmark/prep/**`
- Copy: `benchmark/demo/app.js`
- Copy: `benchmark/demo/index.html`
- Copy: `benchmark/demo/systems.json`
- Copy: `benchmark/demo/systems_rnp.json`
- Modify: `scripts/upload-structures.mjs`
- Modify: `tests/upload-structures.test.js`
- Modify: `package.json`

**Interfaces:**
- Produces: `discoverStructureFiles(rootDir, benchmarkDir?): StructureFile[]`
- Accepts: `BENCHMARK_DEMO_DIR=/absolute/path/to/benchmark/demo`
- Stores: `structures/benchmark/demo/systems/**` and `structures/benchmark/demo/systems_rnp/**`

- [ ] **Step 1: Add failing uploader tests**

Add one temp-directory test containing a `.pdb`, `.cif`, and ignored `.txt`.
Require object keys under `benchmark/demo/` and these content types:

```js
{
  '.pdb': 'chemical/x-pdb',
  '.cif': 'chemical/x-cif',
}
```

- [ ] **Step 2: Run the focused uploader test and confirm failure**

Run:

```bash
node --test tests/upload-structures.test.js
```

Expected: failure because benchmark/CIF discovery is unsupported.

- [ ] **Step 3: Copy benchmark code and manifests**

Copy only the listed tracked code and manifest paths from `FETCH_HEAD`. Do not
copy `benchmark/demo/systems/` or `benchmark/demo/systems_rnp/`.

- [ ] **Step 4: Extend the existing uploader**

Rename the internal discovery path to structures rather than PDB-only while
keeping existing `data/` and `data_rnp/` behavior. When
`BENCHMARK_DEMO_DIR` is set, discover its `systems/` and `systems_rnp/` trees and
prefix keys with `benchmark/demo/`. Select content type from the extension.

Add:

```json
"upload:benchmark": "node scripts/upload-structures.mjs"
```

The documented invocation is:

```bash
BENCHMARK_DEMO_DIR=/path/to/benchmark/demo \
SUPABASE_URL=https://... \
SUPABASE_SERVICE_ROLE_KEY=... \
npm run upload:benchmark
```

- [ ] **Step 5: Point the demo at Storage**

Load `../../supabase-config.js` from the benchmark demo. Resolve relative
`systems/` and `systems_rnp/` molecular paths against:

```js
`${window.FOLDARIUM_SUPABASE.structureBaseUrl}/benchmark/demo`
```

Keep manifest fetches local. Show the upstream per-panel error when an object is
missing.

- [ ] **Step 6: Run uploader and repository tests**

Run:

```bash
npm test
```

Expected: all tests pass.

- [ ] **Step 7: Commit benchmark support**

```bash
git add benchmark scripts/upload-structures.mjs tests/upload-structures.test.js package.json
git commit -m "Add benchmark viewer with Supabase assets"
```

---

### Task 4: Preparation Pipelines and Final Verification

**Files:**
- Copy: `prep/**`
- Modify: `README.md`

**Interfaces:**
- Produces: upstream CAMEO scripts under `prep/cameo/`
- Produces: upstream Runs-n-Poses scripts under `prep/rnp/`

- [ ] **Step 1: Copy the upstream preparation pipeline**

Copy `prep/` verbatim from `FETCH_HEAD`. Do not reformat or alter scientific
logic.

- [ ] **Step 2: Document the integrated features**

Update the root README with Grid, balanced Hard sessions, the shared
leaderboard migration, benchmark demo location, Storage upload command, and
links to `prep/README.md` and `benchmark/README.md`.

- [ ] **Step 3: Compile copied Python**

Run:

```bash
python3 -m compileall -q prep benchmark/prep
```

Expected: exit code 0.

- [ ] **Step 4: Run final automated checks**

Run:

```bash
npm test
git diff --check
```

Expected: all Node tests pass and `git diff --check` exits 0.

- [ ] **Step 5: Perform one browser smoke test**

Serve the repository and verify:

```text
unlock → CAMEO Grid → linked camera → reveal → next
Runs-n-Poses Grid → method pages → exact pick → finish
claim unique username → shared leaderboard loads
recorded answer remains available in replay
benchmark demo loads manifests and reports missing Storage objects clearly
```

- [ ] **Step 6: Commit pipelines and documentation**

```bash
git add prep README.md
git commit -m "Add upstream preparation pipelines"
```

- [ ] **Step 7: Upload benchmark assets when credentials are available**

Materialize upstream `benchmark/demo/systems*` outside the repository, set
`BENCHMARK_DEMO_DIR`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY`, then run
`npm run upload:benchmark`. Expected summary: zero failed objects. If credentials
remain unavailable, report the upload as the only external deployment step
left; do not commit credentials or binaries.
