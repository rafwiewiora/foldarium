# Production handoff — 2026-08-28

This note records the production state after the Weekly training-similarity
rollout, the 2026-09-05 quiz deployment, and the protected continuation of
2026-08-29 voting. It distinguishes shipped work from remaining research so a
new session can resume without relying on chat history.

## Production state

- Canonical repository: `JunctionBioscience/foldarium`.
- Deployment-state verification commit:
  `8937ff028b5a3445295e0086331741686b4fb573`.
- Public mirror: `rafwiewiora/foldarium`.
- Current public `main`: `e25e93ca77e6bdc177e8a3e853a7fd3ad29a2412`.
- Sanitized public source sync:
  [public PR #9](https://github.com/rafwiewiora/foldarium/pull/9).
- Local `/datasets` route parity:
  [public PR #10](https://github.com/rafwiewiora/foldarium/pull/10).
- Browser deployment commit: `8937ff028b5a3445295e0086331741686b4fb573`.
- Modal deployment commit: `80a19afb7f4ce9be3345ebd1c6557c6c5a44ed6d`.
- Reviewed feature:
  [Junction PR #36](https://github.com/JunctionBioscience/foldarium/pull/36).
- Runtime packaging and commit-attestation fix:
  [Junction PR #38](https://github.com/JunctionBioscience/foldarium/pull/38).
- One-retry lifecycle:
  [Junction PR #41](https://github.com/JunctionBioscience/foldarium/pull/41).
- Retrospective Play for fun:
  [Junction PR #42](https://github.com/JunctionBioscience/foldarium/pull/42).
- Retry-accounting and v4 assembly guard:
  [Junction PR #43](https://github.com/JunctionBioscience/foldarium/pull/43).
- Mol* Grid loading performance:
  [Junction PR #50](https://github.com/JunctionBioscience/foldarium/pull/50) and
  [public PR #13](https://github.com/rafwiewiora/foldarium/pull/13).
- First-Grid background construction:
  [Junction PR #53](https://github.com/JunctionBioscience/foldarium/pull/53)
  and [public PR #15](https://github.com/rafwiewiora/foldarium/pull/15).
- Delayed Weekly retrospective release:
  [Junction PR #55](https://github.com/JunctionBioscience/foldarium/pull/55)
  and provider-neutral [public PR #16](https://github.com/rafwiewiora/foldarium/pull/16).
- September Weekly recovery and protected exact-round voting:
  [Junction PR #57](https://github.com/JunctionBioscience/foldarium/pull/57),
  [Junction PR #58](https://github.com/JunctionBioscience/foldarium/pull/58),
  and provider-neutral [public PR #17](https://github.com/rafwiewiora/foldarium/pull/17).
- Cofolding retrospective performance:
  [Junction PR #48](https://github.com/JunctionBioscience/foldarium/pull/48)
  and provider-neutral [public PR #11](https://github.com/rafwiewiora/foldarium/pull/11).
- Vercel deployment: `dpl_DNCtDzaaq1XGhoCacM9i31gdRGdT`.
- Immutable deployment URL:
  <https://foldarium-buhxztm1i-junctionbioscience.vercel.app>.
- Production alias: <https://www.foldarium.org>.
- Public performance beta: <https://foldarium-performance-beta.vercel.app>.
- Vercel-team-protected August 29 voting preview:
  <https://foldarium-aug29-private-vote-n4iznprei-junctionbioscience.vercel.app>
  (`dpl_2tVGoZHeck1FF4ncxAZRpGPanX4V`).
- Modal profile digest:
  `c69c27f87ea20e41138d9ac34db92aca5a4fc63dc2ad33f97a4051d896b06f96`.
- Deployment was made manually from a clean detached worktree at the merged
  Junction `main` commit. Do not assume that pushes to `main` deploy
  automatically.

## Shipped contract

### Scientific audit

- The post-reveal audit covers 100 published targets: 39 familiar, 58 novel,
  and 3 unknown.
- The canonical metric is protein-frame carried-ligand volume overlap. Its
  novelty threshold remains `0.25`.
- A separately versioned RnP-style SuCOS-pocket approximation is reported in
  parallel; it does not replace or alter the canonical metric.
- Exact scoring uses the released crystal and ligand. Blind scoring accepts
  only archived predicted receptors, pockets, and candidate poses.
- Search, download, parsing, chemistry, and incomplete-candidate failures fail
  closed to `unknown`, not `novel`.
- The retained Foldseek search is limited to 25 pre-cutoff structural
  neighbors. The RnP-style result is not a paper-identical 5,000-candidate
  PLINDER rerun.

Primary references:

- `docs/weekly-training-similarity-audit.md`
- `docs/weekly-training-similarity-results.json`
- `docs/weekly-training-similarity-results.csv`
- `pipeline/README.md`
- `pipeline/src/foldarium_pipeline/training_similarity.py`
- `pipeline/src/foldarium_pipeline/rnp_similarity.py`

### Retrospective UI

- Archive questions retain their original published numbers under sorting.
- Archive order options are Default, Novel first, and Familiar first; unknown
  scores remain last.
- Question names retain the ligand symbol and add the released PDB identity
  with an RCSB link.
- In molecular review, appended reference poses are ordered Xtal first and
  closest training second.
- The training reference is available in One and Grid only. It is absent from
  Show all.
- The training reference renders only the pre-aligned scored ligand in cyan,
  never a second protein cartoon. It is explicitly unscored.
- There is no training toggle and no duplicate bottom annotation.
- The concise active/Grid label includes source PDB, ligand component, and
  overlap score and wraps instead of truncating.
- A reference appears only where a validated, content-addressed overlay exists.
- `.vercelignore` excludes other documentation while explicitly shipping
  `docs/weekly-training-similarity-results.json`; removing that exception
  silently disables production similarity hydration.
- Revealed archive rounds expose a solid-green **Play for fun** action beside
  the solid-blue **Open molecular review** action.
- For-fun sessions and vote attempts use the post-reveal tables and are scored
  separately from blind-week and all-time rankings. There are no seeded or fake
  production leaderboard rows.
- The Cofolding view reports overall and week-by-week method performance for
  revealed rounds. Oracle success means any raw pose is strictly correct; top-1
  means the highest ligand-pLDDT raw pose is strictly correct, with stable
  choice ID as the tie-breaker.
- Cluster `accepted_correct` never contributes to these scientific metrics.
  Missing ligand pLDDT excludes a target only from the top-1 denominator.
- Revealed Archive question rows include per-method Oracle and Top-1 outcome
  matrices. The open August 29 round remains absent until it is revealed.

Primary references:

- `weekly-training-similarity.js`
- `method-performance.js`
- `weekly_method_stats.json`
- `weekly-retrospectives.js`
- `app.js`
- `tests/weekly-training-similarity.test.js`
- `tests/weekly-molecular-review.test.js`

### Mol* loading performance

- Weekly prefetches the next three visible Grid asset sets with four bounded
  concurrent transfers. Foreground loads claim matching in-flight work or use
  the bounded prefetched-byte cache instead of starting duplicate transfers.
- Grid cards appear progressively. The hidden canonical scene is deferred
  while Grid is active.
- A bounded pool reuses up to nine Mol* viewers between questions. While the
  participant enters their name, the first nine complete molecular scenes are
  built offscreen and adopted only when their exact question/view signature
  matches. Any remaining preparation overlaps named-session creation. Final
  settled camera synchronization no longer waits an additional 600 ms.
- Public content-addressed assets from future publications receive immutable
  cache metadata. The backfill tool remains reveal-gated and therefore has not
  mutated the currently open production round.
- Production enables the speedups but not the performance clock or diagnostics
  UI. The separate public beta enables the clock and consented, bounded reports
  through deployment configuration, so its stable URL requires no query flags.
- Performance reports use a dedicated private table and append-only RPC. They
  are not stored in replay traces and exclude asset URLs, IP addresses, raw user
  agents, plugins, fonts, and other browser-fingerprint fields.

### Delayed Weekly retrospective release

- `weekly-2026-09-05-beta-v2` is the public current round. It opened at
  2026-09-09 20:06:06 UTC with 37 blind items and closes at
  2026-09-16 00:00:00 UTC.
- `weekly-2026-08-29-beta-v2` remains open and unrevealed with 33 blind items.
  Its finite safety close was explicitly extended from 2026-09-09 to
  2026-09-16 00:00:00 UTC without running the successor handoff.
- The scheduled retrospective job detects an opted-in open round and performs
  private pre-close preparation instead of calling the post-close evaluator.
  Existing rounds without the policy retain the deployed Wednesday behavior.
- Extending the August voting window superseded its evaluation prepared for the
  earlier close. The superseded descriptor remains private provenance; a fresh
  evaluation is required after the extended window.
- A Vercel-team-protected deployment pins the exact August round while using
  the normal authenticated production named-session, vote-attempt, trace, and
  `selection_kind` paths. Its temporary automation bypass was removed.
- Successor handoff is bound to exact predecessor and successor IDs. It shortens
  the predecessor close to activation time, promotes the prepared artifact,
  reveals the round, snapshots final votes, and publishes the retrospective.
  Missing or inconsistent lifecycle provenance fails closed.
- Ballot scope, correctness, vote persistence, result aggregation, and
  player-name disclosure semantics are unchanged.

## Verification

- Required `contracts-and-adapters` and `scientific-evaluation` checks passed
  for both the feature and runtime hotfix PRs.
- Final JavaScript suite: 571 passed, 1 optional WASM test skipped.
- Targeted evaluation-dependent Python suite: 39 passed, 0 failed, 0 skipped.
- All 52 overlay objects declared available in the report returned
  successfully from the public Storage bucket.
- Production serves the v2 similarity report with 100 records, while
  non-runtime documentation remains excluded.
- A production browser smoke test confirmed Xtal then Training navigation,
  successful report and overlay requests, no Training in Show all, no bottom
  annotation, and a wrapping active label with no horizontal overflow.
- Production serves the Play-for-fun endpoint, preserves the password gate, and
  serves the requested green/blue archive action styling.
- Production `/api/config` reports exact commit
  `7fa1eb1120385286c496665eceef4e860eda7f75` with performance-beta mode off.
- A read-only live smoke adopted nine prepared Grid scenes, created no
  foreground viewer, and had no failed card. Click-to-ready was 346 ms and the
  first card was ready at 317 ms in that run.
- The queryless beta smoke exposed the clock and consent control, adopted nine
  prepared scenes, created no foreground viewer, and had no failed card.
- The 2026-08-29 production round is
  `weekly-2026-08-29-beta-v2`, promoted from
  `preview-weekly-2026-08-29-nextweekly-v4`.
- The final round has 33 items and 330 choices. Prediction state is 71 succeeded
  and 7 failed across 78 method runs; the failures are the six repeated failures
  below plus the previously exhausted `13IB` run.
- Seven previously unretried failures were authorized at exactly attempt 2/2.
  `38GO` OpenFold3 recovered; the other six runs failed again and cannot receive
  a third attempt.
- The production Selector kit is registered with SHA-256
  `cfa18b867e84b3886706e349ea3ed4a46b4665ee4628a993127ada85a020cd68`.
- `git diff --check junction/main...HEAD` passed before merge.
- Public PR #9 merged the final scientific audit, retrospective UI, Play for
  fun, and provider-neutral lifecycle fixes. The earlier partial audit PR #6
  was closed as superseded.
- Public PR #10 added the production-equivalent `/datasets` mapping to the
  provider-neutral local server and updated the public sync handoff.
- Public PR #15 passed all six required GitHub checks, 585 JavaScript tests
  with one optional WASM skip, and the public-tree boundary audit. The
  feature-on versus feature-off desktop/mobile viewer parity audit also passed.
- Junction PR #55 passed both required checks. The complete local pipeline suite
  passed 546 tests with 160 optional-dependency skips.
- The reviewed Modal profile deployed commit
  `80a19afb7f4ce9be3345ebd1c6557c6c5a44ed6d`; post-deploy verification matched
  configuration digest
  `c69c27f87ea20e41138d9ac34db92aca5a4fc63dc2ad33f97a4051d896b06f96`.
- Public PR #16 passed all six checks after rerunning one GitHub API rate-limit
  failure in the Supabase CLI setup step. Its portable pipeline suite passed
  486 tests with 113 optional-dependency skips.
- Production migration
  `20260909203000_add_exact_open_weekly_round_lookup.sql` was applied before
  the protected pinned client was deployed.
- Live RPC verification confirmed September 5 is the public current round and
  both September 5 and August 29 are open through 2026-09-16 with null reveal
  manifests.
- The protected August deployment reports writable production configuration,
  exact round `weekly-2026-08-29-beta-v2`, commit
  `80a19afb7f4ce9be3345ebd1c6557c6c5a44ed6d`, 33 blind items, and no reveal.
- Junction PR #58 passed both required checks. Public PR #17 passed all six
  checks; locally, 590 JavaScript tests, 492 pipeline tests, and the
  provider-neutral public-tree audit passed.
- The cofolding statistics fixture exactly matched live revealed production
  data for August 8, 15, and 22: 100 targets per method. Boltz-2 has 46 oracle
  and 41 top-1 successes; OpenFold3 has 44 oracle and 31 top-1 successes.
- Junction PR #48 passed both required checks. Local verification passed 583
  JavaScript tests, 553 pipeline tests, 14 focused method/UI tests, and 5
  desktop/mobile browser tests.
- Production serves browser commit
  `8937ff028b5a3445295e0086331741686b4fb573`. A live browser smoke confirmed
  overall ranking, weekly chart, no desktop/mobile overflow, all 39 August 22
  target matrices, and no unexpected browser errors.
- Public PR #11 passed all six checks. Its provider-neutral verification passed
  597 JavaScript tests, 492 pipeline tests, and the public-tree audit.

## Remaining work

### Operational follow-up

- After the 2026-09-16 close, prepare a fresh August evaluation and explicitly
  run the exact August 29 / September 5 handoff. Verify close, reveal, vote
  snapshot, and retrospective publication before treating the lifecycle as
  fully shipped.
- Apply the immutable cache metadata backfill for the current round only after
  that reveal; the apply path intentionally refuses an open round.

### Research backlog

`docs/feature-backlog.md` records apo-pocket structural similarity as a
candidate. If prioritized, plan and calibrate it as a separate signal from
protein familiarity and ligand-bound-system familiarity.

## Resume checklist

1. Confirm production still resolves to browser commit
   `8937ff028b5a3445295e0086331741686b4fb573`.
2. Confirm foldarium.org still selects `weekly-2026-09-05-beta-v2` and the
   protected preview selects `weekly-2026-08-29-beta-v2`.
3. Confirm both rounds remain open through 2026-09-16 00:00:00 UTC and both
   reveal manifests remain null.
4. Confirm the Modal app still reports deployment commit
   `80a19afb7f4ce9be3345ebd1c6557c6c5a44ed6d` and configuration digest
   `c69c27f87ea20e41138d9ac34db92aca5a4fc63dc2ad33f97a4051d896b06f96`.
5. Treat all seven terminal failed runs as final at attempt 2/2.
6. Confirm public `main` contains public PR #11 at
   `e25e93ca77e6bdc177e8a3e853a7fd3ad29a2412`.
7. After the extended close, create a fresh August evaluation, run the exact
   predecessor handoff, add August 29 to `weekly_method_stats.json`, and apply
   immutable cache metadata only after reveal.
8. Regenerate and cache-bust the static method statistics after every newly
   revealed Weekly round.
9. Promote apo-pocket similarity to a plan only if it becomes a priority.
