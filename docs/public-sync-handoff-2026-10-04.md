# Public mirror follow-up — 2026-10-04

This updates the same-day integration handoff after public pull request 19 merged
as public `main` commit `a515a08`. The follow-up starts from that merged source;
previous browser, scientific display, selection, and Storage retry changes are
retained without being reapplied.

## Verified runtime state

- **Completed:** five featured questions are registered for the current
  37-question round. The marker is immutable, bound to the exact full blind
  manifest, and verified through the public read boundary. Private selection
  artifacts remain private. The full manifest, Selector kit, and scientific
  benchmark population retain all 37 questions.
- **Completed:** the browser supports the featured subset and optional full-round
  exploration while preserving canonical question ordinals. Resume tokens keep
  the same selection; historical sessions keep their original full scope.
- **Completed:** the production cofolding endpoint serves three published weeks
  with explicit population and metric definitions. Counts describe published quiz
  questions with method poses, not unpublished prediction campaigns.
- **Completed:** the featured-selection and automation-outbox migrations and the
  corresponding browser and worker sources were deployed. Immutable Storage
  upload retries were also deployed.
- **Pending:** the new weekly assembly run has not yet completed. The scientific
  display fallback and upload retry retain fail-closed artifact validation.
- **Pending:** recurring reconciliation, inference, selection, reveal, and
  publication activation. All newly introduced automation gates remain disabled;
  registering one selection does not enable a recurring schedule.

## Portable follow-up source

- **Completed:** mirrored exact lifecycle scope in the provider-independent
  planner, service loop, and tests. Each canonical identity binds campaign,
  environment, round, and blind-manifest SHA. Ambiguous historical siblings stay
  blocked unless explicitly scoped or already enrolled by frozen expectations.
  The planner does not infer the preferred round from a version label.
- **Completed:** mirrored the immutable inference-budget initialization migration
  and its isolated PostgreSQL behavioral harness. Its service-only claim grants
  initialization once per exact frozen execution/kit/config/budget. An executor
  must retain and validate the existing ledger after every subsequent claim,
  including after an ambiguous acknowledgement or complete volume loss.
- **Pending:** operational rollout of the new budget-initialization contract and
  any separately reviewed inference executor. This public schema is not an
  executable provider launcher and enables no paid calls.

## Intentional exclusions

Credentials, live runtime configuration, private data and artifacts, pre-reveal
ballots, operator logs, recovery execution identifiers, access gates, deployment
SDKs and profiles, and spend-producing schedules remain outside this mirror.
Hosting-specific route adapters and the private canonical lifecycle scope are
not copied. The schema and tests contain synthetic fixtures only.

The operational journal, exact-date recovery helpers, deployment-specific
reconciliation executor/tests, and unattended provider launchers remain excluded.
The public tree preserves its local defaults, loopback development support,
portable environment names, and pinned browser dependency. Shared planner,
service-loop, migration, and database-harness source matches accepted operational
source; provider launch adapters are independently maintained and reviewed.

## Verification

- Follow-up JavaScript suite: 608 tests passed with one expected optional
  WebAssembly skip. Production parity again passed for all seven shared browser
  targets and the public configuration contract.
- Follow-up portable pipeline: 595 tests passed, including six additional exact
  lifecycle-scope regressions.
- The new isolated PostgreSQL harness passed one-time initialization, repeated
  claims, exact source/config/kit/budget binding, immutable authority, open voting
  compatibility, and service-only privilege checks.
- Public audit passed for 2,392 tracked files. The eight changed files passed
  additional credential-pattern, private-path, and recovery-identifier checks.
- All five newly mirrored portable source, test, and migration files match the
  accepted operational source exactly; `git diff --check` passed.
- Scientific evaluation recovery remains pending separately; this follow-up
  does not claim that the next complete weekly publication has succeeded.

The database, object store, runtime deployment, and public Git source are
independently versioned. The previously applied migration head was
`20261004090000_add_weekly_automation_outbox.sql`; the prepared budget anchor is
`20261004170000_anchor_weekly_inference_budget.sql`. The optional legacy data
release is unchanged. Matching source does not certify lifecycle completion or
activation of any gate.
