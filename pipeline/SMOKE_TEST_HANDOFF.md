# Foldarium pipeline and two-job Modal smoke-test handoff

Last updated: 2026-08-06 (America/Los_Angeles)

> **Repository status change.** This pipeline work is no longer pushed to the public
> `rafwiewiora/foldarium` remote. Commits stay local until a private remote exists. Do not push
> `pipeline/...` to `origin` (`https://github.com/rafwiewiora/foldarium`).

This is the working handoff for the next engineer/model. It records the decisions made, what is already
implemented and pushed, and the exact remaining work to run **one Boltz-2 job and one OpenFold3 job** in
the credit-limited Modal test workspace.

## Objective and hard boundaries

Foldarium is an independent public project. It must be able to run co-folding methods itself on any
supported compute backend.

- OpenFold Portal is a separate organization/service. Foldarium may later import prediction artifacts
  through a normalized external-provider adapter.
- Do **not** copy Portal source, private PR content, endpoints, credentials, internal schemas, or other
  implementation details into this repository.
- The adapters here use only the independent public OpenFold3 and Boltz projects.
- Vercel/GitHub Pages serves the browser UI. It never runs inference.
- Supabase is the durable control plane/catalog. Raw molecular artifacts go to object storage.
- Modal is the first execution backend, not the owner of pipeline state.
- A later move to GCP must reuse the same normalized tasks, method adapters, OCI image identities, worker,
  artifact checksums, and result records.

## Repository and pushed state

Repository:

```text
/Users/rafalwiewiora/repos/foldarium
https://github.com/rafwiewiora/foldarium
branch: main
```

Local commits, newest first. Everything above `7d34423` is **local only and must not be pushed to the
public remote**:

```text
a1e3b2d Generate control-plane staging rows from planned tasks   (local only)
cf81d1b Bound GPU containers and add a synchronous run entrypoint (local only)
7d34423 Document Modal smoke-test handoff
7216fca Disable weekly Modal cron by default
760cd69 Bound Modal smoke-test resources
c4a30b9 Use standard A100 profile for OpenFold3
07775e4 Add portable cofolding pipeline scaffold
```

`origin/main` is `7d34423`.

Important: other work is modifying many `data_rnp_aligned/` and `prep/` files in the same worktree. Those
changes are unrelated to the pipeline work and were deliberately left untouched. Check status and stage
only explicit `pipeline/...` paths; never use `git add -A` for this task.

## What is implemented

### Portable core

`pipeline/src/foldarium_pipeline/` contains:

- `contracts.py`: versioned target/task contracts and deterministic content-derived run IDs;
- `methods/openfold3.py`: original Foldarium translator/CLI planner/output collector for public OpenFold3;
- `methods/boltz2.py`: original Foldarium translator/CLI planner/output collector for public Boltz-2;
- `worker.py`: backend-independent subprocess execution and normalized successful/failed results;
- `supabase.py`: stdlib-only Supabase REST/Storage publisher;
- `execution.py`: interfaces for execution, object storage, and control-plane implementations;
- `staging.py`: derives campaign/target/run rows from a validated task and renders idempotent
  registration SQL; has no database connectivity by design;
- `cli.py`: local validation, deterministic task creation, staging SQL, and no-GPU planning.

No Modal, GCP, or Supabase SDK types are present in the scientific adapters.

### Supabase durability path

`pipeline/migrations/001_control_plane.sql` defines:

- campaigns;
- targets;
- deterministic prediction runs;
- prediction artifacts;
- publication batches;
- bounded run leases through `claim_prediction_run`;
- atomic terminal-state/artifact recording through `finish_prediction_run`;
- atomic redacted publication through `publish_foldarium_batch`;
- private RLS-enabled control tables and an anonymous published-items view.

The GPU wrapper follows this order:

1. Resolve Supabase credentials before spending GPU time.
2. Claim the existing deterministic run with a bounded lease.
3. Execute the method adapter.
4. Re-hash every local output and verify its declared size/path.
5. Upload to a content-addressed Storage key: `sha256/<first-two>/<full-sha256>`.
6. If a retry finds that key already present, download and verify the existing bytes.
7. Atomically insert artifact metadata and mark the run succeeded or failed.

