# Supabase Structure Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload all Foldarium PDB files to one public Supabase Storage bucket and make the quiz load them from stable CDN URLs while retaining local Git copies.

**Architecture:** Existing relative paths remain in the quiz manifests. A small resolver prefixes relative structure paths with a configured public Storage base URL. A dependency-free Node script creates the bucket and uploads `data/**/*.pdb` and `data_rnp/**/*.pdb`; `.vercelignore` omits those retained backup files from deployment.

**Tech Stack:** Browser JavaScript, Node.js built-in APIs, Supabase Storage REST, Node.js built-in test runner, static Vercel deployment.

## Global Constraints

- Use one public bucket named `structures`.
- Preserve repository-relative object keys.
- Keep all PDB files in Git.
- Exclude `data/` and `data_rnp/` from Vercel only after upload verification.
- Keep quiz manifests unchanged.
- Add no runtime package dependencies.
- Never commit or expose the Supabase server credential.
- Do not delete local or remote objects.

---

### Task 1: Structure URL resolution

**Files:**
- Create: `structure-assets.js`
- Create: `tests/structure-assets.test.js`
- Modify: `supabase-config.js`
- Modify: `index.html`
- Modify: `app.js`

**Interfaces:**
- Produces: `resolveAssetUrl(path, baseUrl) -> string`
- Produces: `window.foldariumAssetUrl(path) -> string`
- Consumes: `window.FOLDARIUM_SUPABASE.structureBaseUrl`

- [ ] **Step 1: Write failing resolver tests**

Create `tests/structure-assets.test.js`:

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveAssetUrl } from '../structure-assets.js';

test('prefixes relative structure paths with the Storage base URL', () => {
  assert.equal(
    resolveAssetUrl(
      'data/13NM/pose-1.pdb',
      'https://project.supabase.co/storage/v1/object/public/structures/',
    ),
    'https://project.supabase.co/storage/v1/object/public/structures/data/13NM/pose-1.pdb',
  );
});

test('leaves absolute URLs unchanged', () => {
  assert.equal(
    resolveAssetUrl('https://example.test/pose.pdb', 'https://storage.test'),
    'https://example.test/pose.pdb',
  );
});

test('uses local paths when Storage is unconfigured', () => {
  assert.equal(resolveAssetUrl('data/13NM/pose-1.pdb', ''), 'data/13NM/pose-1.pdb');
});
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `node --test tests/structure-assets.test.js`

Expected: FAIL because `structure-assets.js` does not exist.

- [ ] **Step 3: Implement the resolver**

Create `structure-assets.js`:

```js
export function resolveAssetUrl(path, baseUrl = '') {
  if (!path || /^https?:\/\//i.test(path) || !baseUrl) return path;
  return `${baseUrl.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
}

if (typeof window !== 'undefined') {
  window.foldariumAssetUrl = path => resolveAssetUrl(
    path,
    window.FOLDARIUM_SUPABASE?.structureBaseUrl || '',
  );
}
```

- [ ] **Step 4: Configure and load the resolver**

Add this public value to `supabase-config.js`:

```js
structureBaseUrl: 'https://wwentnogbknrbmxhfgbg.supabase.co/storage/v1/object/public/structures',
```

In `index.html`, import `structure-assets.js` before loading `app.js`. Keep persistence startup non-blocking. The resolver reads configuration at call time, so config and persistence may continue loading asynchronously.

- [ ] **Step 5: Route both structure-loading paths through Storage**

In `app.js`, add:

```js
const assetUrl = path => window.foldariumAssetUrl?.(path) || path;
```

Use `assetUrl(url)` in `loadStruct()` and `fetchPdbText()`. Do not change manifest loading, scoring, Mol* representation logic, or H-bond construction.

- [ ] **Step 6: Run tests**

Run: `npm test`

Expected: all existing and resolver tests PASS.

- [ ] **Step 7: Commit**

```bash
git add structure-assets.js tests/structure-assets.test.js supabase-config.js index.html app.js
git commit -m "Load molecular structures from Supabase Storage"
```

---

### Task 2: Dependency-free upload script

**Files:**
- Create: `scripts/upload-structures.mjs`
- Create: `tests/upload-structures.test.js`
- Modify: `package.json`
- Modify: `README.md`

**Interfaces:**
- Produces: `discoverPdbFiles(rootDir) -> Promise<Array<{ absolutePath, objectKey }>>`
- Produces: `ensurePublicBucket({ fetchImpl, url, key }) -> Promise<void>`
- Produces: `uploadStructures({ files, fetchImpl, url, key, overwrite, concurrency }) -> Promise<Summary>`
- Produces: CLI command `npm run upload:structures`

- [ ] **Step 1: Write failing upload-script tests**

Create `tests/upload-structures.test.js` using temporary directories and a fake `fetch` implementation. Cover:

```js
test('discovers only PDB files and preserves repository-relative keys', async () => {
  // Create data/A/pose-1.pdb, data/A/readme.txt, and data_rnp/B/protein.pdb.
  const files = await discoverPdbFiles(tempRoot);
  assert.deepEqual(files.map(file => file.objectKey), [
    'data/A/pose-1.pdb',
    'data_rnp/B/protein.pdb',
  ]);
});

