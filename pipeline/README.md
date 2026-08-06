# Foldarium prediction pipeline

This directory is the independent, portable beginning of Foldarium's weekly co-folding pipeline. It
does not run in the browser and it does not contain code, credentials, weights, or implementation details
from any external prediction service.

## What is ready

- A versioned, method-neutral target/task/result contract with deterministic run IDs.
- Foldarium-owned input and output adapters for upstream OpenFold3 and Boltz-2 CLIs.
- A backend-independent worker and a no-GPU dry-run CLI.
- A Supabase/Postgres control-plane migration with private worker tables and an atomic published view.
- Thin deployment seams for Modal now and the same container/task contract on GCP later.

This scaffold validates and plans jobs locally. A real GPU run additionally needs the pinned upstream
runtime, durable object upload, Supabase worker credentials, and deployment of an execution wrapper.
No inference is launched merely by installing this package.

## Supabase setup

1. Apply `migrations/001_control_plane.sql` to a staging project.
2. Create a private Storage bucket for prediction results; its name becomes
   `FOLDARIUM_STORAGE_BUCKET` in the worker secret.
3. Have the coordinator insert the campaign, target package, and deterministic task row before submitting
   a GPU call. The migration enforces that duplicated task columns agree with `task_payload`.
4. Give only the coordinator/GPU secret environment `SUPABASE_URL` and
   `SUPABASE_SERVICE_ROLE_KEY`. The browser never receives them.
5. The execution wrapper claims the run, calls the core worker, verifies and uploads every output, and
   invokes `finish_prediction_run` to commit the artifact rows and terminal result atomically.

The storage publisher is implemented and unit-tested without network access. The prerelease intake
coordinator that creates campaign/target/run rows is still a next implementation step; Modal's weekly
hook is deliberately a no-op until that coordinator exists.

## The portability contract

```text
weekly intake -> normalized target -> deterministic prediction task
                                      |               |
                                      v               v
                                Modal adapter     GCP adapter
                                      \               /
                                       same worker + method adapter
                                                |
                                  immutable raw artifacts + result JSON
                                                |
                                  object storage + Supabase control plane
```

The scientific identity includes target content, method/version, pinned container image, and inference
configuration. Retries keep the same `task_id`; a scientific change creates a new one. Raw mmCIF and
confidence files go to object storage. Supabase holds state, hashes, provenance, and object URIs.

Modal Volumes or GCP disks are caches only. They are never the catalog or sole copy of an output. Method
code has no Modal/GCP imports, and the SQL schema stores an opaque `execution_backend` and job ID. This is
what lets the deployment move without changing the benchmark's identity.

## Local setup and dry run

Python 3.11 or 3.12 is sufficient for contract tests and planning:

```bash
cd pipeline
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v

foldarium-pipeline validate-target examples/target.json
foldarium-pipeline make-task examples/target.json \
  --campaign smoke-2026-08 \
  --method boltz2 \
  --method-version 2.2.1 \
  --image ghcr.io/foldarium/boltz2:2.2.1 \
  --config-json examples/boltz2-config.json \
  --output-prefix gs://foldarium-staging/predictions > /tmp/foldarium-task.json
foldarium-pipeline plan /tmp/foldarium-task.json
```

The example sequence and ligand are synthetic packaging fixtures, not a scientific quality test.

## Method runtimes

### OpenFold3

Pin OpenFold3 `0.4.4` and the official Pixi image recorded in
`foldarium_pipeline.methods.openfold3.OPENFOLD3_IMAGE`. The initial policy is one target per A100-40GB
task, one seed, and five diffusion samples. The checkpoint and CCD cache belong on a persistent cache
mount and must not be committed or baked into this repository. The default checkpoint is
`openfold3-p2-155k`; record the installed file's hash in run provenance before production publication.

The example uses the public MSA server only to prove packaging. At weekly scale, generate and store
immutable MSAs keyed by sequence and database provenance, then teach the artifact-localization seam to
materialize them. Do not send non-public sequences to an external MSA service.

### Boltz-2

Pin `boltz[cuda]==2.2.1` in a Python 3.11 image and store its downloaded weights/molecule cache under an
absolute persistent cache path. The starter production policy is five diffusion samples, one parallel
sample, three recycles, 200 sampling steps, and fixed seeds. `msa_mode: empty` is useful only for a GPU
packaging smoke test; production campaigns should use versioned precomputed MSAs.

Both adapters retain raw complex mmCIF as the canonical scientific output. The later evaluation stage
will align to released coordinates, split viewer assets, score ligands, cluster poses, and publish a
redacted quiz manifest.

## Weekly ownership

Foldarium owns the orchestration from end to end:

1. A scheduled intake coordinator records the new public prerelease snapshot and selection policy.
2. It upserts targets and deterministic tasks into Supabase.
3. The configured execution backend claims and runs pending tasks, uploads verified artifacts, then
   commits normalized results.
4. After reference coordinates release, a separate evaluation worker aligns, scores, clusters, and
   creates a publication batch.
5. A privileged publisher atomically exposes the redacted batch to the browser.

Keep scheduler policy out of method adapters. A Modal cron can trigger the coordinator today; Cloud
Scheduler can trigger the same coordinator on GCP. Supabase is the durable owner of schedules already
materialized into work, leases, retries, and publication state.

## Upstream projects and licenses

- [OpenFold3](https://github.com/aqlaboratory/openfold-3) is Apache-2.0; use its official package/image
  and follow the [installation and model setup](https://openfold-3.readthedocs.io/en/latest/Installation.html)
  and citation instructions.
- [Boltz-2](https://github.com/jwohlwend/boltz/tree/v2.2.1) code and weights are MIT; install the released
  package and follow its [prediction contract](https://github.com/jwohlwend/boltz/blob/v2.2.1/docs/prediction.md).

Foldarium depends on those independent public projects at runtime. It does not vendor their weights or
source. Before production, review their current releases and runtime terms, pin images by digest, record
all package/checkpoint hashes, and run one end-to-end staging target for each method.
