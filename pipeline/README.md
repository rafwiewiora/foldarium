# Foldarium prediction pipeline

This package owns the provider-neutral contracts and scientific workflow behind
Foldarium weekly rounds. Installing it does not submit GPU work, mutate a
database, download model weights, or start a scheduler.

## Capabilities

- versioned target, task, result, and artifact contracts;
- deterministic task/run identifiers and content-addressed provenance;
- OpenFold3 and Boltz-2 input/output adapters;
- a local execution wrapper and no-GPU planning path;
- public wwPDB/CAMEO intake;
- blind weekly quiz assembly and Selector-kit generation;
- receptor-aligned, graph-symmetry-aware ligand RMSD evaluation;
- Wednesday reference evaluation and retrospective publication;
- fail-closed post-reveal and blind training-similarity audits;
- optional Smina/ProLIF metrics and audited LLM scoring clients;
- Supabase/Postgres control-plane migrations.

Method adapters contain no scheduler or cloud SDK imports. An execution backend
only needs to materialize a task workspace, invoke the worker, persist verified
artifacts, and report terminal state to the control plane.

## Install and test

Python 3.11 or 3.12:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./pipeline
python -m unittest discover -s pipeline/tests -v
```

Scientific evaluation dependencies are optional:

```bash
python -m pip install -e './pipeline[evaluation]'
python -m unittest discover -s pipeline/tests -v
```

The optional `weekly-llm` extra installs the Cursor SDK adapter. The Claude
adapter uses an independently installed `claude` CLI. Neither adapter runs
unless explicitly invoked.

## Plan without submitting work

```bash
foldarium-pipeline validate-target pipeline/examples/target.json

foldarium-pipeline make-task pipeline/examples/target.json \
  --campaign local-smoke \
  --method boltz2 \
  --method-version 2.2.1 \
  --image ghcr.io/example/boltz2@sha256:replace-me \
  --config-json pipeline/examples/boltz2-config.json \
  --output-prefix file:///tmp/foldarium-output \
  > /tmp/foldarium-task.json

foldarium-pipeline plan /tmp/foldarium-task.json

foldarium-pipeline weekly-plan \
  --release-date 2026-08-29 \
  --max-targets 2 \
  --output /tmp/foldarium-week.json
```

`weekly-plan` performs public downloads and writes a deterministic plan. It does
not write to Supabase or launch predictions.

## Audit published Weekly training similarity

Install the evaluation extra, keep the resumable cache outside Git, and run the
exact post-reveal label separately from the blind proxy:

```bash
PYTHONPATH=pipeline/src python pipeline/scripts/audit_weekly_training_similarity.py \
  --cache-dir /tmp/foldarium-training-cache \
  --output /tmp/foldarium-training-exact.json \
  --mode exact

PYTHONPATH=pipeline/src python pipeline/scripts/audit_weekly_training_similarity.py \
  --cache-dir /tmp/foldarium-training-cache \
  --output /tmp/foldarium-training-blind.json \
  --mode blind
```

The command records search, download, parse, and incomplete-candidate failures
as `unknown`. `--workers`, `--limit`, `--only`, and `--force` support bounded
pilots and resumable reruns. The blind scorer's input type contains only the
archived predicted receptor, predicted pocket, and candidate poses.

Each exact target and blind pose also receives an additive RnP-style
SuCOS-pocket score. It applies Crippen/shape ligand alignment, SuCOS, and
Foldseek-aligned 6 Å pocket query coverage to the same retained top-25
candidates. Its published 25/100 novelty threshold is independent of the
canonical carried-ligand overlap cutoff. This is a controlled RnP-style
approximation, not the published RnP metric: the paper uses PLINDER holo
systems, up to 5,000 Foldseek candidates, MMseqs coverage, PLIP-augmented
pockets, multi-chain matching, and its recorded RDKit 2024.9.6 environment.

### Publish exact training-system overlays

Rerun the exact audit once to materialize its content-addressed overlay cache,
then keep that exact audit JSON immutable for publication and report generation:

```bash
PYTHONPATH=pipeline/src python pipeline/scripts/audit_weekly_training_similarity.py \
  --cache-dir "$CACHE_DIR" \
  --output "$EXACT_AUDIT" \
  --mode exact

SUPABASE_URL="$SUPABASE_URL" \
SUPABASE_SERVICE_ROLE_KEY="$SUPABASE_SERVICE_ROLE_KEY" \
FOLDARIUM_STORAGE_BUCKET="$BROWSER_PUBLIC_BUCKET" \
PYTHONPATH=pipeline/src python pipeline/scripts/publish_weekly_training_overlays.py \
  --exact "$EXACT_AUDIT" \
  --cache-dir "$CACHE_DIR" \
  --manifest "$OVERLAY_MANIFEST"

