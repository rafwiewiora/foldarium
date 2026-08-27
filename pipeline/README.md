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
