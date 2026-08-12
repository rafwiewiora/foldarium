# Foldarium Minimal — co-folding pose-triage quiz

## What's included

| Path | Role |
|------|------|
| `index.html`, `app.js` | Quiz UI + Mol* viewer |
| `quiz_items*.json` | Novel-only item manifests |
| `data/`, `data_rnp/` | Per-item pocket/pose PDBs |

## Modes

- **Easy** — ensembles that contain a correct pose; pick it.
- **Hard** — pick the correct pose or **"none of these"** (class-balanced sessions).
- **CAMEO** / **Runs-n-Poses** — prospective AF3 vs multi-method retrospective poses, with a Grid for comparing candidates.

## Benchmark demo and preparation pipelines

- The training-similarity benchmark viewer and its static demo live under
  [`benchmark/`](benchmark/README.md); see its README for how to serve the demo.
- Upstream CAMEO and Runs-n-Poses preparation scripts are in
  [`prep/`](prep/README.md).

## Supabase quiz persistence

To enable remote quiz-result persistence:

1. Create a Supabase project.
2. Enable anonymous sign-ins under Auth providers.
3. Apply `supabase/migrations/20260805180000_create_quiz_results.sql`.
4. Apply `supabase/migrations/20260806040000_add_shared_leaderboard.sql`.
5. Configure the browser-safe Vercel runtime variables described below; do not put credentials intended for privileged server-side access in browser configuration.
6. Before production, run live RLS checks with two anonymous accounts: verify own writes succeed, cross-user session updates and answer inserts fail, and answer updates/deletes fail. This is a required pre-production check.
7. Deploy through the existing Vercel Git integration.

If the runtime browser configuration is absent or invalid, the quiz stays local-only. The anonymous browser identity is lost when site data is cleared.

### Environment-isolated browser configuration

`supabase-config.js` loads browser-safe settings from `/api/config`. The endpoint
selects a separate variable namespace from Vercel's `VERCEL_ENV`; it never reads
`SUPABASE_SERVICE_ROLE_KEY`, `REPLAY_PASSWORD`, or the server-only `SUPABASE_URL`.

Set these variables for Production:

- `FOLDARIUM_PRODUCTION_SUPABASE_URL`
- `FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY` (or the legacy
  `FOLDARIUM_PRODUCTION_SUPABASE_ANON_KEY`)
- Optional: `FOLDARIUM_PRODUCTION_STRUCTURE_BASE_URL`; when omitted, the public
  `structures` bucket URL is derived from the project URL.

Preview is deliberately disabled unless a separate staging project is configured
with all of the following:

- `FOLDARIUM_PREVIEW_SUPABASE_URL`
- `FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY` (or the legacy
  `FOLDARIUM_PREVIEW_SUPABASE_ANON_KEY`)
- `FOLDARIUM_PREVIEW_WRITES_ENABLED=1`
- Optional: `FOLDARIUM_PREVIEW_STRUCTURE_BASE_URL`

Do not point the Preview variables at Production. Without the complete Preview
configuration and explicit opt-in, Preview remains local-only and cannot write
quiz or analytics data. Local development uses the corresponding
`FOLDARIUM_DEVELOPMENT_*` names and likewise requires
`FOLDARIUM_DEVELOPMENT_WRITES_ENABLED=1`.

The standalone shared leaderboard is available at [`leaderboard.html`](leaderboard.html).

### Leaderboard score integrity

The shared leaderboard is a privacy-safe aggregate, not a tamper-resistant scoring system. Existing RLS intentionally lets each authenticated anonymous client submit its own answer rows, including `picked_correct` and `af3_correct`; this task has no canonical server-side answer catalog from which to recompute those fields. The leaderboard hides raw answers and identifiers, but its scores should be treated as client-reported results for trusted research participants, not verified competitive rankings.

## Uploading structure files

Upload all PDB files in `data/` and `data_rnp/` to the public `structures` Storage bucket:

```bash
SUPABASE_URL=https://... \
SUPABASE_SERVICE_ROLE_KEY=... \
npm run upload:structures
```

