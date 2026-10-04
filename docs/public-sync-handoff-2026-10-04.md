# Public mirror: audited unscorable references — 2026-10-04

This follow-up starts from public main `7391e28`, after public pull request 20.
It mirrors the accepted scientific and consumer changes through the same-day
operational pull request 83 without copying its deployment or recovery files.

## Completed source

- Exact heavy-atom identity and explicitly deposited missing-atom evidence can
  establish an unscorable released reference. Ambiguous chemistry still fails;
  the RMSD acceptance and reference-coverage thresholds are unchanged.
- Every original item, choice, coordinate artifact, and vote is retained.
  Unscorable choices carry null metrics and no fabricated answer overlay.
- Private evaluation v6 and retrospective v2 record full, scorable, and excluded
  populations. Fully scored legacy artifacts preserve their existing format.
- Selector scoring, public archives, method statistics, and post-reveal Play for
  fun use the scorable denominator and retain the full population separately.
  An all-unscorable population is unranked with null accuracy.
- Molecular review accepts the validated public disposition, preserves original
  poses, and displays an explicit Not scored message. Retrospective projection
  also retains the blind-safe warning for a full-receptor display fallback.
- Exact immutable Storage downloads have bounded transient retries and verify
  the content digest. Arbitrary mutation RPCs and wrong-content responses do not
  gain retries. Both evaluation formats pass the publication registration gate.
- An additive database migration preserves service-only permissions, immutable
  catalog bindings, reveal windows, and automation receipt checks.

## Verification

- Completed: 630 JavaScript tests passed with one optional WebAssembly skip;
  all 616 scientific Python tests passed; the dependency-light run passed with
  139 expected skips. The strict unscorable PostgreSQL behavior harness passed.
- Completed: browser fixture enters an unscorable question, renders all poses,
  navigates to a scored question with crystal comparisons, and returns without
  losing null metrics or selecting None. No production votes are written.
- Completed: public-tree audit passed for 2,406 tracked files, and all 40 changed
  files passed a credential and private-runtime-identifier scan. Production
  parity passed for all seven shared browser modules and public configuration.
- Completed: public pull request 21 passed all six CI jobs, including Supabase
  startup and full migration reset, Rust mapper tests, both Python environments,
  JavaScript tests, and the repository-wide secret scan.
- Pending: production recovery evaluation/publication verification. This source
  handoff does not assert that delayed answers have already been published.

## Intentional exclusions

Credentials, private artifacts and ballots, operational execution identifiers,
local fixture data, access gates, live environment configuration, hosting route
adapters, deployment SDKs and profiles, and spend-producing schedules remain
excluded. Future intake policy v5, historical Preview publication support,
archive identity changes, and featured-cohort ranking are separate work.

The public Supabase adapter retains local execution defaults, and the browser
retains its access-free shell and local development behavior. Existing public
handoff guards are preserved. The new migration is
`20261004200000_support_unscorable_reference_dispositions.sql`; its presence in
Git is not a statement that it has been applied to any running database.
