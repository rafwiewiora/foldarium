# Prediction deployment adapters

This directory is an infrastructure edge around the provider-neutral
`foldarium_pipeline` package. A prediction is always the same versioned
`PredictionTask` JSON, and execution always returns the same `PredictionResult`
dictionary. Modal/GCP settings belong here or in infrastructure configuration;
they must not leak into the scientific task schema.

## Modal bootstrap

The Modal scaffold is safe to prepare before an account is available. It does
not deploy or download models merely by existing in the repository.

1. Install Modal in a deployment-only environment: `python -m pip install modal`.
2. Authenticate with `modal setup`.
3. Create a Modal secret named `foldarium-control-plane` containing
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `FOLDARIUM_STORAGE_BUCKET`.
   Add any other storage credentials required by the core worker to this secret;
   never put values in this repo.
4. Validate with a dry-run task in the core CLI before spending GPU time.
5. Deploy with `modal deploy pipeline/deploy/modal_app.py` from the repository
   root.
6. Bootstrap the OpenFold3 checkpoint cache once with
   `modal run pipeline/deploy/modal_app.py::bootstrap_openfold3_cache`. The
   function writes an explicit `setup_openfold --config` at runtime so both
   `openfold_cache` and `param_directory` point into the mounted Volume.
7. Submit one task with
   `modal run pipeline/deploy/modal_app.py --task-json '<json>'`, or invoke the
   deployed `submit_tasks` function from the control plane.

