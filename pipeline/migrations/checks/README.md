# Control-plane checks

Read-only queries used to apply and verify the control plane safely. None of them writes
anything. Run each in the Supabase SQL editor, or through any Postgres client.

The Supabase SQL editor returns only the **last** statement's result set, so each file that
matters is a single statement. Replace the editor's contents rather than appending: it
restores your previous snippet, and re-running that instead is an easy mistake to make.

| File | When | What it proves |
|---|---|---|
| `01_preflight_collisions.sql` | **Before** applying `001_control_plane.sql` to any project that already has data | That none of the migration's five table names, four function names, or its view already exist. Also lists every table/view in `public` with its RLS state. |
| `02_verify_install.sql` | After applying the migration | The five tables (RLS on), the view, and the four functions exist — and that pre-existing tables were left untouched with RLS intact. |
| `03_verify_staged_runs.sql` | After applying generated staging SQL, before running anything | Each run is `pending`, `att 0/N`, `lease=NULL` — claimable but unclaimed — and the run IDs match the generated task payloads. |
| `04_verify_results.sql` | After runs reach a terminal state | Run status, attempt count, released lease, error code; every artifact row with size, digest, and content-addressed `object_uri`; and the sample count and duration from the stored result. |

## Why the preflight matters

`001_control_plane.sql` uses `create table if not exists`. If a project already has a table
named `campaigns`, `targets`, `prediction_runs`, `prediction_artifacts`, or
`publication_batches`, creation is silently skipped — but the statements that follow still
run against the pre-existing table, including `enable row level security` and `revoke all
... from anon`. On a live project that would break anonymous reads immediately.

The migration is wrapped in one transaction, so it protects against errors. It does not
protect against succeeding while adopting a table that was already there. That is what
`01_preflight_collisions.sql` is for.

## Verifying stored bytes

`04_verify_results.sql` confirms that recorded metadata matches the digest the worker
computed locally before upload, and that each object key is its own content digest. It does
**not** re-download the stored objects and re-hash them. Content addressing makes a mismatch
detectable rather than silent, but an explicit read-back check is still worth adding before
production.
