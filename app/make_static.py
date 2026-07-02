"""Build a self-contained STATIC demo (GitHub-Pages-ready) into ./docs, containing ONLY the NOVEL items
(training-similarity flagged) across BOTH quizzes — CAMEO + Runs-n-Poses — and all three play buckets:
game-able (Easy pick), all-wrong ("none of these"), and all-correct (the Hard positive control).
The same app.js runs; relative paths + the localStorage leaderboard fallback are baked in, so ./docs drops
onto any static host (GitHub Pages, Netlify, S3).

Usage: python3 make_static.py            (novel-only; size-budgeted)

Items must already be tagged with `novel` and `n_heavy`. We bake only items that pass the live quiz filter
(strict 1.5/3 bucket + >=15 heavy), so the demo == what the app shows. A disk BUDGET keeps ./docs small
enough for a static host: if the full novel set exceeds it, we include items round-robin across
(quiz x bucket) until the budget is hit and LOG exactly what was dropped (no silent truncation)."""
import json, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "docs"
CT, WT, HM = 1.5, 3.0, 15        # must match app.js CORRECT_THRESH / WRONG_THRESH / HEAVY_MIN
BUDGET_MB = 200                  # max ./docs data size; subset (with logging) if exceeded


def load(f):
    p = HERE / f
    if not p.exists():
        return []
    x = json.loads(p.read_text())
    return x["items"] if isinstance(x, dict) else x


def bucket_of(it):
    """strict bucket, mirroring app.js norm(): game-able | all-wrong | all-correct | None(=limbo/drop)."""
    rs = [c["rmsd"] for c in it["choices"]]
    if any(r < CT for r in rs) and any(r > WT for r in rs):
        return "game-able"
    if all(r > WT for r in rs):
        return "all-wrong"
    if all(r < CT for r in rs):
        return "all-correct"
    return None


def keep(it):
    """novel + >=15 heavy + a real (non-limbo) strict bucket."""
    if it.get("novel") is not True or it.get("n_heavy", 0) < HM:
        return None
    return bucket_of(it)


def _nres(pdb):
    if not pdb.exists():
        return -1
    res = set()
    for l in pdb.read_text().splitlines():
        if l.startswith(("ATOM", "HETATM")):
            res.add((l[21], l[22:26]))
    return len(res)


def pocket_ok(it, subdir):
    """Drop RnP items whose pocket.pdb IS the whole protein (unaligned upstream — sticks-everywhere render)."""
    d = HERE / subdir / it["id"]
    pnr, rnr = _nres(d / "pocket.pdb"), _nres(d / "protein.pdb")
    if pnr < 0 or rnr < 0:
        return True
    return pnr != rnr


def dir_bytes(subdir, iid):
    d = HERE / subdir / iid
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


# ---- gather eligible novel items per (quiz, bucket, subdir) --------------------------------------
SOURCES = [
    ("cameo", "data", "quiz_items.json"),            # game-able
    ("cameo", "data", "quiz_items_allwrong.json"),   # all-wrong
    ("cameo", "data", "quiz_items_allcorrect.json"), # all-correct (positive control)
    ("rnp",   "data_rnp", "quiz_items_rnp.json"),    # RnP carries all three buckets in one file
]
cats = {}   # (quiz, bucket) -> list[(item, subdir)]
for quiz, subdir, fname in SOURCES:
    for it in load(fname):
        b = keep(it)
        if not b:
            continue
        if quiz == "rnp" and not pocket_ok(it, subdir):
            continue
        cats.setdefault((quiz, b), []).append((it, subdir))

for k in cats:
    cats[k].sort(key=lambda t: t[0]["id"])   # deterministic order

avail = {k: len(v) for k, v in cats.items()}

# ---- round-robin include across categories until the disk BUDGET is hit -------------------------
budget = BUDGET_MB * 1024 * 1024
chosen = {k: [] for k in cats}
used = 0
keys = sorted(cats.keys())
progress = True
while progress:
    progress = False
    for k in keys:
        pool = cats[k]
        i = len(chosen[k])
        if i >= len(pool):
            continue
        it, subdir = pool[i]
        sz = dir_bytes(subdir, it["id"])
        if used + sz > budget and used > 0:
            continue   # skip this one (too big for remaining budget); keep trying smaller others
        chosen[k].append((it, subdir))
        used += sz
        progress = True

# ---- write ./docs -------------------------------------------------------------------------------
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
for f in ("index.html", "app.js", "leaderboard.html"):
    shutil.copy(HERE / f, OUT / f)
shutil.copy(HERE / "DEMO_README.md", OUT / "README.md")   # persist the repo README across rebuilds

def items_for(quiz, bucket):
    return [it for (q, b), lst in chosen.items() if q == quiz and b == bucket for (it, _sd) in lst]

cam_g = items_for("cameo", "game-able")
cam_a = items_for("cameo", "all-wrong")
cam_x = items_for("cameo", "all-correct")
rnp_all = [it for (q, _b), lst in chosen.items() if q == "rnp" for (it, _sd) in lst]

# app.js loads these four files; write only the chosen novel subset into each
(OUT / "quiz_items.json").write_text(json.dumps({"items": cam_g}))
(OUT / "quiz_items_allwrong.json").write_text(json.dumps(cam_a))
(OUT / "quiz_items_allcorrect.json").write_text(json.dumps(cam_x))
(OUT / "quiz_items_rnp.json").write_text(json.dumps(rnp_all))


def copy_data(pairs):
    for it, subdir in pairs:
        src = HERE / subdir / it["id"]
        if src.exists():
            shutil.copytree(src, OUT / subdir / it["id"], dirs_exist_ok=True)


for k in chosen:
    copy_data(chosen[k])
(OUT / ".nojekyll").write_text("")

# ---- report -------------------------------------------------------------------------------------
print(f"built NOVEL static demo -> {OUT}   (~{used/1024/1024:.0f} MB / {BUDGET_MB} MB budget)")
total_inc = total_av = 0
for k in keys:
    inc, av = len(chosen[k]), avail[k]
    total_inc += inc; total_av += av
    drop = av - inc
    tag = f"  (dropped {drop} for budget)" if drop else ""
    print(f"  {k[0]:5s} {k[1]:11s}: {inc}/{av}{tag}")
print(f"  TOTAL: {total_inc}/{total_av} novel items"
      + (f"  — {total_av-total_inc} dropped to fit {BUDGET_MB} MB" if total_av > total_inc else ""))
