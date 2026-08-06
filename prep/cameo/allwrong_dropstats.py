"""Tally WHY targets are dropped from the all-wrong harvest, reusing the exact same pipeline as
archive_build_allwrong (no file writes). Reason codes:
  no_het / few_models(<3 scored) / has_correct(min RMSD<2) / xtal_fail / no_xtal_lig / pocket_fail /
  align_too_few(<3 aligned models) / atomcount_mismatch / multi_pocket(spread>=8A) / kept(all-wrong single-pocket)
Writes allwrong_dropstats.json.
"""
import json, glob, re, sys
from pathlib import Path
from collections import Counter
import numpy as np, gemmi, warnings
warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "viewer"))
import process_cameo as P, align_to_crystal as A, build_quiz_items as bq, prep_poses as pp
from archive_build import XTAL


def classify(week, tgt):
    ligs, _ = P.collect_target(week, tgt)
    het = P.select_target_het(ligs)
    if not het:
        return "no_het"
    pm = P.per_model_rmsd(ligs[het])
    if len(pm) < 3:
        return "few_models"
    if min(pm.values()) < 2:
        return "has_correct"
    amap = P.af3_ligand_map(week, tgt, het)
    cf = XTAL / f"{tgt}.cif"
    if not cf.exists():
        try: pp.fetch_xtal(tgt, cf)
        except Exception: return "xtal_fail"
    try:
        cst = gemmi.read_structure(str(cf)); cst.setup_entities(); cm = cst[0]
    except Exception:
        return "xtal_fail"
    cligs = [r for ch in cm for r in ch if r.name.upper() == het.upper()]
    if not cligs:
        return "no_xtal_lig"
    clp = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in cligs[0] if a.element.name != "H"])
    cpoly = A.pocket_chain(cm, clp)
    if cpoly is None:
        return "pocket_fail"
    poses = {}
    for mdl in pm:
        cr = amap.get(mdl)
        if not cr: continue
        try:
            st = gemmi.read_structure(str(P.server_dir(week, tgt) / f"model-{mdl}" / f"model-{mdl}.cif")); st.setup_entities(); m = st[0]
        except Exception:
            continue
        lr = None
        for ch in m:
            if ch.name == cr[0]:
                for r in ch:
                    if r.name == cr[1]: lr = r
        if lr is None: continue
        apoly = A.pocket_chain(m, np.array([[a.pos.x, a.pos.y, a.pos.z] for a in lr if a.element.name != "H"]))
        sup = A.seq_super(apoly, cpoly) if apoly else None
        if sup is None: continue
        R, t = sup
        ligpos = np.array([R @ np.array([a.pos.x, a.pos.y, a.pos.z]) + t for a in lr if a.element.name != "H"])
        poses[mdl] = dict(pos=ligpos)
    if len(poses) < 3:
        return "align_too_few"
    samples = sorted(poses); cs = [poses[s]["pos"] for s in samples]; n = len(cs[0])
    if any(len(c) != n for c in cs):
        return "atomcount_mismatch"
    labels, medoid = bq.cluster(cs); nclu = len(medoid)
    cents = [cs[medoid[c]].mean(0) for c in range(nclu)]
    spread = max((float(np.linalg.norm(cents[i] - cents[j])) for i in range(nclu) for j in range(i + 1, nclu)), default=0)
    if spread >= bq.SINGLE_POCKET:
        return "multi_pocket"
    return "kept"


def main():
    bases = sorted(glob.glob(str(P.CAMEO / "*" / "*" / "servers" / "server993")))
    seen, c = set(), Counter()
    for sd in bases:
        mm = re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/", sd); week, tgt = mm.group(1), mm.group(2)
        if tgt in seen: continue
        seen.add(tgt)
        try:
            r = classify(week, tgt)
        except Exception:
            r = "exception"
        c[r] += 1
        if len(seen) % 500 == 0:
            print(f"... {len(seen)} targets, {dict(c)}", flush=True)
    (HERE / "allwrong_dropstats.json").write_text(json.dumps(dict(c), indent=2))
    print("DONE_STATS:", json.dumps(dict(c)))


if __name__ == "__main__":
    main()
