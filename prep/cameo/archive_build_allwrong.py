"""HARD-mode ("none of these are correct") quiz builder. REUSES the archive_build per-target pipeline
VERBATIM (collect/select drug-like het, per-model RMSD, crystal-frame seq-Ca alignment, greedy pose
clustering, single-pocket spread, identical protein.pdb/pocket.pdb/xtal_lig.pdb/pose-N.pdb + afprotein/
afpocket writes) but CHANGES ONLY THE ELIGIBILITY to harvest ALL-WRONG single-pocket ensembles:

  keep a target iff:
    - >= 3 scored models loaded + crystal aligns (same as archive_build),
    - min model-RMSD >= 2  (NO pose is correct -> "none of these"),
    - single pocket: cluster-centroid spread < SINGLE_POCKET (8 A).
  (>=2 clusters is NOT required; a single confidently/ambiguously-wrong cluster is fine and common.)

Each item matches the existing CAMEO schema PLUS: source="cameo", single_pocket=true, bucket="all-wrong",
has_correct=false. Every choice has correct=false. Novelty is tagged from sample_novelty.json / train_sim.json
when the id is present (else null). Items -> quiz_items_allwrong.json; data files -> data/<id>/ exactly like
the game-able items so they render identically. We do NOT touch quiz_items.json / app.js / index.html / server.py.

  python3 archive_build_allwrong.py            # process all CAMEO targets, write quiz_items_allwrong.json
  python3 archive_build_allwrong.py <TARGET>   # test one target, print the item (no save)
"""
import json, glob, re, sys
from pathlib import Path
import numpy as np, gemmi, warnings
warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "viewer"))
import process_cameo as P, align_to_crystal as A, build_quiz_items as bq, prep_poses as pp
from archive_build import _lig, _poly, DATA, XTAL          # reuse the exact file writers

ITEMS_F = HERE / "quiz_items_allwrong.json"

# novelty lookups (id -> bool/None), built once
def _load_novelty():
    nov = {}
    for fn in ("sample_novelty.json", "train_sim.json"):
        p = HERE / fn
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        if isinstance(d, dict):
            for tid, meta in d.items():
                if isinstance(meta, dict) and meta.get("novel") is not None and tid not in nov:
                    nov[tid] = bool(meta["novel"])
    return nov
NOVELTY = _load_novelty()


