# app/ — the canonical full product app

This is the source to use for the full Foldarium product deployment. It has the same Mol* viewer and
recent camera/grid work as the static demo, but serves **all** buckets (not just novel), retains balanced
Hard-session sampling, and can use a shared leaderboard backend.

For the Vercel + Supabase handoff—including what exists, what still needs replacing, and the API/pose
asset contracts—read [`DEPLOYMENT.md`](DEPLOYMENT.md) first.

## Files
- `index.html`, `app.js`, `leaderboard.html` — the full product viewer. UI changes are mirrored to the
  root demo; this `app.js` also retains balanced Hard-session sampling.
- `server.py` — tiny stdlib server: serves the app + records sessions to a SQLite leaderboard. It is a
  local/reference backend, not the planned Supabase implementation.
- `quiz_items.json`, `quiz_items_allwrong.json`, `quiz_items_allcorrect.json`, `quiz_items_rnp.json` —
  the **canonical** item sets (all buckets). These reference per-item pose/pocket PDBs under `data/`
  (CAMEO) and `data_rnp/` (RnP), which are **not** in the repo — regenerate them with the `prep/` scripts.
- `make_static.py` — bakes the novel-only static demo (what's deployed at the repo root / GitHub Pages).

## Run it
```bash
cd app
# 1. regenerate data/ and data_rnp/ with the prep pipelines (see ../prep/README.md), or symlink existing
# 2. serve:
python3 server.py 8000        # → http://127.0.0.1:8000
```
Scores accumulate in a local SQLite DB (git-ignored). `?dev=1` enables no-vote browse mode.

## Tunables (top of `app.js`)
`CORRECT_THRESH=1.5`, `WRONG_THRESH=3.0`, `HEAVY_MIN=15`, `ALLCORRECT_MAX_FRAC=0.2`,
`HARD_MIX={game-able:0.40, all-wrong:0.45, all-correct:0.15}`.
