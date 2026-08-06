"""Is pose-cluster MULTIPLICITY (single vs multiple) a reliable indicator that a CAMEO
AF3 5-model ensemble contains a CORRECT pose, and what is the best detector to pick
TRUE-POSITIVE correct ensembles while avoiding CONFIDENTLY-WRONG (single-cluster, all-wrong)
false positives?

Reuses quiz machinery:
  - process_cameo (collect_target, select_target_het, per_model_rmsd, af3_ligand_map, server_dir)
  - prep_poses.af3_residue_by_id / pocket_chain (via the same approach as physics_select.py)
  - build_quiz_items.cluster (greedy ligand-RMSD < CLUSTER_THRESH=2.0)
  - physics_select.py feature approach (min ligand-protein heavy-atom dist, clash, contacts, ligand pLDDT)

Sample: quiz/sample_novelty.json (stratified trivial/game-able/all-wrong, with `novel` flag).
For each target:
  1. load 5 model CIFs, locate the target-het ligand in each (af3_ligand_map),
  2. superpose models 2..5 onto model-1's pocket chain (Ca, gemmi CaP) -> ligand poses in a
     common frame, CLUSTER the (heavy-atom) poses (greedy RMSD<2). record n_clusters, max-cluster size,
  3. per-pose physics (ligand vs its OWN model protein): mindist, clash, contacts, pLDDT(=mean B_iso),
  4. oracle label from CAMEO per-model RMSD: contains_correct = min(rmsd)<2.

Writes quiz/single_vs_multi_results.json.
"""
import json, sys, warnings, math
from pathlib import Path
from collections import defaultdict, Counter
import numpy as np, gemmi
warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "viewer"))
import process_cameo as P
import prep_poses as pp
import build_quiz_items as bq           # cluster(), CLUSTER_THRESH=2.0

VDW = {"C": 1.7, "N": 1.55, "O": 1.52, "F": 1.47, "P": 1.8, "S": 1.8,
       "CL": 1.75, "BR": 1.85, "I": 1.98, "H": 1.2}
vdw = lambda e: VDW.get(e.upper(), 1.7)
CLASH_RATIO = 0.75      # heavy-atom pair clash if dist/(r_i+r_j) < this (~same as physics_select)
CLASH_DIST = 2.1        # absolute min-distance clash threshold (A)


def lig_residue(model, cr):
    if cr is None:
        return None
    for ch in model:
        if ch.name != cr[0]:
            continue
        for r in ch:
            if r.name == cr[1]:
                return r
    return None


def heavy_coords(res):
    return np.array([[a.pos.x, a.pos.y, a.pos.z] for a in res if a.element.name != "H"])


def physics(model, lig):
    """min ligand-protein heavy-atom dist, clash count (vdw-ratio), abs-clash flag, contacts(<4.5A)."""
    lp = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in lig if a.element.name != "H"])
    lr = np.array([vdw(a.element.name) for a in lig if a.element.name != "H"])
    pp_, pr = [], []
    for ch in model:
        poly = ch.get_polymer()
        if len(poly) < 5:
            continue
        for res in poly:
            for a in res:
                if a.element.name == "H":
                    continue
                pp_.append([a.pos.x, a.pos.y, a.pos.z]); pr.append(vdw(a.element.name))
    if not pp_ or not len(lp):
        return None
    pp_ = np.array(pp_); pr = np.array(pr)
    D = np.sqrt(((lp[:, None] - pp_[None]) ** 2).sum(-1))
    ratio = D / (lr[:, None] + pr[None])
    return dict(mindist=float(D.min()),
                clash=int((ratio < CLASH_RATIO).sum()),
                abs_clash=int(D.min() < CLASH_DIST),
                contacts=int((D.min(0) < 4.5).sum()),
                plddt=float(np.mean([a.b_iso for a in lig if a.element.name != "H"])))


