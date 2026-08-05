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
4. Put the project URL and publishable key—not the service-role key—in `supabase-config.js`.
5. Deploy through the existing Vercel Git integration.

Leaving `supabase-config.js` empty keeps the quiz local-only. The anonymous browser identity is lost when site data is cleared.