Keep the server credential uncommitted. Rerunning the command without `--overwrite` is safe: existing objects are skipped. Pass `-- --overwrite` to replace existing objects.

Production loads structures from the public Supabase Storage URL supplied by the
runtime configuration endpoint. The PDB files remain in Git as a backup, while
`.vercelignore` excludes `data/` and `data_rnp/` from Vercel deployments.

## Uploading benchmark demo assets

The benchmark demo structures are not committed, so they must be uploaded before
the demo is functional. Materialize the demo's `systems*` files outside this
repository, then upload them to the public `structures` bucket:

```bash
BENCHMARK_DEMO_DIR=/path/to/benchmark/demo \
SUPABASE_URL=https://... \
SUPABASE_SERVICE_ROLE_KEY=... \
npm run upload:benchmark
```

Keep the service credential and generated benchmark assets uncommitted.

## Replaying recorded answers

1. Apply `supabase/migrations/20260805230000_add_viewer_trace.sql` to the Supabase project.
2. Set `REPLAY_PASSWORD`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` in the Vercel project environment.
3. Use a strong, unique replay password. Keep both that password and the server credential out of browser files, including `supabase-config.js`.
4. Open `/replay.html`, enter the password, select a recent session, then select and play one traced answer.

Replay access deliberately uses one shared password. It has no individual replay accounts, per-user authorization, audit trail, or built-in rate limiting; anyone with the shared password can read every replay exposed by the endpoint. Use it only for a small trusted audience and rotate the password if it is disclosed.

### Continuous weekly thinking traces

Apply `supabase/migrations/20260811192000_add_weekly_thinking_trace_batches.sql`
after the named-research migration to retain weekly interactions independently of
vote submission. The browser saves bounded append-only batches locally before
uploading them every five seconds and at navigation, vote, visibility, and
completion boundaries. Failed uploads remain in IndexedDB and reuse the same
idempotency key and payload after reconnect or refresh. Server-side analysis must
read `replay_weekly_trace_batches_safe`; the browser has RPC-only append access and
cannot read or alter stored batches.

Apply `supabase/migrations/20260812010000_deduplicate_weekly_vote_traces.sql`
after the continuous-trace migrations. New weekly vote revisions bind their compact
app state to a continuous `visit_id` and exact sequence boundary rather than storing
a second full viewer trace. The vote choice, rejection/selection state, active pane,
and optional `vote_comment` remain append-only on the revision, while
`weekly_quiz_votes` remains the latest-vote projection. Existing weekly rows with a
legacy `viewer_trace` and all classic quiz answer traces remain unchanged and
replayable. If both IndexedDB and the trace server are unavailable at the vote
boundary, the client retains the legacy snapshot on that vote as a loss-prevention
fallback.

The vote checkpoint waits for local IndexedDB durability, not the trace network
request. Retryable offline/network failures remain queued idempotently. A
permanently invalid batch is dead-lettered visibly so it cannot block later
batches, and its vote falls back to the legacy snapshot. The local queue is
bounded at 256 batches; reaching that bound also fails closed to the legacy vote
snapshot. Version-2 batches use a conservative 300 KiB client cap below the
database's 480 KiB `jsonb::text` cap, contiguous zero-based sequence numbers, explicit
omission markers, Mol* version, monotonic visit time, and visit ordinal. Replay
marks any omission or unexplained legacy gap incomplete rather than silently
claiming fidelity.

`visibilitychange` normally provides time to persist the final batch before a tab
closes. Browsers do not guarantee completion of asynchronous IndexedDB work begun
only in the final `pagehide` window; a page killed without an earlier visibility
transition can therefore lose that last unsubmitted interaction window. A
submitted vote remains safe because it awaits its local checkpoint (or embeds the
legacy snapshot fallback) before the vote RPC.

For the dry-run-only cold archive schema and deterministic offline
export/verification contract, see
[`pipeline/docs/weekly-trace-cold-archive.md`](pipeline/docs/weekly-trace-cold-archive.md).
The foundation adds no upload or deletion operation; compact vote revisions and
comments remain hot.