def process_target(date, tgt, het_hint):
    """-> dict(record) or (None, reason)."""
    ligs, _ = P.collect_target(date, tgt)
    het = P.select_target_het(ligs)
    if het is None:
        return None, "no_het"
    pm = P.per_model_rmsd(ligs[het])
    if len(pm) < 3:
        return None, "few_models"
    amap = P.af3_ligand_map(date, tgt, het)

    # load each model, get ligand residue + pocket chain
    models, ligres, pocket = {}, {}, {}
    for mdl in sorted(pm):
        cif = P.server_dir(date, tgt) / f"model-{mdl}" / f"model-{mdl}.cif"
        if not cif.exists():
            continue
        try:
            st = gemmi.read_structure(str(cif)); st.setup_entities(); m = st[0]
        except Exception:
            continue
        lig = lig_residue(m, amap.get(mdl))
        if lig is None:
            continue
        pc = pp.pocket_chain(m, lig)        # returns a gemmi.Chain (most Ca within 10A of lig)
        if pc is None or len(pc.get_polymer()) < 5:
            continue
        models[mdl] = m; ligres[mdl] = lig; pocket[mdl] = pc
    if len(models) < 3:
        return None, "load_too_few"

    # superpose every model's pocket chain onto the lowest-index model's, transform ligand
    ref = min(models)
    refpoly = pocket[ref].get_polymer()
    poses = {}      # mdl -> heavy-atom ligand coords in ref frame (only same-atom-count kept)
    align_fail = 0
    for mdl in models:
        if mdl == ref:
            poses[mdl] = heavy_coords(ligres[mdl]); continue
        try:
            sup = gemmi.calculate_superposition(
                refpoly, pocket[mdl].get_polymer(),
                gemmi.PolymerType.PeptideL, gemmi.SupSelect.CaP)
            if not math.isfinite(sup.rmsd):
                align_fail += 1; continue
            T = sup.transform
            lc = heavy_coords(ligres[mdl])
            poses[mdl] = np.array([[p.x, p.y, p.z] for p in
                                   (T.apply(gemmi.Position(*x)) for x in lc)])
        except Exception:
            align_fail += 1; continue

    # clustering needs consistent atom count
    samples = sorted(poses)
    if len(samples) < 3:
        return None, "align_too_few"
    n0 = len(poses[ref])
    samples = [s for s in samples if len(poses[s]) == n0]
    if len(samples) < 3 or n0 == 0:
        return None, "atomcount_mismatch"
    cs = [poses[s] for s in samples]
    labels, medoid = bq.cluster(cs)
    nclu = len(medoid)
    sizes = Counter(labels)
    max_cluster = max(sizes.values())

    # oracle (only over the samples we actually clustered)
    rmsds = {s: pm[s] for s in samples}
    min_rmsd = min(rmsds.values()); max_rmsd = max(rmsds.values())
    contains_correct = int(min_rmsd < 2.0)

    # per-pose physics + pLDDT
    phys = {}
    for s in samples:
        ph = physics(models[s], ligres[s])
        if ph is not None:
            phys[s] = ph
    # ensemble pLDDT = max over models; best-pLDDT model's physics
    if phys:
        plddt_max = max(p["plddt"] for p in phys.values())
        best_s = max(phys, key=lambda s: phys[s]["plddt"])
        # model-1 (or lowest index = ref) physics for the "AF3 top pick" pose
        m1 = ref if ref in phys else best_s
        phys_m1 = phys[m1]; phys_best = phys[best_s]
    else:
        plddt_max = None; phys_m1 = phys_best = None

    return dict(
        id=tgt, week=date, het=het, n_models=len(samples),
        n_clusters=nclu, max_cluster=max_cluster, single_cluster=int(nclu == 1),
        min_rmsd=round(min_rmsd, 3), max_rmsd=round(max_rmsd, 3),
        contains_correct=contains_correct, align_fail=align_fail,
        plddt_max=plddt_max,
        m1_mindist=(phys_m1 or {}).get("mindist"),
        m1_clash=(phys_m1 or {}).get("clash"),
        m1_abs_clash=(phys_m1 or {}).get("abs_clash"),
        m1_contacts=(phys_m1 or {}).get("contacts"),
        best_mindist=(phys_best or {}).get("mindist"),
        best_clash=(phys_best or {}).get("clash"),
        best_abs_clash=(phys_best or {}).get("abs_clash"),
        best_contacts=(phys_best or {}).get("contacts"),
    ), None


