"""QA gate over all systems: verify the DISPLAYED entities are geometrically consistent.

Checks the files the viewer actually loads (xtal_1copy.pdb, train_ligand[_disp].pdb, pose-*.pdb):
  1. xtal_1copy contains exactly ONE ligand residue (the target HET), no lipids/ions  -> declutter.
  2. every CORRECT pose (rmsd<2) overlays the displayed crystal ligand (centroid < CORR_TOL A).
  3. the displayed training ligand sits in the same pocket as the displayed crystal (< POCKET_TOL A).
A wrong pose sitting far from the crystal is EXPECTED (truthful) and is NOT flagged.
"""
import json
from pathlib import Path
import numpy as np
import compute_overlaps as co

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"
CORR_TOL = 6.0      # a correct pose must overlay the displayed crystal within this
POCKET_TOL = 12.0   # training ligand should be in the same pocket (may be offset within it)


def lig_centroid_pdb(path):
    """Centroid of HETATM records labelled LIG (the single displayed ligand)."""
    xs = [[float(l[30:38]), float(l[38:46]), float(l[46:54])]
          for l in open(path) if l[:6] == "HETATM"]
    return np.mean(xs, 0) if xs else None


def lig_resnames(path):
    return {l[17:20].strip() for l in open(path) if l[:6] == "HETATM"}


def main():
    man = json.loads((HERE / "systems.json").read_text())["systems"]
    flagged = []
    for s in man:
        sid = s["id"]
        x1 = HERE / s["xtal_1copy_file"] if s.get("xtal_1copy_file") else None
        flags = []
        if not x1 or not x1.exists():
            flagged.append((sid, "NO xtal_1copy")); continue
        # 1. declutter: only one ligand resname (LIG)
        rn = lig_resnames(x1)
        if rn - {"LIG"}:
            flags.append(f"extra-lig-resnames={rn}")
        xc = lig_centroid_pdb(x1)
        # 2. correct poses overlay the displayed crystal (use the remapped file the viewer renders)
        worst_corr = None
        for p in s["poses"]:
            if not p.get("correct"):
                continue
            pf = HERE / (p.get("pose_disp_file") or p["pose_file"])
            if not pf.exists():
                continue
            pos, _ = co.read_lig_pdb(pf)
            if not len(pos) or xc is None:
                continue
            d = float(np.linalg.norm(pos.mean(0) - xc))
            if worst_corr is None or d > worst_corr[1]:
                worst_corr = (p["sample"], d)
        if worst_corr and worst_corr[1] > CORR_TOL:
            flags.append(f"correct pose s{worst_corr[0]} is {worst_corr[1]:.1f}A from crystal")
        # 3. training ligand in the same pocket (offsite training is intentionally NOT displayed)
        td = None
        if s.get("train_offsite"):
            td = None  # suppressed on purpose; the panel notes the different-pocket match
        elif s.get("train_ligand_file"):
            tf = HERE / s["train_ligand_file"]
            if tf.exists() and xc is not None:
                tpos, _ = co.read_lig_pdb(tf)
                if len(tpos):
                    td = float(np.linalg.norm(tpos.mean(0) - xc))
                    if td > POCKET_TOL:
                        flags.append(f"train ligand {td:.1f}A from crystal (wrong pocket/copy)")
        ncorr = sum(1 for p in s["poses"] if p.get("correct"))
        cc = f"{worst_corr[1]:.1f}" if worst_corr else "-"
        td_s = f"{td:.1f}" if td is not None else "-"
        mark = "  ** " + " | ".join(flags) if flags else ""
        print(f"{sid:6s} corr={ncorr}/5 worstCorr@crystal={cc:>5s}A  train@crystal={td_s:>5s}A{mark}")
        if flags:
            flagged.append((sid, " | ".join(flags)))
    print(f"\n=== {len(flagged)} FLAGGED ===")
    for sid, tag in flagged:
        print(f"  {sid}: {tag}")


if __name__ == "__main__":
    main()
