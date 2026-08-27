# Pipeline control-plane migrations

The numbered files are the readable source for Foldarium's provider-neutral
prediction control plane. Apply them in numeric order to a new database:

1. `001_control_plane.sql`
2. `002_weekly_intake.sql`
3. `003_weekly_quiz.sql`
4. `004_external_predictions.sql`
5. `005_curation_decisions.sql`

Timestamped mirrors under `../../supabase/migrations/` are consumed by the
Supabase CLI. Tests verify that mirrored foundational migrations remain
byte-identical.

The schema stores immutable campaign, target, task, run, artifact, and
publication metadata. Large files stay in object storage and are referenced by
content digest. Workers claim bounded leases; another execution backend can
reclaim an expired lease without changing the scientific run identity.

All source/control-plane tables use row-level security and expose no browser
write policy. Server workers require a service-role credential. Browser clients
must receive only redacted public views and a publishable key.

Later timestamped migrations add weekly recording, evaluation, retrospective,
Selector, benchmark, and post-reveal voting contracts. For a clean installation
apply all root migrations in timestamp order and test with independent users
before enabling writes.
