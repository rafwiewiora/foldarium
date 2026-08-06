"""Add the physics-cutoff verdict to each viewer system in systems.json.

Rule (v1): mindist_min = min over the 5 poses of the closest heavy-atom distance between the
predicted ligand and AF3's OWN model protein. method_fail (flag as untrustworthy) if mindist_min
< 2.1 Å (poses jam sub-vdW into the protein). Computed from the CAMEO model cifs in CAMEO_SRC,
consistent with experiment/. Adds `mindist_min` + `method_fail` to systems.json.
"""
import json, glob, re, os
from pathlib import Path
import numpy as np, gemmi

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("CAMEO_SRC", "/tmp/cameo_week/month"))
THR = 2.1


def tdir(t):
    g = glob.glob(str(SRC / f"**/{t}/servers/server993"), recursive=True)
    return Path(g[0]).parent.parent if g else None


def lig_chain(td, het):
    for f in glob.glob(str(td / "servers/server993/model-*/scores/ligand_pose.json")):
        try: ligs = json.load(open(f))["results"]["details"]["ligand_pose"]["ligands"]
        except Exception: continue
        for k, v in ligs.items():
            if k.split(".")[-1].upper() == het and v.get("model_ligand_rmsd") and "." in str(v["model_ligand_rmsd"]):
                return str(v["model_ligand_rmsd"]).split(".")[0]
    return None


def pose_mindist(cif, lc):
    try: st = gemmi.read_structure(str(cif)); st.setup_entities()
    except Exception: return None
    m = st[0]; lig = None
    for ch in m:
        if lc and ch.name != lc: continue
        for res in ch:
            if res.name.startswith("LIG") and (lig is None or len(res) > len(lig)): lig = res
    if lig is None:
        for ch in m:
            for res in ch:
                if res.name.startswith("LIG") and (lig is None or len(res) > len(lig)): lig = res
    if lig is None: return None
    lp = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in lig if a.element.name != "H"])
    pp = [[a.pos.x, a.pos.y, a.pos.z] for ch in m for res in ch.get_polymer() for a in res if a.element.name != "H"]
    if not len(lp) or not pp: return None
    pp = np.array(pp)
    return float(np.sqrt(((lp[:, None] - pp[None]) ** 2).sum(-1)).min())


def _xyz_pdb(path, het_only):
    """Heavy-atom coords from a PDB; het_only=True keeps HETATM (the LIG), else ATOM (protein)."""
    tag = "HETATM" if het_only else "ATOM  "
    out = [[float(l[30:38]), float(l[38:46]), float(l[46:54])]
           for l in open(path) if l[:6] == tag and l[76:78].strip() != "H"]
    return np.array(out) if out else None


def pose_mindist_pdb(pose_pdb, prot_pdb):
    """Closest heavy-atom distance between the CORRECTED predicted ligand (pose_file) and AF3's own
    pocket protein (protein_file) — both in the same crystal-aligned frame, so the distance is exact."""
    lp = _xyz_pdb(pose_pdb, True)
    pp = _xyz_pdb(prot_pdb, False)
    if lp is None or pp is None:
        return None
    return float(np.sqrt(((lp[:, None] - pp[None]) ** 2).sum(-1)).min())


def main():
    man = json.loads((HERE / "systems.json").read_text())
    cat = {"caught": 0, "missed": 0, "false_alarm": 0, "ok": 0, "nodata": 0}
    for s in man["systems"]:
        # Use the already-corrected per-pose ligand (pose_file) vs the model pocket protein
        # (protein_file). This uses the right fragment (not the largest LIG = cholesterol).
        mds = []
        for p in s["poses"]:
            pf, prf = HERE / p["pose_file"], (HERE / p["protein_file"]) if p.get("protein_file") else None
            if pf.exists() and prf and prf.exists():
                d = pose_mindist_pdb(pf, prf)
                if d is not None:
                    mds.append(d)
        if not mds:
            s["mindist_min"] = None; s["method_fail"] = None; cat["nodata"] += 1; continue
        mm = round(min(mds), 2)
        s["mindist_min"] = mm
        s["method_fail"] = bool(mm < THR)
        allwrong = not any(p["correct"] for p in s["poses"])
        if allwrong and s["method_fail"]: cat["caught"] += 1
        elif allwrong and not s["method_fail"]: cat["missed"] += 1
        elif not allwrong and s["method_fail"]: cat["false_alarm"] += 1
        else: cat["ok"] += 1
    (HERE / "systems.json").write_text(json.dumps(man, indent=2, allow_nan=False))
    print(f"threshold {THR} Å. categories across {len(man['systems'])} systems:")
    for k, v in cat.items(): print(f"  {k:12s}: {v}")


if __name__ == "__main__":
    main()
