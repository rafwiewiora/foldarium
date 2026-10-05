# Scoped automatic benchmark enrollment

An explicit `foldarium.weekly-benchmark-policy/v2` restricts new enrollment to
canonical production rounds whose stored voting window is currently open. Its
`enrollment_scope` contains exactly `kind` (`canonical-production-weekly`),
`first_release_date` (an ISO Saturday), `production_round_suffix`, and a finite
positive `max_weekly_cost_usd`. Required methods must be nonempty and their
combined configured execution budgets cannot exceed this cap per canonical weekly round.

Campaign release metadata must match the exact `wwpdb-YYYY-MM-DD` campaign and
`weekly-YYYY-MM-DD-<suffix>` round identities. Preview, historical, pre-start,
expired, revealed, mismatched and previously enrolled sibling rounds are excluded
from new scoped enrollment. Frozen obligations remain valid after voting closes.
The existing v1 policy normalization, digests, execution identities and explicit
archival authority remain unchanged.

`freeze_expected_benchmarks` reloads authoritative state and recomputes the whole
action under the current policy before executing it. A stale queued action cannot
silently enroll under a previous policy or a changed budget. The service-only
`freeze_weekly_automation_policy_v2` RPC also verifies the complete policy digest,
manifest and method/job correspondence. It locks the campaign and round, checks
the live window, and records one append-only scoped enrollment per campaign.
An exact lost-acknowledgement retry remains idempotent after closing.

This is a guard for new scoped automatic enrollment, not an account-wide invoice
limit: explicit legacy/manual authority remains separate. It neither supplies a
policy nor enables an inference driver, scheduler or deployment. Integrators must
route automatic freezes through the executor and RPC; copying these libraries
does not wire an existing launcher automatically.

## Verification

`test_weekly_benchmark_scope.py` covers canonical identities, open-window and
cap checks, stale actions, preserved v1 bytes and execution UUIDs, and continuing
frozen work. `check_weekly_benchmark_scope.mjs` applies every migration to a local
PGlite database through `acceptance_database.mjs`, then checks live SQL scope,
immutability, exact retry semantics and service-only permissions. It denies
external network access and sends no provider requests.

Run the database check with the existing pinned PGlite installation:

```sh
node pipeline/tests/check_weekly_benchmark_scope.mjs \
  /private/tmp/foldarium-pglite-acceptance/node_modules/@electric-sql/pglite/dist/index.js
```

The existing scientific CI job runs the scope check alongside vote-provenance
and composed-lifecycle acceptance. No new CI job or hosted worker is required.
