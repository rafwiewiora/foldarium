"""Does AF3's pose-SELECTION problem survive at scale, or is the 83% conditional inflated by trivial
self-consistent ensembles? For every ORACLE-CORRECT target in the 3-month CAMEO set, cluster its 5 AF3
poses (pose-pose ligand RMSD after protein Ca superposition) and measure AF3 top-1 (model-1) accuracy
stratified by cluster structure. Reuses process_cameo's corrected extraction."""
import json, glob, re, sys
from pathlib import Path
from statistics import median
import numpy as np, gemmi
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "viewer"))
import process_cameo as P, prep_poses as pp, build_quiz_items as bq

def lig_coords(model, chain, resname):
    res = pp.af3_residue_by_id(model, chain, resname)
    if res is None: return None
    return np.array([[a.pos.x, a.pos.y, a.pos.z] for a in res if a.element.name != "H"])

def first_poly(model):
    polys = [ch.get_polymer() for ch in model if len(ch.get_polymer()) > 5]
    return max(polys, key=len) if polys else None

def main():
    bases = sorted(glob.glob(str(P.CAMEO / "*" / "*" / "servers" / "server993")))
    rows = []; n = 0
    for sd in bases:
        m = re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/", sd)
        date, tgt = m.group(1), m.group(2)
        try:
            ligs, nm = P.collect_target(date, tgt)
            het = P.select_target_het(ligs)
            if not het: continue
            pm = P.per_model_rmsd(ligs[het])
            if len(pm) < 3 or min(pm.values()) >= 2: continue      # oracle-correct only
            amap = P.af3_ligand_map(date, tgt, het)
            models = {}; ligc = {}
            for mdl in pm:
                cif = P.server_dir(date, tgt) / f"model-{mdl}" / f"model-{mdl}.cif"
                try:
                    st = gemmi.read_structure(str(cif)); st.setup_entities(); mo = st[0]
                except Exception: continue
                cr = amap.get(mdl)
                if not cr: continue
                lc = lig_coords(mo, cr[0], cr[1])
                if lc is None: continue
                models[mdl] = mo; ligc[mdl] = lc
            if len(ligc) < 3: continue
            ref = min(ligc); refpoly = first_poly(models[ref])
            if refpoly is None: continue
            coords = {}
            ok = True
            for mdl in ligc:
                if mdl == ref:
                    coords[mdl] = ligc[mdl]; continue
                mp = first_poly(models[mdl])
                if mp is None: ok = False; break
                T = gemmi.calculate_superposition(refpoly, mp, gemmi.PolymerType.PeptideL, gemmi.SupSelect.CaP).transform
                coords[mdl] = np.array([[q.x, q.y, q.z] for q in
                                        (T.apply(gemmi.Position(*xyz)) for xyz in ligc[mdl])])
            if not ok: continue
            keys = sorted(coords); cs = [coords[k] for k in keys]
            if any(len(c) != len(cs[0]) for c in cs): continue
            labels, medoid = bq.cluster(cs)
            nclu = len(medoid)
            reps_correct = [pm[keys[medoid[c]]] < 2 for c in range(nclu)]
            ncorr_reps = sum(reps_correct)
            ncorr_poses = sum(1 for k in keys if pm[k] < 2)
            corr_idx = [i for i, k in enumerate(keys) if pm[k] < 2]
            maxcc = max((float(np.sqrt(((cs[i]-cs[j])**2).sum(1).mean()))
                         for i in corr_idx for j in corr_idx if i < j), default=0.0)
            top1 = pm.get(1, pm[min(pm)]) < 2
            rows.append((nclu, ncorr_reps, len(reps_correct), ncorr_poses, top1, round(maxcc, 1))); n += 1
            if n % 50 == 0: print("...", n, "processed", flush=True)
        except Exception:
            continue
    def acc(sub): return f"{sum(r[4] for r in sub)}/{len(sub)} = {round(100*sum(r[4] for r in sub)/len(sub))}%" if sub else "n/a"
    multi = [r for r in rows if r[0] >= 2]
    all_correct = [r for r in multi if r[1] == r[2]]                 # every cluster rep correct (wobble)
    mixed = [r for r in multi if 0 < r[1] < r[2]]                    # >=1 right + >=1 wrong rep = AMBIGUOUS
    no_rep = [r for r in multi if r[1] == 0]                         # no rep correct but oracle (correct pose is non-medoid)
    print(f"\n=== oracle-correct: {len(rows)} | single-cluster {len([r for r in rows if r[0]==1])} | multi-cluster {len(multi)} ===")
    print(f"MULTI-CLUSTER decomposition ({len(multi)}):")
    print(f"  all clusters CORRECT (wobble, your case): {len(all_correct)}   AF3 top-1 {acc(all_correct)}")
    print(f"  MIXED right+wrong (AMBIGUOUS, the quiz):   {len(mixed)}   AF3 top-1 {acc(mixed)}")
    print(f"  no medoid correct, correct pose is non-medoid: {len(no_rep)}   AF3 top-1 {acc(no_rep)}")
    import statistics as st
    mcc = [r[5] for r in all_correct]
    if mcc:
        print(f"  -> max pose-pose dist among CORRECT poses in the wobble set: "
              f"median {st.median(mcc):.1f}A, max {max(mcc):.1f}A (all <2A from crystal, but split by the 2A cluster cut)")

if __name__ == "__main__":
    main()
