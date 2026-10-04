# Durable prediction handoffs

The portable library and service-only database contract preserve exact prediction
submission identity across lost acknowledgements and proven worker loss. They do
not launch jobs on import. A host supplies its SDK interface, submission callback,
coordinator and observation loop; hosted workers, profiles and schedules are not
included here.

Apply `20261005010000_preserve_prediction_failure_diagnostics.sql` before
`20261005020000_durable_prediction_dispatch.sql`. The latter wraps the existing
snapshot without replacing round, evaluation, benchmark or publication contracts.

## Caller integration

Use `reconcile_prediction_dispatch` with an injected SDK and `spawn(task,
dispatch_id, retry_request)` callback. Initial task identity is frozen before a
single durable submission grant. Retry preparation atomically binds reviewed
resource overrides to the sole increase from one to two allowed attempts.
The host must pass the dispatch ID through to its worker. Before claiming the
run, that worker acknowledges the exact provider call ID, task ID and execution
task using `acknowledge_weekly_prediction_dispatch_v1`. This can heal a lost caller
acknowledgement without submitting again. Direct untracked submissions are outside
this recovery contract.

An unknown submission outcome retains its intent. Missing output, a polling
deadline, an expired lease or an ambiguous call graph never authorizes another
scientific attempt. Distinct outbox phases permit delayed acknowledgement and
later loss observation without falsely completing dependent assembly work.

Loss disposition requires a unique retained terminal failure input. For a claimed
run, its task ID must match the expired lease owner. The final SQL write compares
the exact attempt, owner and expiry under a row lock. Registered science or an
exact diagnostic catalog entry blocks disposition for artifact review. An
immutable private loss row preserves original state and call identity; existing
artifacts are never rewritten. A second-attempt loss is terminal, with no third
attempt granted.

Before retry preparation, `inspect_prediction_retry_evidence` verifies the exact
private descriptor's digest, size, content address, task, run, attempt, worker and
method. Only complete logs-only evidence permits ordinary retry. Native output,
omitted or unknown entries, incomplete inventory and invalid descriptors require
artifact recovery. Legacy failures without a catalog retain their existing
policy. Registered scientific artifacts block retry even without a catalog.

## Cutover and limits

The migration records a cutover timestamp. Legacy jobs without retained call
evidence are not assumed unsubmitted. An existing exact outbox receipt can be
adopted; otherwise operator recovery is required. Keep old untracked submitters
quiescent while integrating the new contract. Applying a migration alone does not
make an existing launcher safe or enable any scheduler.

The SQL contract preserves the reviewed 40-target/80-run initial resource bound
and at most one bounded retry per run. It serializes retry authority by campaign;
Python retains existing duration and weighted cost checks. This source adds no
concurrency, GPU quota, paid inference policy or production activation.

Portable tests use injected SDK records and real PostgreSQL semantics in PGlite.
They cover single grants, uncertainty, immutable science, exact lease checks,
private permissions, loss evidence, retry exhaustion and incomplete inventories.
Hosted SDK integration tests remain with the deployment implementation.
