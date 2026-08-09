# Foldarium prediction pipeline

This directory is the independent, portable beginning of Foldarium's weekly co-folding pipeline. It
does not run in the browser and it does not contain code, credentials, weights, or implementation details
from any external prediction service.

## What is ready

- A versioned, method-neutral target/task/result contract with deterministic run IDs.
- Foldarium-owned input and output adapters for upstream OpenFold3 and Boltz-2 CLIs.
- A backend-independent worker and a no-GPU dry-run CLI.
- A Supabase/Postgres control-plane migration with private worker tables and an atomic published view.
- Replayable wwPDB/CAMEO Saturday intake, bounded OF3/Boltz planning, and content-addressed registration.
- A blind Saturday-to-Wednesday quiz/vote/reveal contract and normalized public CAMEO AF3 importer.
- A shared receptor-aligned, symmetry-aware ligand RMSD evaluator for Wednesday comparisons.
- A portable, fail-closed public Foldseek client with batched authoritative RCSB cutoff lookups.
- Thin deployment seams for Modal now and the same container/task contract on GCP later.

No inference is launched merely by installing this package or running `weekly-plan`.

## Supabase setup

1. Apply `migrations/001_control_plane.sql` through `004_external_predictions.sql`, in order, to a
   staging project.
2. Create a private Storage bucket for prediction results; its name becomes
   `FOLDARIUM_STORAGE_BUCKET` in the worker secret.
3. Have the coordinator insert the campaign, target package, and deterministic task row before submitting
   a GPU call. The migration enforces that duplicated task columns agree with `task_payload`.
4. Give only the coordinator/GPU secret environment `SUPABASE_URL` and
   `SUPABASE_SERVICE_ROLE_KEY`. The browser never receives them.
5. The execution wrapper claims the run, calls the core worker, verifies and uploads every output, and
   invokes `finish_prediction_run` to commit the artifact rows and terminal result atomically.

The registration RPC creates the snapshot, campaign, target, and run rows in one transaction only after
the replay inputs have been stored by SHA-256. Browser clients can read blind weekly rounds and submit
authenticated votes, but correctness and RMSD remain private until the Wednesday reveal RPC succeeds.

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

# Public downloads + deterministic plan only: no Supabase or Modal writes.
foldarium-pipeline weekly-plan \
  --release-date 2026-08-08 \
  --max-targets 2 \
  --output /tmp/foldarium-week-2026-08-08.json