Service-role credentials stay in request headers only and are explicitly blocked from result
serialization. They must never enter task JSON, browser code, logs, or git.

### Modal deployment seam

`pipeline/deploy/modal_app.py` currently defines:

- app name: `foldarium-predictions`;
- official OpenFold3 0.4.4 Pixi image pinned by OCI index digest;
- Boltz image built from `boltz[cuda]==2.2.1` on Python 3.12;
- separate persistent cache Volumes for OF3 and Boltz;
- explicit OF3 checkpoint/cache bootstrap;
- OF3 GPU: A100-40GB, 8 physical CPU cores, 32 GiB host RAM;
- Boltz GPU: L40S, 4 physical CPU cores, 16 GiB host RAM;
- `GPU_FUNCTION_TIMEOUT_SECONDS = 20 * 60` as the outer container ceiling on both GPU functions;
- a synchronous `run_task` local entrypoint that blocks on `.remote()` and prints the terminal result;
- `max_containers=1` on each GPU function;
- scale-to-zero behavior (no warm containers requested);
- weekly cron completely absent unless `FOLDARIUM_ENABLE_WEEKLY_CRON=1` is present at deploy time.

The user-provided dashboard URL is:

```text
https://modal.com/apps/foldariumtest/main
```

Interpret `foldariumtest` as the Modal workspace/profile and `main` as the environment. The deployed app
will be named `foldarium-predictions` within that environment.

### GCP portability

`pipeline/deploy/gcp/README.md` maps the same contracts to GCP Batch or Cloud Run Jobs, Artifact Registry,
GCS, Secret Manager, and Cloud Scheduler. The preferred long-running GPU backend is GCP Batch. Modal
Volumes and GCP disks are caches only; Supabase/object storage remains authoritative.

## Upstream runtime pins

### OpenFold3

```text
version: 0.4.4
image: docker.io/openfoldconsortium/openfold3:0.4-pixi@
       sha256:9bc891b799285f0edae94f9f3f05ffcb88f29dc8e758248ce384c64f80e16eec
checkpoint: openfold3-p2-155k
license: Apache-2.0
```

The bootstrap function generates a runtime setup JSON with both `openfold_cache` and `param_directory`
inside `/cache/openfold`; setting `OPENFOLD_CACHE` alone is not sufficient for `setup_openfold` defaults.
Do not commit the checkpoint or CCD cache.

### Boltz-2

```text
package: boltz[cuda]==2.2.1
model: boltz2
cache: /cache/boltz
license for code and weights: MIT
```

Boltz automatically downloads its structure checkpoint, affinity checkpoint, and molecule data on first
prediction (roughly 6 GB total at the time of research). It has no separate supported setup command in
the current scaffold, so the first tiny prediction also warms the cache. Do not commit weights.

## Validation already completed

The local suite has 36 passing tests on Python 3.11:

```bash
cd /Users/rafalwiewiora/repos/foldarium
PYTHONPATH=pipeline/src python3.11 -m unittest discover -s pipeline/tests -v
```

Coverage includes:

- deterministic identities and tamper rejection;
- OF3/Boltz input planning and output collection;
- publishable launch/command failures;
- atomic Supabase claim/finish RPC shapes;
- path traversal rejection;
- SHA-256/size verification;
- idempotent Storage conflict verification;
- credential redaction and serialization refusal;
- staging-row derivation, digests, per-method checkpoint identity, and SQL escaping.

The main pipeline scaffold test workflow and Pages deployment passed before the final two Modal-guard
commits. Re-run the pipeline test workflow after any new source change.

### Control plane verified against real Postgres

The migration and generated staging SQL were applied to a throwaway `postgres:16-alpine` container and
the full lifecycle was exercised without spending any GPU credits:

- the migration applies cleanly (its Supabase-role grants are correctly skipped on plain Postgres);
- the generated staging script applies cleanly and satisfies every payload/column drift constraint;
- `claim_prediction_run` moves the run to `running` with `attempt_count = 1`;
- re-applying the staging script while a run is `running` does **not** reset it;
- a second claim is refused because `max_attempts = 1`;
- `finish_prediction_run` records both artifact rows, releases the lease, and is idempotent on repeat.