test('creates a missing public structures bucket', async () => {
  // Fake GET /storage/v1/bucket/structures as 404 and POST /storage/v1/bucket as 200.
  await ensurePublicBucket({ fetchImpl, url: 'https://project.test', key: 'secret' });
  assert.deepEqual(JSON.parse(createRequest.body), {
    id: 'structures',
    name: 'structures',
    public: true,
  });
});

test('counts successful and existing uploads without failing', async () => {
  const summary = await uploadStructures({ files, fetchImpl, url, key, overwrite: false, concurrency: 2 });
  assert.deepEqual(summary, { uploaded: 1, skipped: 1, failed: [] });
});

test('reports failed object keys and rejects the CLI run', async () => {
  const summary = await uploadStructures({ files, fetchImpl, url, key, overwrite: false, concurrency: 2 });
  assert.deepEqual(summary.failed, ['data/A/broken.pdb']);
});
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `node --test tests/upload-structures.test.js`

Expected: FAIL because `scripts/upload-structures.mjs` does not exist.

- [ ] **Step 3: Implement discovery and bucket creation**

Use only `node:fs/promises`, `node:path`, and built-in `fetch`. Recursively walk `data/` and `data_rnp/`, sort keys, and normalize separators to `/`.

`ensurePublicBucket()` must:

- `GET /storage/v1/bucket/structures`;
- return when the response is OK;
- on `404`, `POST /storage/v1/bucket` with `{ id, name, public: true }`;
- reject any other response with a concise status/message.

Send both `apikey` and `Authorization: Bearer <key>` headers.

- [ ] **Step 4: Implement bounded uploads**

Upload each file with:

```text
POST /storage/v1/object/structures/<encoded object path>
Content-Type: chemical/x-pdb
cache-control: max-age=31536000
x-upsert: true|false
```

Run at most six uploads concurrently by default. Treat an existing-object response as skipped when overwrite is false. Collect other failed keys without stopping remaining uploads.

The CLI must require `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`, support `--overwrite`, print local/uploaded/skipped/failed counts, and set a nonzero exit code if any file fails.

- [ ] **Step 5: Add the command and documentation**

Add to `package.json`:

```json
"upload:structures": "node scripts/upload-structures.mjs"
```

Document:

```bash
SUPABASE_URL=https://... \
SUPABASE_SERVICE_ROLE_KEY=... \
npm run upload:structures
```

State that the server credential must remain uncommitted and that rerunning without `--overwrite` is safe.

- [ ] **Step 6: Run tests and static checks**

Run:

```bash
npm test
node --check scripts/upload-structures.mjs
git diff --check
```

Expected: all tests and checks PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/upload-structures.mjs tests/upload-structures.test.js package.json README.md
git commit -m "Add Supabase structure upload utility"
```

---

### Task 3: Upload verification and Vercel exclusion

**Files:**
- Create: `.vercelignore`
- Modify: `README.md`

**Interfaces:**
- Consumes: the Task 2 CLI and local Supabase server credential
- Produces: a verified public `structures` bucket containing all local PDBs

- [ ] **Step 1: Count local source files**

Run the upload script without `--overwrite`. Its `local` count is the authoritative discovered-file count.

Expected: 1,977 PDB files discovered (1,403 under `data/`, 574 under `data_rnp/`).

- [ ] **Step 2: Upload all structures**

Run:

```bash
SUPABASE_URL=https://wwentnogbknrbmxhfgbg.supabase.co \
SUPABASE_SERVICE_ROLE_KEY="$SUPABASE_SERVICE_ROLE_KEY" \
npm run upload:structures
```

Expected: zero failed uploads. Uploaded plus skipped must equal 1,977.

- [ ] **Step 3: Verify representative public objects**

Fetch at least:

```text
/storage/v1/object/public/structures/data/13NM/protein.pdb
/storage/v1/object/public/structures/data/13NM/pocket.pdb
/storage/v1/object/public/structures/data/13NM/pose-1.pdb
/storage/v1/object/public/structures/data_rnp/7w6z__1__1_A__1_B_1_C__1_C/protein.pdb
/storage/v1/object/public/structures/data_rnp/7w6z__1__1_A__1_B_1_C__1_C/pose-0.pdb
```

Expected: HTTP 200 and PDB-like text content for every object.

- [ ] **Step 4: Exclude backup files from Vercel**

Create `.vercelignore`:

```text
data/
data_rnp/
```

Add a README note that Git retains the files but production loads them from Supabase Storage.

- [ ] **Step 5: Verify the quiz against Storage**

Run the local server and complete representative CAMEO and Runs-n-Poses question loads. Enable H-bonds on one question to verify raw PDB fetches use Storage.

Run:

```bash
npm test
git diff --check
```

Expected: all tests pass and no whitespace errors.

- [ ] **Step 6: Commit**

```bash
git add .vercelignore README.md
git commit -m "Exclude Storage-backed structures from Vercel"
```
