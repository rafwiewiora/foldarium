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

The default Saturday 06:00 UTC cron is a deployment-owned seam. It is a no-op
until `FOLDARIUM_WEEKLY_HOOK=module:function` is supplied in the Modal secret.
The hook returns planned task JSON values; it must use deterministic task IDs so
retries and duplicate scheduler events remain idempotent. Change
`FOLDARIUM_WEEKLY_CRON` in the environment used by `modal deploy` to move the
schedule without changing core code.

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
