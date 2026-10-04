# Public mirror: featured comparisons and selection evidence — 2026-10-04

This update starts from public main `ba825ef`, after pull request 22. It prepares
accepted portable featured-assignment results, versioned intake correction, and a
reproducible analysis of previously published Foldseek evidence.

## Prepared source

- Featured results require an immutable selection registered strictly before
  voting closed, verified against the exact published source and private audit.
  Humans, authorized models, and both baselines use the same selected questions.
  Exact choices use raw correctness, explicit cluster choices use cluster
  acceptance, and None succeeds only when no raw pose passes.
- Assignment completion includes verified unscorable references; accuracy does
  not. Completed assignments feed separate featured all-time totals. Full-round
  history, full scientific populations, original ballots, and legacy formulas
  remain intact. The API and UI offer explicit featured/full views.
- Opt-in intake v5 excludes explicit/isotopic hydrogen from heavy-atom counts.
  Default and unstamped historical inputs remain v4. Frozen policy provenance
  survives staging, publication, and evaluation; invalid/mixed policies fail.
- The offline Foldseek report retains all 1,000 raw poses from 100 targets across
  three already public revealed rounds. Its compact snapshot contains only
  scientific identifiers, public source URLs/digests, windows, metrics, and
  correctness labels. No participants, ballots, structure bytes, or private
  artifacts are included. It does not validate a blind interestingness score or
  change the uniform five-question default.

## Verification status

- Completed: 665 JavaScript tests passed with one optional WebAssembly skip;
  650 scientific Python tests passed; dependency-light Python passed 647 tests
  with 148 expected optional-dependency skips.
- Completed: offline analysis reproduced exact result bytes; the public v4 plan
  fixture independently matched the unchanged public baseline, including IDs.
- Completed: real PostgreSQL service-role ACL, pre-reveal, production-environment,
  and manifest-binding checks; public-tree audit of 2,442 tracked files and
  credential/private-runtime scans of all 30 changed files.
- Completed: seven shared production browser modules and public configuration
  match exactly; the featured-question module also matches. Retrospective source
  differs only by the intentionally excluded deployment password gate.
- Completed: the code revision passed all six public CI jobs, including clean
  Supabase startup and migration reset, JavaScript, both Python environments,
  Rust, and repository-wide secret scanning.
- Deployed: featured API/schema and shared browser source. The historical draw
  frozen after voting closed correctly remains ineligible; no completed featured
  research cohort is fabricated. Production verification of a future eligible
  published cohort and any v5 activation remain pending. This mirror does not
  authorize inference or experiments.

## Intentional exclusions

No deployment SDKs, provider launchers/profiles, schedules, deployment tests,
private handoffs, credentials, operational logs, live configuration, access
policies, or hosting routing are copied. Public HTML remains password-free;
local server behavior, environment defaults, and authenticated-proxy admin checks
are preserved. Migration `20261005000000_read_verified_featured_cohorts.sql` adds
only a service-role read of the existing immutable assignment catalog.