PYTHONPATH=pipeline/src python pipeline/scripts/report_weekly_training_similarity.py \
  --exact "$EXACT_AUDIT" \
  --blind "$BLIND_AUDIT" \
  --overlay-manifest "$OVERLAY_MANIFEST" \
  --json "$REPORT_JSON" \
  --csv "$REPORT_CSV" \
  --markdown "$REPORT_MARKDOWN"
```

The publisher verifies that `FOLDARIUM_STORAGE_BUCKET` is browser-public and
writes the manifest after each upload, so publication resumes by rerunning the
same command. A resumed publication must use the same immutable exact audit;
the manifest and report reject a different audit digest.

For a version-pinned local or batch Foldseek backend,
`pipeline/scripts/weekly_foldseek_batch.py prepare` emits 100 first-chain query
PDBs plus a digest manifest. Run Foldseek with the documented eight-column
format, then use its `import` command to seed the same fail-closed hit cache.

## Run one task locally

`foldarium-pipeline run` invokes the configured method adapter in the current
environment:

```bash
foldarium-pipeline run /tmp/foldarium-task.json \
  --work-root /tmp/foldarium-work
```

The operator must install the selected upstream predictor and supply its
weights/cache. Keep weights and caches outside this repository. Pin predictor
packages, container images, and checkpoints by immutable version or digest and
record their hashes in result provenance.

## Control plane

Apply `pipeline/migrations/001_control_plane.sql` onward in numeric order to a
staging database. The root `supabase/migrations/` directory contains the web
quiz and publication schema.

Coordinator/worker processes use:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `FOLDARIUM_STORAGE_BUCKET`

These are server-only. Browser clients must receive only the project URL and a
publishable key. Start with a staging project, write gates disabled, and a small
target cap. Verify RLS with independent users before enabling writes.

## Featured questions and reconciliation

`weekly_question_selection.select_weekly_questions` selects up to five human
questions from a complete blind round. Its default uniform draw is reproducible
from the seed and candidate identities. Blind pose-cluster diversity and
cross-method disagreement are recorded as explanatory scores; weighting is an
explicit option. Missing scores do not exclude a question. Selection does not
reduce the full scientific manifest, Selector kit, or benchmark denominator.

`freeze_weekly_featured_questions` stores a content-addressed private audit and
registers a source-bound public marker through a service-role RPC. The browser
uses that marker for navigation and retains canonical full-round question
indices. Historical sessions resume their original full scope.

`weekly_reconciliation.plan_reconciliation` is a pure desired-state planner.
`weekly_reconciliation_store.reconcile_weekly` adds a bounded, leased execution
loop around an operator-supplied executor. Every write gate defaults off; the
default dry run neither enqueues work nor invokes the executor. A benchmark
policy must be explicitly configured before a round can advance to release.
Durable dispatch receipts and idempotent handlers are still needed because a
database lease cannot make external compute dispatch exactly once.

Apply the matching featured-selection migration before the automation-outbox
migration in `supabase/migrations/`. These are prepared source contracts, not an
assertion that an existing production database has been migrated. Provider
launchers and schedules are intentionally outside the public repository.

The isolated PostgreSQL behavioral tests accept a locally installed PGlite module
path as an optional argument:

```bash
node pipeline/tests/check_weekly_featured_database.mjs
node pipeline/tests/check_weekly_automation_database.mjs
```

Install `@electric-sql/pglite` in an external test environment or pass its module
path. These harnesses create disposable databases and use synthetic data; they
do not connect to production. The featured harness uses PostgreSQL's actual
SHA-256 function to test the digest contract.

## Portable execution contract

```text
public intake -> normalized target -> deterministic task
                                          |
                                operator execution backend
                                          |
                                 method-neutral worker
                                          |
                         verified immutable result artifacts
                                          |
                           object storage + SQL control plane
```

Schedulers are intentionally outside this repository. Cron, a CI dispatcher,
Kubernetes, a queue consumer, or a local process can all call the same
coordinator and worker APIs.

## Upstream software

OpenFold3 and Boltz-2 are independent projects and are not vendored here.
Follow their current installation, model, citation, and license requirements.
See the repository-level `THIRD_PARTY_NOTICES.md` before redistributing
structures, weights, or generated predictions.
