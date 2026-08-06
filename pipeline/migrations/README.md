# Control-plane migrations

`001_control_plane.sql` creates Foldarium's provider-neutral orchestration schema. Apply it with the
Supabase SQL editor or your normal Postgres migration runner. It is transactional and idempotent.

The tables hold campaign, target, prediction-run, artifact, and publication metadata. Large inputs and
outputs remain in Foldarium-controlled object storage; rows refer to immutable object URIs and SHA-256
checksums. The normalized task hash makes run creation retry-safe regardless of whether execution happens
locally, on Modal, or on GCP.

Workers use `claim_prediction_run` for a bounded lease. After running a model, a worker uploads every
verified artifact to Storage and calls `finish_prediction_run`; artifact metadata and the terminal result
are committed in one database transaction. A dead Modal worker can therefore be reclaimed by a GCP worker
after its lease without inventing a new scientific run.

The coordinator must insert a campaign, target, and prediction run before submission. A run's
`task_payload`, `task_sha256`, method/configuration, image, target, and output prefix are cross-checked by
database constraints. `claim_prediction_run` will reject an unknown, completed, exhausted, or actively
leased run.

All control-plane tables have row-level security enabled and no browser policies. Supabase
`service_role` workers can write them; never ship that credential to the frontend. Anonymous and signed-in
clients receive only `published_foldarium_items`, one row per item from a published batch.

Publishing is a privileged, atomic RPC:

```sql
select public.publish_foldarium_batch(
  '2026-w32-v1',
  '{"schema_version":1,"items":[]}'::jsonb,
  '0000000000000000000000000000000000000000000000000000000000000000',
  'https://objects.example/manifests/2026-w32-v1.json'
);
```

The public manifest must already be redacted: do not include correctness, RMSD, answers, scoring data,
or method identity intended for reveal. Repeating the same batch and digest is a no-op. Corrections use a
new batch with `supersedes_batch_id`; the RPC withdraws the old batch and publishes its replacement in one
transaction. A campaign can have only one live published batch.
