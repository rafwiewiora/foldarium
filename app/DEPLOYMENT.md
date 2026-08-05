# Full-app handoff: Vercel + Supabase

## Start here

Use this `app/` directory as the canonical full Foldarium product frontend. The files at the repository
root are a deliberately smaller GitHub Pages demo, not the source of the database-backed product.

The frontend is currently framework-free and has no build step:

- `index.html` + `app.js` are the quiz and Mol* viewer;
- `leaderboard.html` is the leaderboard UI;
- `quiz_items*.json` are the four canonical full item manifests;
- `server.py` documents the existing API behavior with a local SQLite implementation.

All recent viewer work is already mirrored here: stable cameras, calm question transitions, the paged
multi-view grid, full-stage grid layout, and automatic ligand-focused framing.

## What is and is not deployment-ready

The static frontend can be served by Vercel, but this directory is not yet a complete Vercel + Supabase
application. In particular:

- there is no Supabase client, schema, migration, or Vercel function in the repo yet;
- `server.py` writes to `quiz.db`, which is suitable as a local reference but not as a Vercel backend;
- the manifests reference `data/<item-id>/...` and `data_rnp/<item-id>/...` structure files;
- those full pose directories are intentionally not committed and must be regenerated or uploaded to
  object storage.

Until the Supabase adapter exists, running `python3 server.py` remains the reference full-app setup.

## Contracts to preserve while replacing the backend

The current frontend uses these score endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/session` | Store `{ username, answers, client_ts }` and return session totals |
| `GET` | `/api/leaderboard` | Return aggregated per-user rows |
| `GET` | `/api/session/<id>` | Return one session and its answers |

See `server.py` for the exact fields and current aggregation rule. `app.js` falls back to browser-only
`localStorage` scores if the shared backend is unavailable.

The viewer currently loads the four manifests below and then fetches the structure URLs found in each
item:

- `quiz_items.json`
- `quiz_items_allwrong.json`
- `quiz_items_allcorrect.json`
- `quiz_items_rnp.json`

The least disruptive Supabase migration is to keep the item shape consumed by `app.js`, store pose,
pocket, protein, and crystal files in Supabase Storage, and return public or short-lived signed URLs in
the same file fields. A database/API can replace the four monolithic JSON files once it returns the same
normalized item records.

Recommended logical data groups are systems/items, poses, sessions, and answers. Keep model/method and
clustering provenance on pose records; those fields drive the anonymized method pages and clustered
viewer modes.

## Production cautions

- Never expose a Supabase service-role key in the browser; use row-level security and server-side
  functions for privileged writes.
- The current manifests include correctness and RMSD values because the demo is a client-side quiz. If
  answer secrecy matters, return those fields only through a reveal/score endpoint.
- Treat structure files as immutable versioned assets. Store the manifest version on each recorded
  answer so later database growth or reclustering does not change the meaning of old sessions.
- Preserve stable item and pose IDs. The existing leaderboard intentionally aggregates the latest answer
  per user and item so the pose database can grow without invalidating earlier sessions.

## Local reference run

```bash
cd app
# Regenerate or link data/ and data_rnp/ first; see ../prep/README.md.
python3 server.py 8000
# Open http://127.0.0.1:8000
```

`?dev=1` enables browse mode without submitting votes.
