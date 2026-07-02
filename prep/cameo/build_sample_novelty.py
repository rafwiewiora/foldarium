"""Training-novelty of a REPRESENTATIVE RANDOM SAMPLE of the FULL CAMEO drug-like set, split by bucket.

The 228 game-able quiz items are ALL game-able by construction (min<2 & max>=2), so train_sim.json
cannot tell us how trivial / all-wrong targets split by training novelty. This script instead samples
across ALL three loose buckets of the full drug-like set and runs the SAME Foldseek novelty pipeline
on each, so the buckets can be split novel-vs-familiar (mirroring the Runs-n-Poses 13/24/64 table).

We REUSE everything (no science rewritten):
  - process_cameo (collect_target, select_target_het, per_model_rmsd)  : the full drug-like iteration.
  - prep_poses.fetch_xtal                                              : RCSB crystal fetch (id==PDB id).
  - build_train_sim_quiz.process_item                                  : Foldseek->pre-cutoff->shape
        overlap novelty pipeline, cached in viewer/_fshits/, emitting train_shape_overlap + `novel`.

BUCKET (loose, <2 A, exactly the funnel.py definition over per-model median RMSD):
  trivial   : max < 2          (every model is right)
  game-able : min < 2 & max>=2 (a right and a wrong model exist)
  all-wrong : min >= 2         (no model is right)

SAMPLE: stratified across the three buckets with a fixed seed (random.seed(42)). We aim for ~TARGET_N
total, taking up to PER_BUCKET from each bucket (or the whole bucket if smaller). Sampled ids are
recorded in sample_targets.json (audit) before any Foldseek work, so the draw is reproducible.

OUTPUT (resumable, incremental): quiz/sample_novelty.json =
  {target_id: {ligand, min_rmsd, max_rmsd, bucket, train_pdb, train_shape_overlap, novel, ...}}
A target already present is skipped. On a HARD Foldseek failure (rate-limit) we STOP cleanly (the
target is NOT recorded, so it is never mislabeled novel) and the run resumes on the next invocation.
"""
import json, sys, time, random, argparse
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
VIEWER = HERE.parent / "viewer"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(VIEWER))

import process_cameo as P
import prep_poses as pp
import build_train_sim_quiz as bt   # reuse process_item verbatim (Foldseek + shape overlap)

XTAL = bt.XTAL                      # quiz/_xtal_cache  (process_item reads <id>.cif from here)
OUT = HERE / "sample_novelty.json"
SAMPLE_FILE = HERE / "sample_targets.json"
SEED = 42
TARGET_N = 350
PER_BUCKET = 120                    # cap per bucket; smaller buckets contribute whatever they have


def bucket_of(mn, mx):
    if mx < 2.0:
        return "trivial"
    if mn < 2.0:                    # and mx >= 2
        return "game-able"
    return "all-wrong"             # mn >= 2


def build_full_drug_like():
    """Iterate every CAMEO target; for the drug-like target het with >=3 scored models, record
    (week, tgt, het, min_rmsd, max_rmsd, bucket). Returns list of dicts."""
    dates = sorted(p.name for p in P.CAMEO.iterdir() if p.is_dir())
    rows = []
    n_targets = 0
    for date in dates:
        for tdir in sorted((P.CAMEO / date).iterdir()):
            if not tdir.is_dir():
                continue
            tgt = tdir.name
            n_targets += 1
            try:
                ligs, _ = P.collect_target(date, tgt)
                het = P.select_target_het(ligs)
            except Exception:
                het = None
            if not het:
                continue
            pmr = P.per_model_rmsd(ligs[het])
            if len(pmr) < 3:
                continue
            vals = list(pmr.values())
            mn, mx = min(vals), max(vals)
            rows.append({"id": tgt, "week": date, "ligand": het,
                         "min_rmsd": round(mn, 3), "max_rmsd": round(mx, 3),
                         "bucket": bucket_of(mn, mx)})
    return rows, n_targets


def draw_sample(rows):
    """Stratified random sample across buckets with fixed seed. Returns the sampled rows."""
    by_bucket = defaultdict(list)
    for r in rows:
        by_bucket[r["bucket"]].append(r)
    rnd = random.Random(SEED)
    sampled = []
    for b in ("trivial", "game-able", "all-wrong"):
        pool = sorted(by_bucket.get(b, []), key=lambda r: r["id"])  # deterministic order pre-shuffle
        k = min(PER_BUCKET, len(pool))
        sampled.extend(rnd.sample(pool, k))
    # if buckets were small and we are under TARGET_N, top up from the remainder at random
    if len(sampled) < TARGET_N:
        chosen = {r["id"] for r in sampled}
        rest = sorted((r for r in rows if r["id"] not in chosen), key=lambda r: r["id"])
        rnd.shuffle(rest)
        sampled.extend(rest[: TARGET_N - len(sampled)])
    return sampled


