# Composed Weekly lifecycle acceptance

The existing suite tests native prediction/assembly, immutable uploads, selectors,
private evaluation, provider checkpoints and public projections extensively.
Its PostgreSQL checks originally exercised each migration against separate,
reduced schemas. In particular, the automation test replaced the underlying
reveal RPC with a simple status update; its artifact tests used fake RPC receipts.
Those tests remain useful for focused failures, but did not prove that the
actual guards accepted the same artifacts throughout a completed lifecycle.

`tests/check_weekly_lifecycle_acceptance.mjs` closes that specific coverage gap.
It applies the complete migration directory, in order, to a disposable persistent
PGlite PostgreSQL database. It uses the real Python artifact builders, actual
catalog/RPC guards, the real desired-state planner, and the JavaScript archive,
featured-result and cofold benchmark consumers.

The scenario retains six fixture questions throughout Preview promotion,
production, selector kit, evaluation and benchmarks. The human assignment freezes
five of those six. A fake Messages transport supplies six responses through the
actual outer benchmark adapter and unattended runner. Their calls cross a local
standard-input/output bridge into the actual PostgreSQL RPCs. An injected
registration failure leaves the completed budget ledger and responses reusable. The test closes/reopens PostgreSQL after inference and before
artifact registration, proving the separate first-launch budget authority
survives a process restart. Removing the entire execution state then makes the
real outer adapter reject a fresh budget before launching inference. Restoring
the exact retained checkpoint allows completion. Duplicate registrations and
completed item replay create neither a second grant nor a second request.

A real authenticated fixture session submits five typed V2 human votes and retries
each identical request. Four omit optional UI state; one includes contradictory
UI state. Validated typed vote provenance is authoritative. The archive and
featured-result projections must recognize this human assignment as complete at
five while retaining six questions for the model and method benchmark.

It then exercises closed-but-unrevealed ingestion, exact receipt verification,
private evaluation, guarded reveal, immutable retrospective publication, and
public/featured/cofold projections. The real Supabase source adapter reads the
local SQL rows and verified typed-vote/benchmark proofs; the Python-normalized
source must equal the independently constructed PostgreSQL source exactly. The planner must propose ingestion before
reveal, reveal only once evaluation and receipts exist, and no further action
for the completed round. A repeated raw reveal RPC is rejected after success;
the restarted planner recognizes that state instead of reissuing the RPC.

Adversarial cases reject a changed draw, changed frozen model, wrong artifact
hash, early benchmark ingestion, reduced reveal population, and a changed
close time. Anonymous access cannot read private evaluations or frozen model
jobs, and the blind round view has no answers or private metadata. The final
public detail and five-question results pass the application's strict response
safety checks. The cofold projection still counts all six targets per method.

## Reproduce

From the repository root, using Python 3.11 and Node 24:

```sh
python -m pip install -e './pipeline[evaluation]'
npm install --prefix /tmp/foldarium-pglite-acceptance \
  --no-save --ignore-scripts --no-audit --no-fund @electric-sql/pglite@0.3.14
node pipeline/tests/check_weekly_lifecycle_acceptance.mjs \
  /tmp/foldarium-pglite-acceptance/node_modules/@electric-sql/pglite/dist/index.js \
  "$(command -v python)"
```

The existing scientific-evaluation CI job runs this check after its unit suite;
it installs PGlite only beneath `RUNNER_TEMP`. No credentials are supplied. The
scenario denies socket connections/DNS in Python and socket/HTTP/fetch access in
Node, including DNS and UDP, and deliberately probes those guards before loading
application modules. Each Python phase has a 120-second deadline. Child environments include only
PATH and the test import configuration, excluding inherited provider credentials
and proxies. It invokes no Modal function and uses no GPU or paid provider. Its output identifies a temporary directory containing synthetic
artifacts and the local PostgreSQL database for diagnosis.

## Explicit limits

This acceptance begins with small assembled native fixtures. It does not rerun
OpenFold/Boltz, Foldseek, Smina or the geometry evaluator; those boundaries retain
their existing independent scientific tests. Its evaluator is explicitly a
fixture, while private evaluation artifact construction, validation and catalog
registration are real. It does not claim to test network isolation, Vercel HTTP
routing, Storage availability, or Modal worker scheduling.

Supabase auth/users and Storage bucket schemas are local fixtures. PGlite 0.3.14
lacks pgcrypto, so only its extension-install checks are skipped. The SHA-256
wrapper uses PostgreSQL's native `sha256(bytea)`; a deterministic fixture seed
allows the HMAC secret table to initialize. A SHA-256 HMAC adapter built from
PostgreSQL byte operations is checked against Node crypto before exercising the
authenticated human-session/vote RPCs. External authentication and production
secret generation remain outside this test. All application SQL functions, triggers, constraints and grants
remain unchanged, including the real reveal function.

The bridge carries canonical manifests as text and exports PostgreSQL JSON text
for Python, preserving numeric spellings such as `0.0` that a JavaScript object
round trip would change. It does not relax artifact digest checks to accommodate
that test transport.

Prerelease download readiness and prediction execution remain separate boundaries.
`test_weekly.py` covers incomplete advertising and missing prerelease inputs;
`test_weekly_reconciliation.py` verifies that a successful transport response
without a durable state change remains waiting instead of falsely completing an
action. Those tests do not justify inventing a new Saturday clock gate. Actual
Modal worker dispatch, GPU outputs and Storage failures retain their focused
dispatch, worker and assembly tests rather than being mocked into this scenario.