# ----------------------------------------------------------------------------- metrics
def auroc(scores, labels):
    """rank-based AUROC; higher score -> more likely positive."""
    s = np.asarray(scores, float); y = np.asarray(labels, int)
    ok = ~np.isnan(s)
    s, y = s[ok], y[ok]
    if len(set(y.tolist())) < 2:
        return None
    order = np.argsort(s)
    ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    # average ranks for ties
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    csum = np.cumsum(cnt); start = csum - cnt
    avg = (start + csum + 1) / 2.0
    ranks = avg[inv]
    npos = y.sum(); nneg = len(y) - npos
    return float((ranks[y == 1].sum() - npos * (npos + 1) / 2) / (npos * nneg))


def prec_rec(flag, labels):
    """flag (1=picked positive), labels (1=actually contains correct). returns prec, recall, n_picked."""
    flag = np.asarray(flag, int); y = np.asarray(labels, int)
    tp = int(((flag == 1) & (y == 1)).sum()); fp = int(((flag == 1) & (y == 0)).sum())
    fn = int(((flag == 0) & (y == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    return dict(precision=prec, recall=rec, n_picked=int((flag == 1).sum()), tp=tp, fp=fp)


def main():
    samp = json.load(open(HERE / "sample_novelty.json"))
    records, skipped = [], defaultdict(int)
    items = list(samp.items())
    print(f"processing {len(items)} sampled targets ...", flush=True)
    for i, (tid, meta) in enumerate(items):
        rec, reason = process_target(meta["week"], tid, meta.get("ligand"))
        if rec is None:
            skipped[reason] += 1; continue
        rec["bucket"] = meta.get("bucket")
        rec["novel"] = meta.get("novel")
        records.append(rec)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(items)} done, {len(records)} kept", flush=True)
    print(f"kept {len(records)} ; skipped {dict(skipped)}", flush=True)

    # ---- consistency check: our recomputed bucket vs sample's bucket ----
    def our_bucket(r):
        if r["max_rmsd"] < 2: return "trivial"
        if r["min_rmsd"] >= 2: return "all-wrong"
        return "game-able"
    mism = sum(1 for r in records if r["bucket"] and our_bucket(r) != r["bucket"])
    print(f"bucket recompute mismatch vs sample label: {mism}/{len(records)}", flush=True)

    # ===================================================================== (2) by bucket
    by_bucket = {}
    for b in ["trivial", "game-able", "all-wrong"]:
        rs = [r for r in records if our_bucket(r) == b]
        if not rs:
            continue
        nclu_dist = Counter(r["n_clusters"] for r in rs)
        n_single = sum(r["single_cluster"] for r in rs)
        by_bucket[b] = dict(
            n=len(rs),
            nclu_distribution=dict(sorted(nclu_dist.items())),
            pct_single=round(100 * n_single / len(rs), 1),
            pct_multiple=round(100 * (len(rs) - n_single) / len(rs), 1),
            mean_n_clusters=round(np.mean([r["n_clusters"] for r in rs]), 2),
        )

    # ===================================================================== (3) detectors
    y = np.array([r["contains_correct"] for r in records])

    # (a) single-cluster as a positive flag (self-consistency)
    single = np.array([r["single_cluster"] for r in records])
    det_single = prec_rec(single, y)
    # false-positive rate = P(single | all-wrong)
    allwrong = [r for r in records if r["contains_correct"] == 0]
    fp_rate_single = round(np.mean([r["single_cluster"] for r in allwrong]), 3) if allwrong else None
    det_single["fp_rate_single_given_allwrong"] = fp_rate_single

    # (b) pLDDT (max over 5 models): AUROC + precision at high-confidence threshold
    plddt = np.array([r["plddt_max"] if r["plddt_max"] is not None else np.nan for r in records])
    auc_plddt = auroc(plddt, y)
    # high-confidence threshold = 70th percentile of pLDDT (top-confidence ensembles)
    thr_plddt = np.nanpercentile(plddt, 70)
    plddt_flag = (plddt >= thr_plddt).astype(int)
    det_plddt = prec_rec(plddt_flag, y); det_plddt["auroc"] = auc_plddt
    det_plddt["threshold(p70)"] = round(float(thr_plddt), 1)

    # (c) physics on model-1 pose: min ligand-protein dist (clash if small) + contacts
    #     higher mindist away from clash, but correct poses are well-packed (more contacts).
    #     Use "physically plausible" = no abs-clash AND mindist in a sane H-bond/contact range.
    mindist = np.array([r["m1_mindist"] if r["m1_mindist"] is not None else np.nan for r in records])
    contacts = np.array([r["m1_contacts"] if r["m1_contacts"] is not None else np.nan for r in records])
    absclash = np.array([r["m1_abs_clash"] if r["m1_abs_clash"] is not None else 0 for r in records])
    # AUROC of contacts (more contacts -> more likely a real, buried pose)
    auc_contacts = auroc(contacts, y)
    auc_mindist = auroc(-np.abs(mindist - 1.6), y)  # closeness to a typical contact distance
    # plausible flag: not clashing AND mindist >= CLASH_DIST (no atomic overlap)
    plausible = ((absclash == 0) & (mindist >= CLASH_DIST)).astype(int)
    det_phys = prec_rec(plausible, y)
    det_phys["auroc_contacts"] = auc_contacts
    det_phys["auroc_mindist_closeness"] = auc_mindist

    # (d) combine single-cluster AND physics-plausible
    combo = ((single == 1) & (plausible == 1)).astype(int)
    det_combo = prec_rec(combo, y)
    allwrong_idx = (y == 0)
    det_combo["fp_rate_given_allwrong"] = round(
        float(combo[allwrong_idx].mean()), 3) if allwrong_idx.any() else None

    # also: single-cluster AND high-pLDDT
    combo_pl = ((single == 1) & (plddt_flag == 1)).astype(int)
    det_combo_pl = prec_rec(combo_pl, y)
    det_combo_pl["fp_rate_given_allwrong"] = round(
        float(combo_pl[allwrong_idx].mean()), 3) if allwrong_idx.any() else None

    # triple: single AND plausible AND high-pLDDT
    triple = ((single == 1) & (plausible == 1) & (plddt_flag == 1)).astype(int)
    det_triple = prec_rec(triple, y)
    det_triple["fp_rate_given_allwrong"] = round(
        float(triple[allwrong_idx].mean()), 3) if allwrong_idx.any() else None

    base_rate = round(float(y.mean()), 3)

    detectors = dict(
        base_rate_contains_correct=base_rate,
        a_single_cluster=det_single,
        b_plddt_max=det_plddt,
        c_physics_plausible=det_phys,
        d_single_AND_plausible=det_combo,
        d_single_AND_highplddt=det_combo_pl,
        d_single_AND_plausible_AND_highplddt=det_triple,
    )

    # ===================================================================== novelty split
    novelty = {}
    for lab, want in [("familiar", False), ("novel", True)]:
        rs_idx = [i for i, r in enumerate(records) if r["novel"] == want]
        if not rs_idx:
            continue
        yy = y[rs_idx]; ss = single[rs_idx]
        pr = prec_rec(ss, yy)
        aw = [records[i] for i in rs_idx if records[i]["contains_correct"] == 0]
        pr["fp_rate_single_given_allwrong"] = (
            round(np.mean([r["single_cluster"] for r in aw]), 3) if aw else None)
        pr["n"] = len(rs_idx); pr["base_rate"] = round(float(yy.mean()), 3)
        pr["auroc_plddt"] = auroc(plddt[rs_idx], yy)
        novelty[lab] = pr

    out = dict(
        n_targets_processed=len(records),
        skipped=dict(skipped),
        bucket_recompute_mismatch=mism,
        by_bucket=by_bucket,
        detectors=detectors,
        novelty_split=novelty,
        records=records,
    )
    (HERE / "single_vs_multi_results.json").write_text(json.dumps(out, indent=2))
    print("\n=== BY BUCKET (single vs multiple) ===")
    for b, d in by_bucket.items():
        print(f"  {b:10s} n={d['n']:3d}  %single={d['pct_single']:5.1f}  "
              f"nclu={d['nclu_distribution']}")
    print("\n=== DETECTORS (label = ensemble contains correct pose, base rate "
          f"{base_rate}) ===")
    for k, v in detectors.items():
        if isinstance(v, dict):
            print(f"  {k}: {v}")
    print("\n=== NOVELTY SPLIT ===")
    for k, v in novelty.items():
        print(f"  {k}: {v}")
    print(f"\nwrote {HERE/'single_vs_multi_results.json'}")


if __name__ == "__main__":
    main()