```

The example sequence and ligand are synthetic packaging fixtures, not a scientific quality test. The
weekly planner fails if even one advertised CAMEO page is unavailable, applies the checked-in
`cameo-drug-like/v2` filter (including the quiz's ≥15-heavy-atom rule), selects at most one ligand target
per distinct polymer set, records unknown prerelease stoichiometry explicitly, and reports the exact GPU
classes and maximum GPU-seconds before submission.

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

Both adapters retain raw complex mmCIF as the canonical scientific output. The evaluation extra
(`pip install -e 'pipeline[evaluation]'`) supplies Gemmi, NumPy, and RDKit for receptor alignment and
graph-symmetry-aware ligand RMSD. On the 36IQ/DM2 calibration target, all five scores are within 0.12 Å
of CAMEO's BiSyRMSD values and preserve the same correct/wrong classification.

## Weekly ownership

Foldarium owns the orchestration from end to end:

1. A scheduled intake coordinator records the new public prerelease snapshot and selection policy.
2. It upserts targets and deterministic tasks into Supabase.
3. The configured execution backend claims and runs pending tasks, uploads verified artifacts, then
   commits normalized results.
4. After reference coordinates release, a separate evaluation worker aligns, scores, clusters, and
   imports public CAMEO AF3 models, and creates a reveal manifest.
5. A privileged publisher exposes the redacted blind round on Saturday; Postgres accepts votes only
   before the Wednesday close and exposes answers/vote totals only after the reveal transaction.

## Weekly timing and safety switches

wwPDB prerelease and CAMEO target selection begin Saturday at 03:00 UTC. CAMEO accepts participant
predictions until Wednesday 00:00 UTC and evaluates after coordinates release; public AF3 outputs must be
treated as Wednesday data. The Saturday quiz therefore uses Foldarium's own OF3/Boltz poses. Wednesday
imports CAMEO AF3, scores all methods against the released coordinates, and reveals results.

Modal uses three independent gates:

- `FOLDARIUM_ENABLE_WEEKLY_CRON=1` adds the Saturday 03:00–06:45 UTC 15-minute poll schedule at deployment;
- `FOLDARIUM_WEEKLY_REGISTER=1` permits immutable Storage/Supabase registration;
- `FOLDARIUM_WEEKLY_SUBMIT=1` permits GPU spawning, and only after registration reports success.

Saturday quiz assembly can additionally opt into pose-only smina and ProLIF
metrics with `include_pose_metrics=True`. Those metrics run serially in the
separate CPU-only `foldarium-weekly-scoring` Modal app, use each pose's exact
predicted protein, and have no reference-coordinate or database access. The
default remains off; see `deploy/SCORING.md` for the bounded canary/deployment
workflow.

Set `FOLDARIUM_WEEKLY_HOOK=foldarium_pipeline.weekly:modal_weekly_hook`. A new deployment should first
leave registration/submission off, inspect the returned budget, then test registration in the test
project, and only then enable submission. `FOLDARIUM_WEEKLY_MAX_TARGETS` is the hard per-week target cap.

Wednesday evaluation has independent controls. `FOLDARIUM_ENABLE_WEDNESDAY_REVEAL=1` installs the
CPU-only schedule; its default `FOLDARIUM_WEDNESDAY_REVEAL_CRON` runs hourly from 00:05 through 05:05 UTC
on Wednesday, giving delayed coordinate releases a bounded retry window after the 00:00 UTC close. The
evaluation image pins `gemmi==0.7.3`, `numpy==2.3.2`, and `rdkit==2025.3.6`. Publication remains off unless
`FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH=1` is present, or an operator explicitly passes `--publish` to a
manual invocation; `--no-publish` performs the full scoring dry run without the reveal RPC.

The reveal worker derives the most recent Saturday campaign when no round ID is supplied, resolves its
newest immutable public round by `opens_at`, reads that exact private round and checksum-bound private
index with the service role, then resolves every original
`predicted_complex` by exact `(run_id, sample_id)` and recorded digest. Classic four-character PDB target
IDs select the released RCSB coordinates. Missing/delayed coordinates, an incomplete item, a missing
artifact, or a checksum mismatch aborts the entire reveal before mutation. A round already revealed with
the same canonical content returns idempotently without downloading or rescoring predictions.

Keep scheduler policy out of method adapters. A Modal cron can trigger the coordinator today; Cloud
Scheduler can trigger the same coordinator on GCP. Supabase is the durable owner of schedules already
materialized into work, leases, retries, and publication state.

The first production deployment intentionally has only the first gate enabled. It reports
`waiting-for-inputs` or a bounded `planned-not-submitted` budget in Modal logs without database writes or
GPU work. Registration and submission require a separate redeploy so spend cannot be enabled accidentally.

Historical novelty uses `foldarium_pipeline.foldseek` against the public Foldseek `pdb100` service,
filters candidates to RCSB `initial_release_date < 2021-09-30`, carries ligand-bearing training structures
into the query receptor frame, and labels a target novel only when the best in-pocket ligand volume
overlap is below 0.25 (or there is a confirmed empty pre-cutoff result). API or parsing failures remain
unknown. Cache the Foldseek results and downloaded PDB structures. A local cutoff-specific Foldseek
database is a future reproducibility optimization, not a prerequisite for the first catch-up.

## Upstream projects and licenses

- [OpenFold3](https://github.com/aqlaboratory/openfold-3) is Apache-2.0; use its official package/image
  and follow the [installation and model setup](https://openfold-3.readthedocs.io/en/latest/Installation.html)
  and citation instructions.
- [Boltz-2](https://github.com/jwohlwend/boltz/tree/v2.2.1) code and weights are MIT; install the released
  package and follow its [prediction contract](https://github.com/jwohlwend/boltz/blob/v2.2.1/docs/prediction.md).

Foldarium depends on those independent public projects at runtime. It does not vendor their weights or
source. Before production, review their current releases and runtime terms, pin images by digest, record
all package/checkpoint hashes, and run one end-to-end staging target for each method.
