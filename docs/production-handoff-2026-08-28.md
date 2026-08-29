# Production handoff — 2026-08-28

This note records the production state after the Weekly training-similarity
rollout and the 2026-08-29 quiz deployment. It distinguishes shipped work from
remaining research so a new session can resume without relying on chat history.

## Production state

- Canonical repository: `JunctionBioscience/foldarium`.
- Deployment-state verification commit:
  `f8a99e712ec1704a620348e9f17eddfdcca0c0ab`.
- Public mirror: `rafwiewiora/foldarium`.
- Current public `main`: `0905bebf087ee8c7012c8ad8133c02692ab8e6db`.
- Sanitized public source sync:
  [public PR #9](https://github.com/rafwiewiora/foldarium/pull/9).
- Local `/datasets` route parity:
  [public PR #10](https://github.com/rafwiewiora/foldarium/pull/10).
- Browser deployment commit: `0e614ec`.
- Modal deployment commit: `7af7b75`.
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
- Vercel deployment: `dpl_9xUqbDWLsjiM3cndZ8t3QEXZcHvF`.
- Immutable deployment URL:
  <https://foldarium-182pldlmo-junctionbioscience.vercel.app>.
- Production alias: <https://www.foldarium.org>.
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

Primary references:

- `weekly-training-similarity.js`
- `weekly-retrospectives.js`
- `app.js`
- `tests/weekly-training-similarity.test.js`
- `tests/weekly-molecular-review.test.js`

## Verification

- Required `contracts-and-adapters` and `scientific-evaluation` checks passed
  for both the feature and runtime hotfix PRs.
- Final JavaScript suite: 523 passed, 1 optional WASM test skipped.
- Targeted evaluation-dependent Python suite: 39 passed, 0 failed, 0 skipped.
- All 52 overlay objects declared available in the report returned
  successfully from the public Storage bucket.
- Production serves the v2 similarity report with 100 records, while
  non-runtime documentation remains excluded.
- Production `/api/config` reports environment `production` and exact commit
  `0e614ec`.
- A production browser smoke test confirmed Xtal then Training navigation,
  successful report and overlay requests, no Training in Show all, no bottom
  annotation, and a wrapping active label with no horizontal overflow.
- Production serves the Play-for-fun endpoint, preserves the password gate, and
  serves the requested green/blue archive action styling.
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
- Public verification passed: all six required GitHub checks, 552 JavaScript
  tests, 471 Python tests, the public-tree boundary audit, and shared-module
  production parity.

## Remaining work

### Research backlog

`docs/feature-backlog.md` records apo-pocket structural similarity as a
candidate. If prioritized, plan and calibrate it as a separate signal from
protein familiarity and ligand-bound-system familiarity.

## Resume checklist

1. Confirm production still resolves to browser commit `0e614ec`.
2. Confirm the current production round remains
   `weekly-2026-08-29-beta-v2` with 33 items.
3. Treat all seven terminal failed runs as final at attempt 2/2.
4. Confirm public `main` still contains public PRs #9 and #10 at
   `0905bebf087ee8c7012c8ad8133c02692ab8e6db`.
5. Promote apo-pocket similarity to a plan only if it becomes a priority.
