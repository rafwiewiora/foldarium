# app/ — legacy full-data/SQLite prototype

This directory preserves the earlier "full hosted quiz" prototype. It paired a copy of the viewer and
large full-data manifests with a small Python/SQLite server. The intended production architecture is
now the repository-root static frontend on Vercel plus Supabase Postgres and Storage.

**Do not use this directory as the Vercel root and do not deploy `server.py` for the new product.** Start
with [`../DEPLOYMENT.md`](../DEPLOYMENT.md). This prototype remains useful for its manifest examples,
local API semantics, and pipeline history.

## Files
- `index.html`, `app.js`, `leaderboard.html` — a snapshot of the viewer; new frontend work belongs at
  the repository root.
- `server.py` — tiny stdlib server: serves the app + records sessions to a SQLite leaderboard. It is a
  local/reference backend, not the planned Supabase implementation.
- `quiz_items.json`, `quiz_items_allwrong.json`, `quiz_items_allcorrect.json`, `quiz_items_rnp.json` —
  legacy full-size manifest examples covering all buckets. These reference per-item pose/pocket PDBs under `data/`
  (CAMEO) and `data_rnp/` (RnP), which are **not** in the repo — regenerate them with the `prep/` scripts.
- `make_static.py` — historical builder that writes a novel-only static bundle to `app/docs/`.

## Run the legacy prototype
```bash
cd app
# 1. regenerate data/ and data_rnp/ with the prep pipelines (see ../prep/README.md), or symlink existing
# 2. serve:
python3 server.py 8000        # → http://127.0.0.1:8000
```
Scores accumulate in a local SQLite DB (git-ignored). This is for reference/local exploration only;
`?dev=1` enables no-vote browse mode.

## Tunables (top of `app.js`)
`CORRECT_THRESH=1.5`, `WRONG_THRESH=3.0`, `HEAVY_MIN=15`, `ALLCORRECT_MAX_FRAC=0.2`,
`HARD_MIX={game-able:0.40, all-wrong:0.45, all-correct:0.15}`.