This is the cheapest place to catch schema mistakes. Re-run it after any migration or staging change.

## Local Modal CLI state

An ignored virtual environment exists at:

```text
pipeline/.venv
```

It contains Modal client `1.5.3`. This Intel Mac could not build the Rust-backed newest `cbor2`, so the
environment intentionally installed `cbor2==5.9.0` first; Modal accepts it.

The client is now authenticated (2026-08-06):

```text
Workspace: foldariumtest
User:      rafwiewiora
Environment: main (the only environment, active)
```

## Supabase prerequisites

The user designated this project for the smoke test:

```text
https://supabase.com/dashboard/project/wwentnogbknrbmxhfgbg
SUPABASE_URL = https://wwentnogbknrbmxhfgbg.supabase.co
```

Still outstanding: the migration has not been applied, the test bucket does not exist, the Modal secret
has not been created, and no run rows have been inserted. Setup:

1. Apply `pipeline/migrations/001_control_plane.sql` in the Supabase SQL editor.
2. Create a **private** Storage bucket:

   ```text
   foldarium-predictions-test
   ```

3. In Modal workspace `foldariumtest`, environment `main`, create a custom secret named:

   ```text
   foldarium-control-plane
   ```

4. Put these values directly in the Modal secret UI, never in chat or git:

   ```text
   SUPABASE_URL
   SUPABASE_SERVICE_ROLE_KEY
   FOLDARIUM_STORAGE_BUCKET=foldarium-predictions-test
   ```

5. Insert a staging campaign, target, and two prediction-run rows before launching Modal. The publisher
   refuses unknown/unclaimed tasks by design.

The enqueue helper now exists: `foldarium_pipeline.staging`, exposed as `cli.py stage-sql`. It derives
every run column from the validated task, so the payload and its searchable columns cannot drift, and
renders one idempotent transaction. Existing runs are never modified on conflict. Do not weaken the
claim-before-run behavior to bypass this prerequisite.

## Exact two-job smoke-test policy

Budget: approximately USD 20 in Modal credits. Run exactly one job per method, sequentially.

Use the same synthetic packaging target already committed at:

```text
pipeline/examples/target.json
target_id: foldarium-smoke-001
protein: 29 residues
ligand: ethanol (CCO)
```

This is a packaging/integration smoke test, not a scientific-quality benchmark.

### Boltz-2 smoke configuration

Recommended configuration:

```json
{
  "diffusion_samples": 1,
  "max_parallel_samples": 1,
  "msa_mode": "empty",
  "recycling_steps": 1,
  "sampling_steps": 20,
  "seed": 0,
  "step_scale": 1.5
}
```

Set task resources:

```json
{
  "timeout_seconds": 900
}
```

Run Boltz first because it is the less expensive/simpler end-to-end check. Verify its run and artifacts
in Supabase before spending on OF3.

### OpenFold3 smoke configuration

Recommended configuration:

```json
{
  "checkpoint": "openfold3-p2-155k",
  "diffusion_samples": 1,
  "model_seeds": 1,
  "msa_mode": "none"
}
```

Set task resources:

```json
{
  "timeout_seconds": 900
}
```

Do not call an external MSA server for this smoke test. After the CPU-only checkpoint bootstrap succeeds,
run exactly one OF3 GPU task and verify it independently.

### Cost guardrails

At the checked Modal rates:

```text
A100-40GB: $0.000583/sec -> $0.5247 for 900 GPU seconds
L40S:       $0.000542/sec -> $0.4878 for 900 GPU seconds
```

CPU, host memory, image building, downloads, and Volume storage add cost, but two small bounded jobs
should remain comfortably within USD 20.

The outer-timeout hardening is done: both GPU functions now use a 20-minute ceiling
(`GPU_FUNCTION_TIMEOUT_SECONDS`), which also caps the derived run lease, and `max_containers=1` is
retained. The inner subprocess timeout is no longer the only bound.

## Task/run registration details

Use `make_prediction_task(...)` so each run ID is deterministic from campaign, target, method version,
runtime identity, and method configuration. `cli.py make-task` now takes `--resources-json`, so the task
timeout is set without hand-editing a generated task ID.

