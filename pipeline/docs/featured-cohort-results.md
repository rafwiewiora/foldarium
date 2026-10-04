# Featured assignment results v1

The full weekly manifest, selector kit, predictions, votes, evaluation, and
published full-round statistics stay unchanged. The five-question assignment
has a separate comparison population and a separate completion definition.

A released production publication qualifies only when its immutable featured
selection was registered strictly before that round's voting close. The service
RPC `get_weekly_featured_cohort_source_v1` returns the selection catalog row only
for a revealed production round with the exact blind-manifest digest. It is not
available to anonymous or authenticated browser roles. The server downloads the
private selection audit, verifies its exact size and SHA-256, and checks its
round, blind manifest, private index, selected IDs, policy, and population fields
against the frozen marker and verified publication. Missing or post-close draws
leave that historical week in the full-round view; no past assignment is inferred.
Malformed provenance fails closed.

`GET /api/weekly-retrospectives?round_id=...&scope=featured` adds a
`featured_cohort` object using `foldarium.weekly-featured-results/v1`, or null for
an ineligible historical round. Add `summary=1` for a small result-only response
without molecular manifests or overlays. Default detail and all-time API
responses retain their prior contracts. No correctness is available before the
existing reveal and publication checks succeed.

The cohort scores final archived votes on the same selected item IDs for humans,
authorized LLM identities, Smina, and ligand-pLDDT. Exact choices use the selected
raw pose's correctness, as do both baselines. Explicit cluster choices use
cluster acceptance; None is correct only when no raw pose passes. This respects
the archived selection kind without changing legacy full-round scoring. `assignment_answered`, `assignment_total`, and
`assignment_complete` count every selected question, including a reference later
found unscorable. `answered`, `correct`, `total`, and `accuracy` count only
scorable selected questions. A completed five-question assignment containing one
unscorable reference is 5/5 complete with a 4-question scoring denominator. An
all-unscorable assignment can be complete but has null accuracy and no rank.
Optional full-round votes do not improve the featured score.

`GET /api/weekly-retrospectives?all_time=1&scope=featured` returns
`foldarium.weekly-featured-all-time/v1`. Completed featured assignments contribute
to totals; partial assignments retain their completion counts and have no rank
or aggregate accuracy until an eligible completed week supplies scored questions.
Human grouping uses the same private source linkage and public pseudonyms as the
existing archive. No private IDs, audit digests, execution proofs, or artifact
locations enter the response. Fewer than three completed weeks remains provisional.

The UI defaults to the featured comparison for eligible weeks, and to featured
all-time rankings when at least one eligible published week exists. Explicit
full-round controls retain historical completion and aggregate formulas. Full
scientific cofolding statistics continue to use the full released population.
A voting session remains revisable: finishing the five-question assignment does
not set the backend session's terminal `completed_at` flag.

Apply migration `20261005000000_read_verified_featured_cohorts.sql` before deploying
the frontend/API consumers. This change is prepared code until deployment; it
does not freeze selections, publish retrospectives, alter ballots, or activate
paid inference. Verification includes strict provenance/denominator tests, real
PGlite role and release-boundary tests, and Chromium featured/full-view fixtures.
