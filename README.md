# Foldarium Minimal — co-folding pose-triage quiz

Playable static extract of [rafwiewiora/foldarium](https://github.com/rafwiewiora/foldarium): the novel-only pose quiz (CAMEO + Runs-n-Poses), without prep pipelines, the hosted server, or the training-similarity benchmark viewer.

You're shown a protein **binding pocket** with the ligand removed, plus anonymised predicted poses. Pick the pose that binds, or flag **none of these** (Hard mode).

## Run locally

```bash
python3 -m http.server 8000
```

Then open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) (leaderboard: `/leaderboard.html`).

A static HTTP server is required — Mol* loads structures via `fetch`, so `file://` will not work.

## What's included

| Path | Role |
|------|------|
| `index.html`, `app.js` | Quiz UI + Mol* viewer |
| `leaderboard.html` | localStorage leaderboard |
| `quiz_items*.json` | Novel-only item manifests |
| `data/`, `data_rnp/` | Per-item pocket/pose PDBs |

## Modes

- **Easy** — ensembles that contain a correct pose; pick it.
- **Hard** — pick the correct pose or **"none of these"** (class-balanced draw).
- **CAMEO** / **Runs-n-Poses** — prospective AF3 vs multi-method retrospective poses.

## License

MIT — see `LICENSE`. Upstream © 2026 Rafal Wiewiora.
