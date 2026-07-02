"""Training-set similarity for the 228 quiz items (mirrors viewer/build_training_similarity.py).

For each quiz item we run the SAME Foldseek->pre-cutoff->ligand-shape-overlap pipeline that the
viewer uses for its 47 systems, but iterating quiz/quiz_items.json instead of viewer/systems.json.
We REUSE the viewer module's helpers verbatim (no re-implementation of Foldseek or the shape metric):

  - search_pre_cutoff(seq, exclude_pdb, _cif=...)  : Foldseek STRUCTURE search vs PDB, pre-cutoff
        (released < 2021-09-30) ligand-bearing neighbors, disk-cached in viewer/_fshits/<ID>.json.
  - align_superpose / _first_poly / _poly_ca       : conserved-core (Foldseek alignment) superposition.
  - vol_tanimoto                                   : vdW-volume Tanimoto shape overlap (SuCOS-spirit).
  - load / lig_atoms / lig_arrays / druglike / vdw : crystal IO + ligand selection.

The quiz item `id` IS the RCSB PDB id (verified: fs.release_date('9TLZ') -> 2026-04-01), so it doubles
as Foldseek's exclude_pdb and the release-date key. Crystals are local at quiz/_xtal_cache/<id>.cif.

OUTPUT (resumable, incremental): quiz/train_sim.json = {id: {...}} with, per item:
  train_pdb                   : closest pre-cutoff ligand-bearing PDB, or null (genuinely novel)
  train_identity              : Foldseek seqId of that chosen hit (0-1)
  train_max_protein_identity  : best seqId over all pre-cutoff hits (0-1)
  train_het                   : the training ligand 3-letter code carried into frame
  train_align_rmsd            : pocket-local Ca rmsd of the chosen superposition (A)
  train_shape_overlap         : *** LIGAND-TO-TRAINING SIMILARITY (vdW-volume Tanimoto, 0-1) ***
                                This is the field to split the quiz set by novelty.
  novel                       : bool. True iff train_pdb is null OR train_shape_overlap < NOVEL_THRESH.

NOVELTY: mirrors Runs-n-Poses sim<25 ("novel" = low similarity to anything in training). We use the
ligand shape overlap (train_shape_overlap, 0-1 == 0-100%); NOVEL_THRESH = 0.25 -> overlap < 25%
(or no pre-cutoff ligand-bearing match at all) == novel.
"""
import json, sys, time, argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
VIEWER = HERE.parent / "viewer"
sys.path.insert(0, str(VIEWER))
import numpy as np
import build_training_similarity as bts   # reuse the validated pipeline verbatim
import foldseek_search as fs              # release-date / id verification

XTAL = HERE / "_xtal_cache"
OUT = HERE / "train_sim.json"
TRAINDIR = HERE / "_trainlig"             # per-item carried train ligand/protein PDBs (our own dir)
NOVEL_THRESH = 0.25                       # train_shape_overlap < this (or null hit) == novel


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


