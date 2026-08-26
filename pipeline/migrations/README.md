# Control-plane migrations

The numbered files here are the review-friendly pipeline sources. Timestamped, byte-identical mirrors
under `../../supabase/migrations/` are what the linked Supabase CLI discovers. A unit test fails if the
two sets diverge. Each file is transactional; do not apply a later file without its predecessor.

- `001_control_plane.sql`: provider-neutral campaigns, targets, runs, artifacts, and publication.
- `002_weekly_intake.sql`: immutable prerelease snapshots and atomic weekly-plan registration.
- `003_weekly_quiz.sql`: redacted blind rounds, server-enforced voting windows, and Wednesday reveal.
- `004_external_predictions.sql`: private normalized provenance for public CAMEO AF3 comparators.
- `005_curation_decisions.sql`: private row-level selected/rejected/error decisions with metrics and
  provenance; Saturday intake records these atomically with its prerelease snapshot.

The tables hold campaign, target, prediction-run, artifact, and publication metadata. Large inputs and
outputs remain in Foldarium-controlled object storage; rows refer to immutable object URIs and SHA-256
checksums. The normalized task hash makes run creation retry-safe regardless of whether execution happens
locally, on Modal, or on GCP.

Later timestamped-only migrations under `../../supabase/migrations/` extend the
application schema without rewriting these five foundational mirrors. In
particular, `20260815020000_add_private_weekly_evaluations.sql` and its
`20260825235500_upgrade_private_weekly_evaluations_v5.sql` successor are
unapplied review drafts for an append-only, service-role-only post-close
retrospective integrity catalog. They store no result payload and create no
browser view or RPC; applying them is a separate production decision.

`20260826003000_add_weekly_retrospective_publications.sql` is the next,
separately reviewed migration. It creates one immutable publication row per
revealed production round and binds it to the exact v5 evaluation, reveal,
source-snapshot, sanitized-public-artifact, and private pseudonymous-detail
artifact digests.
The table is selectable only by `service_role`; inserts are possible only
through the service-role-only validating RPC, which recomputes the normalized
final vote/session source inside the registration transaction. The companion
missing-publication RPC scans every revealed production round with a v5
evaluation, not only the current campaign. Neither RPC is executable by
`anon` or `authenticated`, and no browser view or policy is created. This
migration is also an unapplied review draft; do not apply it as part of ordinary
application deployment.

The same draft also creates the append-only, service-role-only
`weekly_retrospective_automated_identities` registry. Its deterministic seed
copies the reviewed Claude Opus/Codex UUID-to-name bindings from beta-v4 and
aborts on ambiguous legacy lineage. Future credential rotations must use the
validating service-role registration RPC; direct inserts and all browser access
remain revoked. Publication source recomputation joins and locks this registry
instead of repeatedly treating a historical session name as authorization.
The additive corrections through
`20260826190000_require_retrospective_vote_scope.sql` preserve the one known
legacy anonymous ballot, fix deterministic publication IDs, normalize the
confirmed unclustered beta round to exact-pose scope, and reject later
non-empty votes that lack explicit exact-or-cluster scope.

Workers use `claim_prediction_run` for a bounded lease. After running a model, a worker uploads every
verified artifact to Storage and calls `finish_prediction_run`; artifact metadata and the terminal result
are committed in one database transaction. A dead Modal worker can therefore be reclaimed by a GCP worker
after its lease without inventing a new scientific run.

The coordinator must insert a campaign, target, and prediction run before submission. A run's
`task_payload`, `task_sha256`, method/configuration, image, target, and output prefix are cross-checked by
database constraints. `claim_prediction_run` will reject an unknown, completed, exhausted, or actively
leased run.

All control-plane/source tables have row-level security enabled and no browser write policies. Supabase
`service_role` workers can write them; never ship that credential to the frontend. Anonymous and
signed-in clients receive only redacted published/blind-round views. Authenticated weekly votes go through
`submit_weekly_quiz_vote`, which derives the user from the JWT and rejects submissions outside the voting
window. The vote row contains a choice, not client-supplied correctness.

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

For the weekly blind mode, use `open_weekly_quiz_round` on Saturday and
`reveal_weekly_quiz_round` after the Wednesday close. The public view returns `reveal_manifest = null`
until that second transaction commits; aggregate vote totals are likewise withheld until reveal.
