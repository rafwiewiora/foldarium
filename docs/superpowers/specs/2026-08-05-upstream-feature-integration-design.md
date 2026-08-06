# Upstream Feature Integration Design

## Goal

Port the first six upstream-only feature groups from
`rafwiewiora/foldarium@f26254f` into the local `dev` branch while preserving the
local password gate, Supabase persistence and Storage integration, serialized
Mol* rebuilds, and viewer trace replay.

The six feature groups are:

1. Paged, synchronized multi-pose Grid view.
2. Class-balanced Hard sessions.
3. Easy playability checks and expanded cluster labels/colors.
4. A shared leaderboard.
5. The training-similarity benchmark viewer.
6. The CAMEO and Runs-n-Poses preparation pipelines.

## Integration Strategy

Copy upstream by subsystem and add only the adapters needed for local behavior.
Do not overwrite the root application with the upstream snapshot because that
would remove local persistence, Storage resolution, password gating, and replay.

Implement the work as separate commits for quiz parity, leaderboard,
benchmark/Storage support, and preparation pipelines.

## Quiz Integration

Port the upstream Grid implementation into the root `app.js` and `index.html`.
Grid displays one isolated Mol* viewer per visible pose, fills the available
stage, pages Runs-n-Poses choices by anonymized method, and synchronizes cameras
between active cells. Stale builds use a revision token, and every viewer is
disposed when its page, question, or display mode is replaced.

Keep the existing singleton viewer and rebuild coordinator for Show All and
One-at-a-Time modes. Grid camera changes are mirrored to the canonical hidden
viewer so the existing trace recorder can preserve camera movement. Replay
continues to use the current single-view presentation rather than recreating the
Grid layout.

Port these upstream quiz rules without changing scientific thresholds:

- `HARD_MIX = { game-able: 0.40, all-wrong: 0.45, all-correct: 0.15 }`.
- Hard sessions draw by bucket and backfill from unused eligible items.
- Easy excludes items whose clustered representative choices do not preserve a
  correct and a clearly wrong option.
- Pose labels extend through A-Z, with enough colors for the current maximum
  Runs-n-Poses cluster count.
- Grid scoring uses the exact choices reachable through its method pages; other
  modes retain their existing cluster-selection behavior.

Reveal still waits for queued viewer work, snapshots only pre-reveal
interactions, and records the answer through the existing Supabase queue.

## Shared Leaderboard

The leaderboard is Supabase-only, with no browser-local fallback.

Add a profile table keyed by `auth.users.id`. Each anonymous authenticated user
may claim or rename one case-insensitive unique username. Usernames are trimmed,
length-bounded, and restricted to a conservative alphanumeric, underscore, and
hyphen character set.

Add security-definer database functions for:

- claiming a username for `auth.uid()`;
- reading privacy-safe leaderboard aggregates.

The leaderboard counts each user's latest answer for each `(source, item_id)`
from completed sessions. It returns only rankable aggregate fields such as
username, item count, session count, player accuracy, automated-pick accuracy,
and the difference between them. It never returns user IDs, session IDs, raw
answers, RMSDs, or viewer traces. Existing row-level policies on quiz sessions
and answers remain unchanged.

At quiz completion, the user enters a username. The client completes and flushes
the session, claims the username, then loads shared results. A username conflict
keeps the form editable and displays a clear error. `leaderboard.html` provides
the same shared read view outside the quiz.

## Benchmark Viewer and Storage

Copy these upstream areas directly:

- `benchmark/app/`
- `benchmark/prep/`
- `benchmark/README.md`
- benchmark demo HTML, JavaScript, and static manifests

Do not commit the upstream demo's 81.3 MiB of PDB/CIF files. Store those objects
in the existing public `structures` bucket under
`benchmark/demo/systems/...`.

Extend the existing uploader rather than creating a second upload stack. It
discovers benchmark PDB and CIF files, assigns the correct content type, keeps
stable relative object keys, skips existing objects unless overwrite is
requested, and exits unsuccessfully if any upload fails.

The benchmark demo keeps its static `systems.json` metadata in Git. Its asset
resolver prefixes molecular paths with the configured Supabase Storage base URL
and `benchmark/demo/`. A missing structure fails only the affected viewer panel
and produces a visible loading error.

The current terminal has no `SUPABASE_SERVICE_ROLE_KEY`. The implementation will
provide and verify the upload command, but the live benchmark upload must be run
after `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set in the terminal.
Credentials must not be committed or placed in browser configuration.

## Preparation Pipelines

Copy upstream `prep/` unchanged, including the CAMEO and Runs-n-Poses scripts and
documentation. Keep `benchmark/prep/` unchanged as part of the benchmark copy.
Preserving the upstream files verbatim avoids accidental changes to alignment,
clustering, novelty, provenance, or scoring logic.

Document required scientific dependencies and generated-data locations. Generated
bulk data remains outside Git and is published through Storage when needed.

## Error Handling

- A failed Grid cell does not prevent other cells from loading.
- Grid mode changes cancel stale asynchronous builds and dispose old viewers.
- Hard-session bucket shortages are backfilled without duplicating items.
- Supabase unavailability produces an explicit leaderboard error; it does not
  fabricate local rankings.
- Username conflicts are reported without discarding the completed quiz.
- Benchmark uploads are idempotent and report every failed object without
  exposing credentials.
- Copied scientific scripts retain upstream fail-closed validation behavior.

## Minimal Verification

- Run the existing Node test suite.
- Add focused tests only for Hard sampling/Easy eligibility, unique-username and
  leaderboard client behavior, and benchmark PDB/CIF upload discovery.
- Compile all copied Python files to catch syntax errors.
- Perform one browser smoke test covering CAMEO and Runs-n-Poses Grid switching,
  linked cameras, reveal, persistence, replay compatibility, username claim, and
  shared leaderboard loading.

Avoid broad test-only refactors or exhaustive mocked browser infrastructure.

## Out of Scope

- The upstream provenance-preserving `data_rnp_aligned/` export.
- The upstream GitHub Pages workflow.
- The legacy `app/` SQLite prototype.
- Committing benchmark molecular binaries.
- Replacing the current replay UI with a multi-view Grid replay.
- Implementing the documented but unfinished provider-neutral prediction
  scheduler or database-backed publication catalog.