def process_item(item):
    """Run the pipeline for one quiz item. Returns the result dict (train_pdb may be None=novel),
    or raises RuntimeError on a hard Foldseek API failure (so we DON'T mislabel it novel)."""
    sid, het = item["id"], item["ligand"]
    cif = XTAL / f"{sid}.cif"
    if not cif.exists():
        return {"train_pdb": None, "error": "no_xtal_cache", "novel": True}

    new_m = bts.load(str(cif))
    qlig = bts.lig_atoms(new_m, het)
    if qlig is None:
        return {"train_pdb": None, "error": f"ligand_{het}_not_in_crystal", "novel": True}
    qpos, qrad = bts.lig_arrays(qlig)
    seq = bts.longest_seq(new_m)

    # Foldseek structure search, pre-cutoff, disk-cached under viewer/_fshits/<sid>.json.
    # Pass the quiz crystal explicitly via _cif so it does NOT look under viewer/systems/.
    hits = bts.search_pre_cutoff(seq, sid, _cif=str(cif))
    if hits is None:
        raise RuntimeError("foldseek API hard-failure (rate-limited)")
    max_ident = max([h["identity"] for h in hits if h["identity"]], default=None)

    qpoly = bts._first_poly(new_m)
    qca = bts._poly_ca(qpoly) if qpoly is not None else None

    best = None
    for h in hits[:25]:
        if qca is None or not h.get("qAln"):
            continue
        try:
            ref_m = bts.load(h["pdb"])
            sup = bts.align_superpose(h, qca, qpos)
        except Exception:
            continue
        if sup is None:
            continue
        tr, rmsd, _nloc = sup
        if rmsd > bts.MAX_LOCAL_RMSD:
            continue
        ref_poly = bts._first_poly(ref_m)
        if ref_poly is None:
            continue
        ligs = sorted({r.name for ch in ref_m for r in ch
                       if not r.is_water() and r.het_flag == "H" and bts.druglike(r.name)})
        if not ligs:
            continue
        for hh in ligs:
            ref_res = bts.lig_atoms(ref_m, hh)
            if ref_res is None:
                continue
            aligned = [(a, tr.apply(a.pos)) for a in ref_res if a.element.name != "H"]
            rpos = np.array([[p.x, p.y, p.z] for _, p in aligned])
            rrad = np.array([bts.vdw(a.element.name) for a, _ in aligned])
            tani = bts.vol_tanimoto(qpos, qrad, rpos, rrad)
            if best is None or tani > best[0]:
                best = (tani, h["pdb"], hh, rmsd, h["identity"], aligned, ref_poly, tr)

    if best is None:
        return {"train_pdb": None,
                "train_max_protein_identity": round(max_ident, 3) if max_ident else None,
                "train_shape_overlap": None, "novel": True}

    tani, pdb, het_ref, rmsd, ident, aligned, ref_poly, tr = best
    # Write carried ligand + aligned protein into OUR own dir (never touch viewer/systems/).
    d = TRAINDIR / sid
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (a, p) in enumerate(aligned, 1):
        lines.append(f"HETATM{i:>5d} {a.name[:4]:<4s} LIG X   1    "
                     f"{bts._xyz(p.x, p.y, p.z)}  1.00  0.00          {a.element.name:>2s}")
    lines.append("END")
    (d / "train_ligand.pdb").write_text("\n".join(lines) + "\n")
    bts.write_protein_pdb(ref_poly, tr, d / "train_protein.pdb")

    return {"train_pdb": pdb,
            "train_identity": round(ident, 3) if ident else None,
            "train_max_protein_identity": round(max_ident, 3) if max_ident else None,
            "train_het": het_ref,
            "train_align_rmsd": round(rmsd, 2),
            "train_shape_overlap": round(tani, 3),
            "novel": tani < NOVEL_THRESH,
            "train_ligand_file": f"_trainlig/{sid}/train_ligand.pdb",
            "train_protein_file": f"_trainlig/{sid}/train_protein.pdb"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="process at most N new items (validation)")
    ap.add_argument("--only", default=None, help="comma-separated item ids to process")
    a = ap.parse_args()
    only = set(x.strip().upper() for x in a.only.split(",")) if a.only else None

    items = json.loads((HERE / "quiz_items.json").read_text())["items"]
    out = _load_out()
    n_new = n_train = n_novel = n_err = 0
    for item in items:
        sid = item["id"]
        if only is not None and sid.upper() not in only:
            continue
        if sid in out and not (only and sid.upper() in only):
            continue   # resume: skip already-processed (unless explicitly re-requested via --only)
        try:
            res = process_item(item)
        except Exception as e:
            print(f"  {sid}: HARD-ERROR {str(e)[:90]}", flush=True)
            n_err += 1
            # Hard API failure: stop the whole run rather than churn (rate-limit won't clear by spamming).
            break
        res["ligand"] = item["ligand"]
        res["week"] = item.get("week")
        out[sid] = res
        _save_out(out)
        n_new += 1
        if res.get("train_pdb"):
            n_train += 1
            print(f"  {sid} ({item['ligand']}): train {res['train_pdb']} ({res.get('train_het')}) "
                  f"id={res.get('train_identity')} overlap={res.get('train_shape_overlap')} "
                  f"novel={res['novel']}", flush=True)
        else:
            n_novel += 1
            print(f"  {sid} ({item['ligand']}): NOVEL ({res.get('error','no pre-cutoff match')})", flush=True)
        if a.limit and n_new >= a.limit:
            print(f"  [limit {a.limit} reached]", flush=True)
            break
        time.sleep(0.5)

    nv = sum(1 for v in out.values() if v.get("novel"))
    print(f"\nnew this run: {n_new} ({n_train} w/ train ligand, {n_novel} no-match, {n_err} errors)", flush=True)
    print(f"total processed: {len(out)}/{len(items)}   novel(overlap<{NOVEL_THRESH} or null): {nv}", flush=True)


if __name__ == "__main__":
    main()