`stage-sql` populates and keeps consistent, for each `prediction_runs` row:

```text
run_id                    task.task_id
target_id                 task.target.target_id
task_payload              complete normalized task JSON
task_sha256               SHA-256 of canonical task JSON
method                    task.method
method_version            task.method_version
adapter_version           Foldarium core git revision/package version
method_configuration      task.config
method_config_sha256      SHA-256 of canonical config JSON
status                    pending or queued
max_attempts              1 for this smoke test
execution_backend         modal
image_ref                 task.container_image
checkpoint_ref            openfold3-p2-155k for OF3; appropriate Boltz identity otherwise
input_uri                 immutable target-package URI, or an explicit inline-contract URI for smoke only
input_sha256              SHA-256 of normalized target JSON
output_prefix             task.output_uri_prefix
```

The OpenFold task must record the exact pinned image above. Modal builds Boltz from a recipe rather than a
pullable OCI digest; record a clear smoke-only recipe identity including Boltz `2.2.1` and the Foldarium
core revision, then replace it with an Artifact Registry OCI digest before GCP/production use.

The database constraints intentionally reject drift between `task_payload` and duplicated searchable
columns.

## Generating the two smoke tasks

The configs are committed at `pipeline/examples/smoke/`. This regenerates both task payloads and the
staging script; the run IDs are deterministic, so re-running it must reproduce them exactly.

```bash
cd /Users/rafalwiewiora/repos/foldarium
OF3_IMAGE="docker.io/openfoldconsortium/openfold3:0.4-pixi@sha256:9bc891b799285f0edae94f9f3f05ffcb88f29dc8e758248ce384c64f80e16eec"
BOLTZ_IMAGE="modal-recipe://foldarium/boltz2?package=boltz%5Bcuda%5D%3D%3D2.2.1&python=3.12&core=7d34423"
PREFIX="supabase://foldarium-predictions-test/runs"

PYTHONPATH=pipeline/src python3.11 -m foldarium_pipeline.cli make-task pipeline/examples/target.json \
  --campaign foldarium-smoke --method boltz2 --method-version 2.2.1 \
  --image "$BOLTZ_IMAGE" --config-json pipeline/examples/smoke/boltz2.config.json \
  --resources-json pipeline/examples/smoke/resources.json --output-prefix "$PREFIX" > boltz2.task.json

PYTHONPATH=pipeline/src python3.11 -m foldarium_pipeline.cli make-task pipeline/examples/target.json \
  --campaign foldarium-smoke --method openfold3 --method-version 0.4.4 \
  --image "$OF3_IMAGE" --config-json pipeline/examples/smoke/openfold3.config.json \
  --resources-json pipeline/examples/smoke/resources.json --output-prefix "$PREFIX" > openfold3.task.json

PYTHONPATH=pipeline/src python3.11 -m foldarium_pipeline.cli stage-sql \
  boltz2.task.json openfold3.task.json \
  --adapter-version "foldarium-pipeline 0.1.0+7d34423" \
  --campaign-name "Foldarium Modal smoke test" > staging.sql
```

Expected deterministic run IDs at core revision `7d34423`:

```text
boltz2     run_80d9f22c2fcb606536750d0b
openfold3  run_f04f63693b4a28984e0631f1
```

The task payloads and staging script are generated build products; keep them out of the repository. The
`modal-recipe://` image identity is smoke-only and must be replaced by an Artifact Registry OCI digest
before GCP/production use.

Set `core=` to the revision actually being deployed: `_add_core` copies the whole `foldarium_pipeline`
package into the image, so the deployed revision is part of the image's content identity even when the
method adapters themselves did not change. Because `container_image` feeds the task hash, changing it
changes both run IDs — regenerate the tasks and the staging script together, and stage them before
deploying. The IDs above correspond to `core=7d34423`.

## Safe execution order

Do these in order and stop on the first failure:

