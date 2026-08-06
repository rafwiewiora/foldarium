"""Incremental archive builder: scan the extracted CAMEO dir, process targets NOT already handled, and
APPEND eligible quiz items to quiz_items.json (existing items untouched). One consolidated pass per target
(target selection -> crystal-frame alignment -> clustering -> eligibility -> AF3 proteins + pLDDT), reusing
the shared pipeline functions so the quiz and viewer stay tied. Each target is processed exactly once
(tracked in archive_processed.json) so re-runs after deleting old CAMEO data are safe.

  python3 archive_build.py            # process all new targets in process_cameo.CAMEO
  python3 archive_build.py <TARGET>   # test: process one target, print the item (no save)
"""
import json, glob, re, sys
from pathlib import Path
from statistics import median
import numpy as np, gemmi, warnings
warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "viewer"))
import process_cameo as P, align_to_crystal as A, build_quiz_items as bq, prep_poses as pp
DATA = HERE / "data"; XTAL = HERE / "_xtal_cache"; XTAL.mkdir(exist_ok=True)
ITEMS_F = HERE / "quiz_items.json"; PROC_F = HERE / "archive_processed.json"


def _lig(atoms, dest):                       # atoms: list of (elem, name, xyz)
    out = [f"HETATM{i+1:>5d} {nm[:4]:<4s} LIG X   1    {p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {el:>2s}"
           for i, (el, nm, p) in enumerate(atoms)]
    dest.write_text("\n".join(out) + "\nEND\n")


def _poly(poly, R, t, dest, near=None):
    out = []; i = 0
    for r in poly:
        ats = [(a, (R @ np.array([a.pos.x, a.pos.y, a.pos.z]) + t) if R is not None
                else np.array([a.pos.x, a.pos.y, a.pos.z])) for a in r if a.element.name != "H"]
        if near is not None:
            xyz = np.array([p for _, p in ats])
            if not len(xyz) or np.min(np.linalg.norm(xyz[:, None] - near[None], axis=2)) >= bq.POCKET_R:
                continue
        for a, p in ats:
            i += 1
            out.append(f"ATOM  {i:>5d} {a.name[:4]:<4s} {r.name[:3]:>3s} A{r.seqid.num:>4d}    "
                       f"{p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {a.element.name:>2s}")
    dest.write_text("\n".join(out) + "\nEND\n")


def process_target(week, tgt):
    ligs, _ = P.collect_target(week, tgt)
    het = P.select_target_het(ligs)
    if not het:
        return None
    pm = P.per_model_rmsd(ligs[het])
    if len(pm) < 3 or min(pm.values()) >= 2 or max(pm.values()) < 2:    # oracle + a clearly-wrong pose
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
    if not (nclu >= 2 and any(reps_correct) and not all(reps_correct) and spread < bq.SINGLE_POCKET):
        return None
    # eligible -> write files (crystal frame)
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
                        "rmsd": round(p["rmsd"], 2), "correct": bool(p["correct"]), "plddt": round(p["plddt"], 2),
                        "cluster": int(labels[idx]), "is_rep": bool(medoid[labels[idx]] == idx)})
    rR, rt = poses[refmdl]["R"], poses[refmdl]["t"]
    _poly(poses[refmdl]["apoly"], rR, rt, dd / "afpocket-union.pdb", near=allpose)
    plddt_pick = max(choices, key=lambda c: c["plddt"])["af3_sample"]
    return {"id": tgt, "ligand": het, "week": week, "protein_file": f"data/{tgt}/protein.pdb",
            "pocket_file": f"data/{tgt}/pocket.pdb", "xtal_lig_file": f"data/{tgt}/xtal_lig.pdb",
            "afprotein_ref": f"data/{tgt}/afprotein-{refmdl}.pdb", "afpocket_union": f"data/{tgt}/afpocket-union.pdb",
            "choices": choices, "n_clusters": nclu, "af3_top_sample": 1, "plddt_pick_sample": plddt_pick,
            "n_correct": sum(c["correct"] for c in choices), "source": "cameo-af3"}


def main():
    items = json.loads(ITEMS_F.read_text())["items"] if ITEMS_F.exists() else []
    proc = set(json.loads(PROC_F.read_text())) if PROC_F.exists() else set()
    proc |= {it["id"] for it in items}
    bases = sorted(glob.glob(str(P.CAMEO / "*" / "*" / "servers" / "server993")))
    tried = added = 0
    for sd in bases:
        mm = re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/", sd); week, tgt = mm.group(1), mm.group(2)
        if tgt in proc: continue
        proc.add(tgt); tried += 1
        try:
            it = process_target(week, tgt)
        except Exception:
            it = None
        if it: items.append(it); added += 1
        if tried % 40 == 0:
            ITEMS_F.write_text(json.dumps({"items": items}, indent=2))
            PROC_F.write_text(json.dumps(sorted(proc)))
            print(f"... {tried} new tried, {added} added, archive {len(items)}", flush=True)
    ITEMS_F.write_text(json.dumps({"items": items}, indent=2))
    PROC_F.write_text(json.dumps(sorted(proc)))
    print(f"DONE: {tried} new targets tried, {added} added; archive now {len(items)} items")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        import glob as g
        sd = g.glob(str(P.CAMEO / "*" / sys.argv[1] / "servers" / "server993"))
        wk = re.search(r"modeling/([\d.]+)/", sd[0]).group(1) if sd else None
        print(json.dumps(process_target(wk, sys.argv[1]), indent=2)[:1500] if wk else "target not found")
    else:
        main()
