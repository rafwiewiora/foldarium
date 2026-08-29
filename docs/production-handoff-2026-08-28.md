# Production handoff — 2026-08-28

This note records the production state at the end of the Weekly
training-similarity rollout. It distinguishes shipped work from active and
unmerged follow-ups so a new session can resume without relying on chat history.

## Production state

- Canonical repository: `JunctionBioscience/foldarium`.
- Deployed commit: `65c0a6290f1237e7d19b99c9d45a2d11c18c467b`.
- Reviewed feature:
  [Junction PR #36](https://github.com/JunctionBioscience/foldarium/pull/36).
- Runtime packaging and commit-attestation fix:
  [Junction PR #38](https://github.com/JunctionBioscience/foldarium/pull/38).
- Vercel deployment: `dpl_GFLHVrxcG6E7RXsmRtD4z88CM8Eu`.
- Immutable deployment URL:
  <https://foldarium-oltxjqkaj-junctionbioscience.vercel.app>.
- Production alias: <https://www.foldarium.org>.
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
  `65c0a6290f1237e7d19b99c9d45a2d11c18c467b`.
- A production browser smoke test confirmed Xtal then Training navigation,
  successful report and overlay requests, no Training in Show all, no bottom
  annotation, and a wrapping active label with no horizontal overflow.
- `git diff --check junction/main...HEAD` passed before merge.

## Remaining work

### Preview intake-cap warning — not shipped

The warning requested when eligible targets exceed the 40-target cap remains
uncommitted in `/private/tmp/foldarium-weekly-cap-warning` on
`fix/weekly-cap-preview-warning`. It includes intake counts, preview manifest
metadata, Modal summary output, browser warning rendering, and tests. Reconcile
it onto current Junction `main`, review, commit, open a PR, deploy the pipeline
and browser changes, and verify with a synthetic overflow preview.

### Public open-source mirror — not at production parity

`rafwiewiora/foldarium` is the public mirror, not the production source.
`origin/feature/weekly-training-similarity-audit` contains an earlier audit
stage, while the final RnP, overlay, and UI changes shipped through Junction
PR #36. Reconcile and review the sanitized final change before merging it into
public `origin/main`; do not copy deployment-specific access configuration.

### Aug 29 Weekly operations — active

Monitoring remains active for the Aug 29 intake, cofolding lifecycle, and first
approval preview. Confirm target counts, prediction completion, quiz assembly,
and preview readiness. The preview intake-cap warning above is not yet
available, so inspect the pre-cap eligible and excluded counts operationally.

### Research backlog

`docs/feature-backlog.md` records apo-pocket structural similarity as a
candidate. If prioritized, plan and calibrate it as a separate signal from
protein familiarity and ligand-bound-system familiarity.

## Resume checklist

1. Confirm production still resolves to commit `65c0a62`.
2. Check the Aug 29 lifecycle and approval-preview status.
3. Finish and ship the preview intake-cap warning.
4. Reconcile the public mirror with Junction PR #36.
5. Promote apo-pocket similarity to a plan only if it becomes a priority.