See Modal's official guides for [GPU functions](https://modal.com/docs/guide/gpu),
[secrets](https://modal.com/docs/guide/secrets), [Volumes](https://modal.com/docs/guide/volumes),
and [scheduled functions](https://modal.com/docs/guide/cron) before production deployment.

OpenFold3 uses the official 0.4-pixi / OpenFold3 0.4.4 OCI index pinned by
immutable digest.
Modal runs under an injected Python 3.12 control interpreter. The upstream OF3 Pixi
environment is activated only around `setup_openfold` and `run_openfold` subprocesses so
its Python and runtime packages cannot shadow Modal's own dependencies.
The initial function requests an A100-40GB, matching upstream's commonly tested
baseline; route genuinely larger targets to a separately costed A100-80GB policy
instead of silently changing the standard campaign runtime.
It also requests eight physical CPU cores and 32 GiB host RAM. Boltz-2 requests
four cores and 16 GiB with its L40S. Both GPU functions have `max_containers=1`
and scale to zero when idle, which is intentionally conservative for the first
credit-limited smoke tests.

Boltz is built with exactly `boltz[cuda]==2.2.1`. Model files persist in
method-specific Modal Volumes to reduce cold starts. Those volumes are caches
only: the deployment wrapper fails closed unless its Supabase publisher is
configured. It atomically claims the deterministic run before GPU execution,
then uploads every durable artifact and writes run state/provenance before
returning success.

Model-command failures are also finalized through the same RPC, with no artifact
rows and a sanitized error summary. Retry policy can then create or reclaim work
deliberately instead of waiting for an invisible failed lease.

Boltz has no separate supported setup command. Its first small, real fixture
prediction is the cache bootstrap/smoke run: use a task with `msa_mode: empty`
and one diffusion sample after the core dry run passes. It downloads into the
mounted Boltz cache and still follows the normal durable publication path. Do
not use an unpublished throwaway run merely to warm the cache.

The weekly schedule is completely absent unless `FOLDARIUM_ENABLE_WEEKLY_CRON=1`
is set when the app is deployed. Once enabled, the default polls every 15 minutes
from Saturday 03:00 through 06:45 UTC. Expected CAMEO publication lag is returned
and logged as `waiting-for-inputs`, not treated as a failed task.
Use `FOLDARIUM_WEEKLY_HOOK=foldarium_pipeline.weekly:modal_weekly_hook`. Planning,
registration, and GPU spend remain independently gated: registration requires
`FOLDARIUM_WEEKLY_REGISTER=1`, and calls are spawned only with
`FOLDARIUM_WEEKLY_SUBMIT=1` after the registration RPC reports success. With the
submit flag absent, the scheduled function returns the target count, accelerator
mix, and maximum GPU-seconds as `planned-not-submitted`. Change
`FOLDARIUM_WEEKLY_CRON` in the environment used by `modal deploy` to move the
schedule without changing core code.

`FOLDARIUM_WEEKLY_GPU_CLASS` is an explicit operator calibration override. The
first production round pins both methods to `l4` and records sampled
`peak_gpu_memory_mib`; a CUDA OOM is stored as `gpu_out_of_memory` and is never
retried automatically. Remove the override only after replacing the provisional
generic sizing ladder with measured, method-specific thresholds.

The deployment adapter embeds only those non-secret weekly switches into the
CPU control image. Supabase credentials are supplied separately by the
`foldarium-control-plane` Secret. Search logs with:

```bash
modal app logs foldarium-predictions --env main --timestamps --search foldarium.weekly
```

Once a registered campaign exists, later ticks exit before crawling CAMEO or
submitting work. Therefore enable registration and GPU submission together only
after approving the displayed bounded budget; a registration-only deployment is
an intentional operator hold point, not an automatic future-submit queue.

### Wednesday reveal

The Wednesday evaluator is a CPU-only Modal function. Its image pins
`gemmi==0.7.3`, `numpy==2.3.2`, and `rdkit==2025.3.6`; it neither reserves a GPU
nor uses either prediction cache Volume. The schedule is absent unless
`FOLDARIUM_ENABLE_WEDNESDAY_REVEAL=1` is set at deploy time. When enabled, the
default `FOLDARIUM_WEDNESDAY_REVEAL_CRON` is `5 0-5 * * 3`: six hourly attempts
from 00:05 through 05:05 UTC on Wednesday, after voting closes at 00:00 UTC.
Each tick has two one-minute Modal infrastructure retries and one active
container, so delayed released coordinates are retried for a bounded window
without creating an unbounded poller.

Publication is a separate mutation gate. With
`FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH` absent or set to `0`, scheduled calls run
the complete evaluation as a dry run and do not call the reveal RPC. Set it to
`1` only in a reviewed deployment that should publish. A manual dry run for an
exact round is:

```bash
modal run --env main pipeline/deploy/modal_app.py::wednesday_reveal_tick \
  --round-id weekly-2026-08-08 --no-publish
```

Use `--publish` only for an explicitly approved manual reveal. If `--round-id`
is omitted, the function derives the round opened on the most recent UTC
Saturday. It reads that exact private round and digest-bound private index,
downloads each original `predicted_complex` by exact `(run_id, sample_id)`, and
uses the stored classic four-character PDB target IDs for RCSB coordinates. A
missing round/index/artifact, digest mismatch, unavailable coordinate, or one
incomplete evaluation aborts the whole call before publication. Repeated calls
after a successful reveal return `already-revealed` without rescoring.

Keep the laptop's default Modal profile on `foldariumtest`. The separately
configured `molspace-production` profile is for Brian's final deployment only;
always pass/verify a profile explicitly before any production command.

## Portability rules

- Supabase is the control/catalog source of truth; object storage is the
  artifact source of truth.
- Inputs and outputs travel through object URIs plus checksums, not a provider
  filesystem.
- Modal Volumes, GCP disks, and container filesystems are replaceable caches or
  scratch space.
- Never bake service keys, signed URLs, proprietary data, or model credentials
  into an image.
- Pin production images by digest and record the digest in result provenance.
- Keep one image per prediction method. Dependency conflicts must not alter the
  shared task/result contract.

The Modal-built Boltz bootstrap image is intentionally convenient for the first
deployment, but a Modal image is not an externally pullable OCI artifact. Before
the GCP cutover, materialize that same pinned recipe as a Foldarium-owned image
in Artifact Registry and point both backends at its digest. The GCP mapping is
documented in [`gcp/README.md`](gcp/README.md).
