# GCP execution mapping

GCP is another execution backend, not a second prediction pipeline. It consumes
the same immutable method images, `PredictionTask` JSON, and
`PredictionResult` dictionary as local and Modal execution.

## Runtime mapping

| Portable contract | Modal bootstrap | GCP |
| --- | --- | --- |
| Method image | `openfold3_image`, `boltz2_image` | Artifact Registry image pinned as `...@sha256:...` |
| One task | one GPU function call | one Batch task or one Cloud Run Job task |
| Scratch | `/tmp/foldarium` | ephemeral task disk at `/tmp/foldarium` |
| Model cache | disposable Modal Volume | staged GCS cache or pre-populated disk/image layer |
| Durable artifacts | object URI in task/result | GCS URI (or Supabase Storage URI) in task/result |
| Run/catalog state | Supabase | the same Supabase project |
| Secrets | Modal Secret | Secret Manager, injected at runtime |
| Weekly trigger | Modal Cron adapter | Cloud Scheduler to Workflows/Cloud Run, outside core |

Cloud Batch is the safer default for long, variable-duration GPU predictions.
Cloud Run Jobs can use the same image and payload for workloads that fit its GPU
resource and task-timeout limits. The choice is an execution policy; it must not
change model inputs, artifact names, IDs, or scientific provenance.

Relevant GCP primitives are [Batch container jobs](https://cloud.google.com/batch/docs/create-run-basic-job),
[Cloud Run Jobs](https://cloud.google.com/run/docs/create-jobs), and
[Cloud Scheduler](https://cloud.google.com/scheduler/docs/configuring/cron-job-schedules).

## Image promotion

Use two Foldarium-owned Artifact Registry images, one per method:

1. Derive the OpenFold3 runtime from the exact official base recorded in
   `modal_app.py`:
   `openfoldconsortium/openfold3:0.4-pixi@sha256:9bc891b799285f0edae94f9f3f05ffcb88f29dc8e758248ce384c64f80e16eec`.
2. Build the Boltz runtime from a reviewed CUDA/Python base and install exactly
   `boltz[cuda]==2.2.1`.
3. Copy only the public `foldarium_pipeline` package and a small container
   entrypoint into both images. Do not copy datasets, secrets, or weights.
4. Test each image locally against a fixture task, push it once, resolve the
   Artifact Registry digest, and use that immutable digest in Modal and GCP
   configuration.
5. Record the method version, base image digest, final image digest, and core
   revision in every result.

During initial Modal-only use, Modal builds the Boltz recipe and layers the core
onto the official OpenFold3 image. A GCP migration should first materialize
those recipes as normal OCI images; scientific code and task records do not
change. Once published, prefer those same Artifact Registry digests on both
providers so runtime parity is literal rather than just recipe-equivalent.

## Job contract

Keep job launch data small. The scheduler serializes one canonical task JSON
object per prediction and passes it to the container directly or by a short-lived
object URI. The container calls:

```python
from foldarium_pipeline.worker import execute_task_json

result = execute_task_json(task_json, work_root="/tmp/foldarium", dry_run=False)
```

The core worker downloads declared inputs, verifies checksums, and runs the
selected adapter. Before GPU execution, the execution wrapper uses the same
publisher interface as Modal to acquire a time-bounded, idempotent run lease.
Afterward it uploads raw/viewer-ready artifacts and transactionally writes the
result before returning. The container should also emit the result as one JSON
object on stdout for Batch logs. Provider identifiers (Batch job ID, Cloud Run
execution ID, region, GPU) belong in execution provenance, not in the
deterministic task ID.

Retries must submit the identical task ID and object prefix. Database uniqueness
constraints and checksum-verified uploads make repeated scheduler events safe.
Mark success only after durable uploads and the Supabase status transaction
complete. A local process exit, Modal return value, or GCP job success alone is
not a durable publication signal.

The shared finish RPC records both successful and failed terminal results.
Retry policy remains a coordinator concern and is identical across backends.

## Storage and control plane

The first GCP version can keep Supabase unchanged and store large artifacts in
GCS. Store `gs://` object identity plus checksum in Supabase; issue signed HTTPS
URLs only at the API boundary for browsers. Never persist an expiring signed URL
as artifact identity.

If artifacts initially remain in Supabase Storage, the GCP worker uses the same
storage adapter as Modal. Migrating to GCS is then a storage-adapter/configuration
change. If the catalog later moves from Supabase Postgres to Cloud SQL, keep the
control-plane interface and migrations stable and replace only its connection
adapter.

For caches, prefer staging required weights/MSAs from versioned GCS objects onto
local SSD at task start. A shared FUSE mount may be useful for distribution, but
prediction tools should not assume POSIX locking/performance from object
storage. Cache loss must affect cost or startup time only, never correctness or
recoverability.

## Weekly ownership

The weekly automation is a control-plane workflow:

1. Cloud Scheduler triggers a small orchestrator (Workflows or a CPU Cloud Run
   service/job).
2. The orchestrator discovers the prerelease, creates a deterministic campaign
   and tasks in Supabase, and submits one GPU job per pending task.
3. GPU jobs execute only prediction tasks and persist results.
4. A later release/evaluation workflow scores and atomically publishes an
   approved batch.

This orchestration must stay outside the model images. While Modal is active,
`weekly_tick` fills the same role through a configured provider-neutral hook.
Moving the cron to Cloud Scheduler changes only the trigger/submitter adapter.

## Cutover checklist

- Build and fixture-test both linux/amd64 OCI images.
- Pin both Artifact Registry references by digest.
- Grant job service accounts least-privilege access to Secret Manager, artifact
  buckets, logs, and the submission API; do not ship key files.
- Configure GPU quota, machine policy, retries, timeout, and concurrency outside
  the task JSON.
- Run identical fixture task IDs on Modal and GCP and compare artifact checksums
  and normalized result manifests.
- Exercise retry/idempotency and a failed upload before enabling the weekly
  Cloud Scheduler trigger.
- Switch the submitter only after GCP results appear correctly in the existing
  Supabase catalog and app.