def _load_out():
    if OUT.exists():
        try:
            return json.loads(OUT.read_text())
        except Exception:
            pass
    return {}


def _save_out(d):
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, allow_nan=False))
    tmp.replace(OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="process at most N new targets")
    ap.add_argument("--plan-only", action="store_true",
                    help="build the full set + draw the sample, print counts, write sample_targets.json, no Foldseek")
    a = ap.parse_args()

    rows, n_targets = build_full_drug_like()
    bc = defaultdict(int)
    for r in rows:
        bc[r["bucket"]] += 1
    tot = len(rows)
    print(f"scanned {n_targets} CAMEO targets; {tot} drug-like (het + >=3 scored models)", flush=True)
    print(f"FULL-SET buckets (loose <2A):", flush=True)
    for b in ("trivial", "game-able", "all-wrong"):
        pct = round(100 * bc[b] / tot) if tot else 0
        print(f"  {b:<10s} {bc[b]:>5d}  ({pct}%)", flush=True)

    sampled = draw_sample(rows)
    sbc = defaultdict(int)
    for r in sampled:
        sbc[r["bucket"]] += 1
    print(f"\nSAMPLE drawn (seed={SEED}): {len(sampled)} targets", flush=True)
    for b in ("trivial", "game-able", "all-wrong"):
        print(f"  {b:<10s} {sbc[b]:>5d}", flush=True)

    # persist the draw for reproducibility/audit
    SAMPLE_FILE.write_text(json.dumps(
        {"seed": SEED, "full_set_counts": dict(bc), "sample_counts": dict(sbc),
         "targets": sampled}, indent=2))
    print(f"wrote draw -> {SAMPLE_FILE.name}", flush=True)

    if a.plan_only:
        return

    sample_by_id = {r["id"]: r for r in sampled}
    out = _load_out()
    n_new = n_novel = n_train = 0
    for r in sampled:
        sid = r["id"]
        if sid in out:
            continue
        cif = XTAL / f"{sid}.cif"
        # crystal fetch: id == PDB id (verified). cached in _xtal_cache.
        if not (cif.exists() and cif.stat().st_size > 0):
            try:
                pp.fetch_xtal(sid, cif)
            except Exception as e:
                print(f"  {sid}: crystal fetch FAILED ({str(e)[:70]}) -- skipping (not recorded)", flush=True)
                time.sleep(1.0)
                continue
        item = {"id": sid, "ligand": r["ligand"]}
        try:
            res = bt.process_item(item)
        except Exception as e:
            # HARD Foldseek failure (rate-limit). STOP cleanly so we never mislabel as novel.
            print(f"  {sid}: HARD-ERROR {str(e)[:90]} -- stopping (resumable)", flush=True)
            break
        rec = {"ligand": r["ligand"], "week": r["week"],
               "min_rmsd": r["min_rmsd"], "max_rmsd": r["max_rmsd"], "bucket": r["bucket"],
               "train_pdb": res.get("train_pdb"),
               "train_shape_overlap": res.get("train_shape_overlap"),
               "train_max_protein_identity": res.get("train_max_protein_identity"),
               "novel": res.get("novel")}
        if res.get("error"):
            rec["error"] = res["error"]
        out[sid] = rec
        _save_out(out)
        n_new += 1
        if res.get("train_pdb"):
            n_train += 1
            print(f"  {sid} [{r['bucket']}] ({r['ligand']}): train {res['train_pdb']} "
                  f"overlap={res.get('train_shape_overlap')} novel={res['novel']}", flush=True)
        else:
            n_novel += 1
            print(f"  {sid} [{r['bucket']}] ({r['ligand']}): NOVEL "
                  f"({res.get('error','no pre-cutoff match')})", flush=True)
        if a.limit and n_new >= a.limit:
            print(f"  [limit {a.limit} reached]", flush=True)
            break
        time.sleep(0.5)

    # progress summary split by bucket x novelty
    print(f"\nnew this run: {n_new} ({n_train} w/ train ligand, {n_novel} novel/no-match)", flush=True)
    scored = {sid: v for sid, v in out.items() if sid in sample_by_id}
    print(f"total scored: {len(scored)}/{len(sampled)}", flush=True)
    nov = defaultdict(lambda: defaultdict(int))
    for v in scored.values():
        key = "novel" if v.get("novel") else "familiar"
        nov[key][v["bucket"]] += 1
        nov["all"][v["bucket"]] += 1
    for key in ("novel", "familiar", "all"):
        d = nov[key]
        t = sum(d.values())
        if not t:
            continue
        parts = "  ".join(f"{b}={d[b]}({round(100*d[b]/t)}%)" for b in ("trivial", "game-able", "all-wrong"))
        print(f"  {key:<9s} n={t:<4d} {parts}", flush=True)


if __name__ == "__main__":
    main()