def process_target_allwrong(week, tgt):
    """Mirror of archive_build.process_target, but ALL-WRONG single-pocket eligibility."""
    ligs, _ = P.collect_target(week, tgt)
    het = P.select_target_het(ligs)
    if not het:
        return None
    pm = P.per_model_rmsd(ligs[het])
    # all-wrong: need >=3 scored models AND no correct pose (min RMSD >= 2)
    if len(pm) < 3 or min(pm.values()) < 2:
        return None
    amap = P.af3_ligand_map(week, tgt, het)
    cf = XTAL / f"{tgt}.cif"
    if not cf.exists():
        try: pp.fetch_xtal(tgt, cf)
        except Exception: return None
    try:
        cst = gemmi.read_structure(str(cf)); cst.setup_entities(); cm = cst[0]
    except Exception:
        return None
    cligs = [r for ch in cm for r in ch if r.name.upper() == het.upper()]
    if not cligs:
        return None
    clp = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in cligs[0] if a.element.name != "H"])
    cpoly = A.pocket_chain(cm, clp)
    if cpoly is None:
        return None
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
        poses[mdl] = dict(lr=lr, apoly=apoly, R=R, t=t, pos=ligpos, rmsd=pm[mdl], correct=pm[mdl] < 2,
                          plddt=float(np.mean([a.b_iso for a in lr if a.element.name != "H"])))
    if len(poses) < 3:
        return None
    samples = sorted(poses); cs = [poses[s]["pos"] for s in samples]; n = len(cs[0])
    if any(len(c) != n for c in cs):
        return None
    labels, medoid = bq.cluster(cs); nclu = len(medoid)
    reps_correct = [poses[samples[medoid[c]]]["correct"] for c in range(nclu)]
    cents = [cs[medoid[c]].mean(0) for c in range(nclu)]
    spread = max((float(np.linalg.norm(cents[i] - cents[j])) for i in range(nclu) for j in range(i + 1, nclu)), default=0)
    # ALL-WRONG single-pocket eligibility (no >=2-cluster requirement; all reps must be wrong)
    if any(reps_correct) or spread >= bq.SINGLE_POCKET:
        return None
    # eligible -> write files (crystal frame), identical to the game-able path
    dd = DATA / tgt; dd.mkdir(parents=True, exist_ok=True)
    allpose = np.vstack(cs)
    _poly(cpoly, None, None, dd / "protein.pdb")
    _poly(cpoly, None, None, dd / "pocket.pdb", near=allpose)
    bestcl = min(cligs, key=lambda r: np.linalg.norm(
        np.array([[a.pos.x, a.pos.y, a.pos.z] for a in r if a.element.name != "H"]).mean(0) - allpose.mean(0)))
    _lig([(a.element.name, a.name, np.array([a.pos.x, a.pos.y, a.pos.z])) for a in bestcl if a.element.name != "H"], dd / "xtal_lig.pdb")
    refmdl = min(samples); choices = []
    for s in samples:
        p = poses[s]; R, t = p["R"], p["t"]; idx = samples.index(s)
        _lig([(a.element.name, a.name, R @ np.array([a.pos.x, a.pos.y, a.pos.z]) + t) for a in p["lr"] if a.element.name != "H"], dd / f"pose-{s}.pdb")
        _poly(p["apoly"], R, t, dd / f"afprotein-{s}.pdb")
        _poly(p["apoly"], R, t, dd / f"afpocket-{s}.pdb", near=p["pos"])
        choices.append({"af3_sample": s, "pose_file": f"data/{tgt}/pose-{s}.pdb",
                        "afprotein_file": f"data/{tgt}/afprotein-{s}.pdb", "afpocket_file": f"data/{tgt}/afpocket-{s}.pdb",
                        "rmsd": round(p["rmsd"], 2), "correct": False, "plddt": round(p["plddt"], 2),
                        "cluster": int(labels[idx]), "is_rep": bool(medoid[labels[idx]] == idx)})
    rR, rt = poses[refmdl]["R"], poses[refmdl]["t"]
    _poly(poses[refmdl]["apoly"], rR, rt, dd / "afpocket-union.pdb", near=allpose)
    plddt_pick = max(choices, key=lambda c: c["plddt"])["af3_sample"]
    return {"id": tgt, "ligand": het, "week": week, "protein_file": f"data/{tgt}/protein.pdb",
            "pocket_file": f"data/{tgt}/pocket.pdb", "xtal_lig_file": f"data/{tgt}/xtal_lig.pdb",
            "afprotein_ref": f"data/{tgt}/afprotein-{refmdl}.pdb", "afpocket_union": f"data/{tgt}/afpocket-union.pdb",
            "choices": choices, "n_clusters": nclu, "af3_top_sample": 1, "plddt_pick_sample": plddt_pick,
            "n_correct": 0, "source": "cameo", "single_pocket": True, "bucket": "all-wrong",
            "has_correct": False, "novel": NOVELTY.get(tgt)}


def main():
    items = json.loads(ITEMS_F.read_text())["items"] if ITEMS_F.exists() else []
    have = {it["id"] for it in items}
    bases = sorted(glob.glob(str(P.CAMEO / "*" / "*" / "servers" / "server993")))
    tried = added = drop_multipocket = drop_align = drop_models = drop_hascorrect = 0
    for sd in bases:
        mm = re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/", sd); week, tgt = mm.group(1), mm.group(2)
        if tgt in have: continue
        tried += 1
        try:
            it = process_target_allwrong(week, tgt)
        except Exception:
            it = None
        if it:
            items.append(it); have.add(tgt); added += 1
        if tried % 200 == 0:
            ITEMS_F.write_text(json.dumps({"items": items}, indent=2))
            print(f"... {tried} tried, {added} all-wrong items", flush=True)
    ITEMS_F.write_text(json.dumps({"items": items}, indent=2))
    print(f"DONE: {tried} targets tried, {added} all-wrong single-pocket items; total {len(items)}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sd = glob.glob(str(P.CAMEO / "*" / sys.argv[1] / "servers" / "server993"))
        wk = re.search(r"modeling/([\d.]+)/", sd[0]).group(1) if sd else None
        print(json.dumps(process_target_allwrong(wk, sys.argv[1]), indent=2) if wk else "target not found")
    else:
        main()
