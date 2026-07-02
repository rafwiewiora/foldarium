# app/ — the full hosted quiz

Same Mol* viewer as the live demo, but serving **all** buckets (not just novel) with a real leaderboard
backend. This is the "requires hosting" version.

## Files
- `index.html`, `app.js`, `leaderboard.html` — the viewer (identical to the demo's).
- `server.py` — tiny Flask/stdlib server: serves the app + records sessions to a SQLite leaderboard.
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
