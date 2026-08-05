# Foldarium Minimal — co-folding pose-triage quiz

## What's included

| Path | Role |
|------|------|
| `index.html`, `app.js` | Quiz UI + Mol* viewer |
| `quiz_items*.json` | Novel-only item manifests |
| `data/`, `data_rnp/` | Per-item pocket/pose PDBs |

## Modes

- **Easy** — ensembles that contain a correct pose; pick it.
- **Hard** — pick the correct pose or **"none of these"** (class-balanced draw).
- **CAMEO** / **Runs-n-Poses** — prospective AF3 vs multi-method retrospective poses.

## Supabase quiz persistence

To enable remote quiz-result persistence:

1. Create a Supabase project.
2. Enable anonymous sign-ins under Auth providers.
3. Apply `supabase/migrations/20260805180000_create_quiz_results.sql`.
4. Put the project URL and browser-safe publishable key in `supabase-config.js`; do not use credentials intended for privileged server-side access.
5. Before production, run live RLS checks with two anonymous accounts: verify own writes succeed, cross-user session updates and answer inserts fail, and answer updates/deletes fail. This is a required pre-production check.
6. Deploy through the existing Vercel Git integration.

Leaving `supabase-config.js` empty keeps the quiz local-only. The anonymous browser identity is lost when site data is cleared.

## Uploading structure files

Upload all PDB files in `data/` and `data_rnp/` to the public `structures` Storage bucket:

```bash
SUPABASE_URL=https://... \
SUPABASE_SERVICE_ROLE_KEY=... \
npm run upload:structures
```

Keep the server credential uncommitted. Rerunning the command without `--overwrite` is safe: existing objects are skipped. Pass `-- --overwrite` to replace existing objects.

## Replaying recorded answers

1. Apply `supabase/migrations/20260805230000_add_viewer_trace.sql` to the Supabase project.
2. Set `REPLAY_PASSWORD`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` in the Vercel project environment.
3. Use a strong, unique replay password. Keep both that password and the server credential out of browser files, including `supabase-config.js`.
4. Open `/replay.html`, enter the password, select a recent session, then select and play one traced answer.

Replay access deliberately uses one shared password. It has no individual replay accounts, per-user authorization, audit trail, or built-in rate limiting; anyone with the shared password can read every replay exposed by the endpoint. Use it only for a small trusted audience and rotate the password if it is disclosed.