1. ~~Confirm local Modal token/workspace/environment.~~ Done: `foldariumtest` / `main`.
2. ~~Patch the outer GPU timeout to about 20 minutes.~~ Done in `cf81d1b`.
3. ~~Confirm the Supabase staging project with the user.~~ Done: project `wwentnogbknrbmxhfgbg`.
4. Apply the migration and create the private bucket.
5. Create the Modal secret in the dashboard.
6. ~~Generate the deterministic campaign/target/two task payloads and staging upserts.~~ Done in
   `a1e3b2d`; see the commands above.
7. Apply the staging rows and verify both runs are claimable but not running.
8. Parse/import the Modal app locally without deploying to catch SDK errors.
9. Deploy to environment `main`:

   ```bash
   pipeline/.venv/bin/modal deploy --env main pipeline/deploy/modal_app.py
   ```

10. Confirm no weekly schedule is registered and GPU functions have `max_containers=1`.
11. Bootstrap OF3 cache without a GPU:

    ```bash
    pipeline/.venv/bin/modal run --env main \
      pipeline/deploy/modal_app.py::bootstrap_openfold3_cache
    ```

12. Run the single Boltz task synchronously and keep its printed result:

    ```bash
    pipeline/.venv/bin/modal run --env main \
      pipeline/deploy/modal_app.py::run_task --task-path boltz2.task.json
    ```

    Do not submit OF3 concurrently. The asynchronous `submit` entrypoint still exists but should not be
    used while credits are metered.
13. Verify Boltz:
    - one succeeded run row;
    - one normalized sample;
    - mmCIF and confidence artifact rows;
    - corresponding private Storage objects whose hashes match;
    - cache Volume populated;
    - actual cost/runtime recorded.
14. Only then submit the single OF3 task and perform the same checks.
15. Stop. Do not enable the weekly cron or submit additional samples.

The synchronous `run_task` entrypoint now exists and waits for the remote result. It reuses the same
claim/publish flow and hard budget timeout as `submit`; it only changes how the operator observes the run.

## Expected Modal commands after authentication

Read-only checks:

```bash
pipeline/.venv/bin/modal token info
pipeline/.venv/bin/modal profile current
pipeline/.venv/bin/modal environment list
pipeline/.venv/bin/modal secret list --env main
pipeline/.venv/bin/modal volume list --env main
```

CLI spelling may differ slightly in Modal `1.5.3`; use `--help` before mutating commands. Never include
secret values in shell history or captured output.

Deployment:

```bash
pipeline/.venv/bin/modal deploy --env main pipeline/deploy/modal_app.py
```

Do not set either of these during the smoke deployment:

```text
FOLDARIUM_ENABLE_WEEKLY_CRON
FOLDARIUM_WEEKLY_HOOK
```

## Success criteria

The smoke test is complete only when all of the following are true for each method:

- the exact deterministic task was claimed once;
- the method CLI exited successfully within the hard budget timeout;
- at least one canonical complex mmCIF was collected;
- its confidence JSON was paired to the sample;
- every file was re-hashed locally and uploaded to the private bucket;
- the Supabase artifact metadata matches stored bytes;
- `finish_prediction_run` atomically marked the run `succeeded`;
- no service key or signed URL appears in source, task JSON, result JSON, or logs;
- actual Modal cost/runtime is recorded before any further work is authorized.

A scientifically plausible pose is **not** required for this synthetic target. This test proves packaging,
execution, persistence, provenance, and portability.

## Known gaps after these two jobs

The following remain intentionally out of scope for the two-job smoke test:

- Saturday wwPDB prerelease intake coordinator;
- production precomputed MSA generation/cache sharing;
- Wednesday reference-coordinate ingestion;
- pose alignment, ligand extraction, RMSD scoring, and clustering integration;
- publication manifest generation and browser database loading;
- production OCI build/promotion for Boltz;
- GCP execution adapter implementation;
- automated retry/cost policy and monitoring;
- external-provider import adapters.

Those should build on the same contracts rather than altering them for Modal.

## Primary documentation paths

```text
pipeline/README.md
pipeline/deploy/README.md
pipeline/deploy/modal_app.py
pipeline/deploy/gcp/README.md
pipeline/migrations/001_control_plane.sql
pipeline/migrations/README.md
pipeline/src/foldarium_pipeline/staging.py
pipeline/examples/smoke/
DEPLOYMENT.md
```
