# Public mirror sync preparation — 2026-10-04

This handoff supersedes the August handoff for the current source review. It is
a source integration and observed parity record; it does not claim that the
public branch has been merged or that gated automation has been activated.

## Tested source and prepared changes

- Baseline: public `main` commit `c774982`.
- **Completed:** compared the portable browser, API, pipeline, and migration
  sources with the current operational source. The public browser and Selector
  API already honor the explicit data environment using provider-neutral
  configuration. No duplicate configuration fix is needed.
- **Completed:** prepared a browser warning for
  `item.metadata.display_pocket.message`, shown alongside the existing
  alignment warning. Warnings stay visible in retrospective review and clear
  when navigating to an unaffected question. No choices or scoring values are
  changed by the browser patch.
- **Completed:** corrected the stale Play-for-fun backlog status to shipped.
- **Completed:** prepared the read-only `?cofolding=1` retrospective API and
  browser integration. It loads SHA-verified evaluation contexts for published,
  revealed production rounds and derives method rankings with raw-pose RMSD
  below 1.5 Å. The population is explicitly `published_weekly_quiz`; denominators
  count quiz questions with method poses, with missing confidence excluded from
  top-1 only. No participant snapshots or ballots are loaded or returned.
- **Completed:** integrated and tested the portable full-receptor display
  fallback for predictions with no ligand contacts. It preserves raw predicted
  coordinates and exact scientific scoring inputs, including far-away poses.
- **Completed:** prepared featured-question navigation with optional all-question
  exploration. The browser retains the full manifest order for vote, trace, and
  performance ordinals. Resume tokens preserve the selected IDs and manifest
  digest; historical tokens resume the full round. A five-question summary
  leaves votes revisable and does not change full-round leaderboard denominators.
- **Completed:** integrated the portable deterministic selector, service adapter,
  immutable private audit catalog, and narrowly projected public selection marker.
  The default is a uniform five-question draw. Optional weighting uses bounded
  blind cluster diversity and cross-method disagreement, without answer data.
  The full manifest, Selector kit, and benchmark populations remain unchanged.
- **Completed:** integrated provider-independent reconciliation planning and the
  service-role execution loop, plus durable leases, frozen benchmark policies,
  exact artifact/receipt binding, and atomic reveal checks in the matching SQL
  migration. Readiness checks bind the complete evaluation identity and window.
- **Completed:** the featured-selection and automation-outbox migrations were
  applied to the operational database. The shared browser and worker code were
  deployed; newly introduced automation gates remain disabled.
- **Completed:** prepared bounded retry for immutable content-addressed Storage
  uploads after transient transport or service failures. Every attempt preserves
  exact bytes and rejects digest conflicts; RPCs and mutable writes do not retry.
- **Pending:** verify the complete featured-selection and reconciliation flows
  before enabling gated actions. Applied schema and matching browser assets do
  not imply that inference, selection, reveal, or publication ran automatically.
- **Completed:** the final shared browser parity check passed against the public
  production origin, including featured navigation and session-resume sources.
- **Pending:** complete public review and merge; runtime end-to-end validation
  remains separate from matching browser assets.

## Intentional exclusions

Credentials, live runtime configuration, private data, pre-reveal ballots,
operator logs, access gates, deployment SDKs and runtime profiles, and
spend-producing schedules remain outside this mirror. Hosting-specific route
and environment adapters are not copied into the portable server.

The operational journal, exact-date recovery helpers, deployment-specific
reconciliation executor/tests, and unattended provider launchers serve excluded
runtime adapters; they are not included in this preparation. Generic planning,
registration, and artifact verification remain independently usable and tested.
The public tree retains its local defaults, loopback development support,
portable environment names, and pinned browser dependency. The Supabase adapter
changes add featured-selection registration and bounded immutable upload retry,
while preserving local execution backend defaults.

The portable backend source, both new migrations, and their tests match the
accepted operational source exactly. Browser code retains the intentional local
configuration boundary described in `production-parity.md`. Source preparation
alone does not certify that a scheduler is running or that migrations are live.

## Verification

- Baseline production parity: all seven shared browser targets and the public
  configuration contract passed against `https://www.foldarium.org`.
- Prepared browser changes: `npm test` passed 608 tests, with one expected skip
  because the optional WebAssembly package was not built.
- Portable scientific pipeline: 589 tests passed, including predicted-pocket
  recovery, featured selection, reconciliation, artifact binding, and immutable
  Storage upload retry tests.
- Both isolated PostgreSQL behavioral harnesses passed: featured selection tests
  cover immutable IDs and hashes, service-only registration, private projection,
  and view-return RPC compatibility; reconciliation tests cover leases, bounded
  retries, exact readiness, artifact bindings, and service-only privileges.
- `npm run audit:public` passed for 2,390 tracked files. An additional scan of
  all 34 changed files found no credential patterns, private workspace paths,
  or recovery execution identifiers. No excluded operational files were copied.
- `git diff --check` passed.
- Final production parity: all seven shared browser targets and the public
  configuration contract passed against `https://www.foldarium.org`. This
  supersedes the earlier expected pre-deployment asset mismatches; it does not
  by itself verify database or scheduled-worker state.

The production database, object store, runtime deployment, and public Git source
are independently versioned. The applied migration head is
`20261004090000_add_weekly_automation_outbox.sql`; the optional legacy data
release is unchanged. This source review does not certify runtime lifecycle
health or activation of any new automation gate.
