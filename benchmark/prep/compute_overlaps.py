"""Recompute BOTH ligand shape overlaps, copy-aware (handles multi-copy crystals).

  train_shape_overlap  = crystal ligand  vs closest-training ligand   (target memorizability)
  train_pred_overlap   = AF3 prediction  vs closest-training ligand   (did AF3 reproduce training)
  train_memo_gap       = pred_overlap - cryst_overlap                 (caught-memorizing signal)

Multi-copy fix: the crystal has N symmetry copies of the ligand; the training ligand was aligned
near ONE copy, the prediction may sit on ANOTHER. Since the copies are identical, the transform
between any two copies is a Kabsch fit on their (same-order) atoms. We pick the crystal copy nearest
the training as the common pocket, and map each pose into it before scoring. Single-copy → identity.
"""
import json
from pathlib import Path
import numpy as np, gemmi
import build_training_similarity as b

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"


def read_lig_pdb(path):
    pos, rad = [], []
    for ln in open(path):
        if ln[:6] in ("HETATM", "ATOM  "):
            t = ln.split(); el = t[-1]
            if el == "H":
                continue
            pos.append([float(t[6]), float(t[7]), float(t[8])]); rad.append(b.vdw(el))
    return np.array(pos), np.array(rad)


def crystal_copies(model, het):
    """All copies of the ligand (heavy-atom coord arrays, consistent atom order)."""
    out = []
    for ch in model:
        for r in ch:
            if r.name.upper() == het.upper():
                out.append(np.array([[a.pos.x, a.pos.y, a.pos.z] for a in r if a.element.name != "H"]))
    # keep only the modal atom count (drop alt-conf/partial copies that would break Kabsch)
    if not out:
        return []
    from collections import Counter
    n = Counter(len(c) for c in out).most_common(1)[0][0]
    return [c for c in out if len(c) == n]


def kabsch(P, Q):
    """Rotation+translation mapping P onto Q (same N, same atom order)."""
    Pc, Qc = P - P.mean(0), Q - Q.mean(0)
    U, S, Vt = np.linalg.svd(Pc.T @ Qc)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1, 1, d]) @ U.T
    return R, Q.mean(0) - R @ P.mean(0)


def write_single_copy_crystal(model, het, focus, dest):
    """Write ONE crystal copy = the protein chain + ligand copy nearest `focus` (the prediction
    pocket), as PDB. Multi-copy crystals otherwise render all copies spread across the scene."""
    focus = np.array(focus)
    # protein chain nearest focus (by min Cα distance)
    best_ch, best_d = None, 1e9
    for ch in model:
        ca = [a.pos for r in ch.get_polymer() for a in r if a.name == "CA"]
        if not ca:
            continue
        d = min(np.linalg.norm([p.x, p.y, p.z] - focus) for p in ca)
        if d < best_d:
            best_d, best_ch = d, ch
    if best_ch is None:
        return False
    # nearest ligand copy to focus
    lig_res, lig_d = None, 1e9
    for ch in model:
        for r in ch:
            if r.name.upper() == het.upper():
                c = np.mean([[a.pos.x, a.pos.y, a.pos.z] for a in r if a.element.name != "H"], 0)
                if np.linalg.norm(c - focus) < lig_d:
                    lig_d, lig_res = np.linalg.norm(c - focus), r
    lines = []; n = 0
    for r in best_ch.get_polymer():
        for a in r:
            if a.element.name == "H":
                continue
            n += 1
            lines.append(f"ATOM  {n:>5d} {a.name[:4]:<4s} {r.name[:3]:>3s} A{r.seqid.num:>4d}    "
                         f"{a.pos.x:8.3f}{a.pos.y:8.3f}{a.pos.z:8.3f}  1.00  0.00          {a.element.name:>2s}")
    if lig_res is not None:
        for a in lig_res:
            if a.element.name == "H":
                continue
            n += 1
            lines.append(f"HETATM{n:>5d} {a.name[:4]:<4s} LIG X   1    "
                         f"{a.pos.x:8.3f}{a.pos.y:8.3f}{a.pos.z:8.3f}  1.00  0.00          {a.element.name:>2s}")
    lines.append("END")
    dest.write_text("\n".join(lines) + "\n")
    return True


def radii_for(model, het):
    for ch in model:
        for r in ch:
            if r.name.upper() == het.upper():
                return np.array([b.vdw(a.element.name) for a in r if a.element.name != "H"])
    return None


def remap_pdb(src, dst, R, t):
    """Apply rotation R + translation t to every ATOM/HETATM coord in a PDB, fixed-column safe."""
    out = []
    for ln in open(src):
        if ln[:6] in ("ATOM  ", "HETATM"):
            xyz = np.array([float(ln[30:38]), float(ln[38:46]), float(ln[46:54])]) @ R.T + t
            out.append(ln[:30] + f"{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}" + ln[54:])
        else:
            out.append(ln)
    dst.write_text("".join(out))


def main():
    man = json.loads((HERE / "systems.json").read_text())
    n = 0
    for s in man["systems"]:
        m = b.load("systems/" + s["id"] + "/xtal.cif")
        copies = crystal_copies(m, s["ligand"])
        if not copies:
            continue
        cent = [c.mean(0) for c in copies]
        crad = radii_for(m, s["ligand"])
        tf = SYS / s["id"] / "train_ligand.pdb"
        have_train = bool(s.get("train_pdb")) and tf.exists()

        # ---- METRIC (only when a training ligand exists) -------------------------------
        # j = crystal copy nearest the training ligand = the common pocket for overlap scoring.
        j = 0
        if have_train:
            tpos, trad = read_lig_pdb(tf)
            if crad is not None and len(tpos):
                j = int(np.argmin([np.linalg.norm(c - tpos.mean(0)) for c in cent]))
                cryst_ov = b.vol_tanimoto(copies[j], crad, tpos, trad)
                pred_ov = 0.0
                for p in s["poses"]:
                    pf = HERE / p["pose_file"]
                    if not pf.exists():
                        continue
                    ppos, prad = read_lig_pdb(pf)
                    if not len(ppos):
                        continue
                    i = int(np.argmin([np.linalg.norm(c - ppos.mean(0)) for c in cent]))
                    t_in = tpos if i == j else (tpos @ (kr := kabsch(copies[j], copies[i]))[0].T + kr[1])
                    pred_ov = max(pred_ov, b.vol_tanimoto(ppos, prad, t_in, trad))
                s["train_shape_overlap"] = round(cryst_ov, 3)
                s["train_pred_overlap"] = round(pred_ov, 3)
                s["train_memo_gap"] = round(pred_ov - cryst_ov, 3)
            else:
                have_train = False

        # ---- DISPLAY: anchor EVERYTHING on the CRYSTAL, never the prediction ------------
        # Canonical pocket copy `ia` = the crystal copy nearest the closest CORRECT pose (the true
        # pocket AF3 was scored against, <2A from truth by definition); else the training-nearest
        # copy j; else copy 0. This is prediction-independent for wrong predictions, so a wrong pose
        # renders displaced (truthfully) instead of dragging the crystal/training to its bad pocket.
        anchor_cen = None
        corr = [p for p in s["poses"] if p.get("correct")]
        if corr:
            cp = min(corr, key=lambda p: p.get("rmsd") if p.get("rmsd") is not None else 9e9)
            cpf = HERE / cp["pose_file"]
            if cpf.exists():
                cpos, _ = read_lig_pdb(cpf)
                if len(cpos):
                    anchor_cen = cpos.mean(0)
        if anchor_cen is None:
            anchor_cen = cent[j]
        ia = int(np.argmin([np.linalg.norm(c - anchor_cen) for c in cent]))

        # Clean single-copy crystal: protein chain + ONLY the target-HET copy `ia` (lipids/ions excluded).
        if write_single_copy_crystal(m, s["ligand"], cent[ia], SYS / s["id"] / "xtal_1copy.pdb"):
            s["xtal_1copy_file"] = f"systems/{s['id']}/xtal_1copy.pdb"

        # Pharmacophore cloud (built by build_ligand_cloud.py in crystal copy-0 frame) -> canonical copy.
        cloud_src = SYS / s["id"] / "train_cloud.pdb"
        if cloud_src.exists():
            if len(copies) > 1 and ia != 0:
                R, t = kabsch(copies[0], copies[ia])
                remap_pdb(cloud_src, SYS / s["id"] / "train_cloud_disp.pdb", R, t)
                s["cloud_file"] = f"systems/{s['id']}/train_cloud_disp.pdb"
            else:
                s["cloud_file"] = f"systems/{s['id']}/train_cloud.pdb"

        # Multi-copy: remap EVERY pose (and its model protein) from the crystal copy it was aligned to
        # into the canonical copy `ia`, so all poses + the prediction protein share ONE pocket frame with
        # the shown crystal. The crystal copies are NCS-related, so the ligand copy->copy Kabsch is the
        # same rigid transform that maps the (Cα-aligned) model protein onto the canonical copy. Without
        # this, a 4-5Å near-miss on a different copy renders as a ~40Å scatter across symmetry mates.
        for p in s["poses"]:
            p.pop("pose_disp_file", None)
            p.pop("protein_disp_file", None)
            pf = HERE / p["pose_file"]
            if len(copies) > 1 and pf.exists():
                ppos, _ = read_lig_pdb(pf)
                if len(ppos):
                    ip = int(np.argmin([np.linalg.norm(c - ppos.mean(0)) for c in cent]))
                    if ip != ia:
                        R, t = kabsch(copies[ip], copies[ia])   # pose's copy -> canonical copy (NCS op)
                        remap_pdb(pf, SYS / s["id"] / f"pose-{p['sample']}_disp.pdb", R, t)
                        p["pose_disp_file"] = f"systems/{s['id']}/pose-{p['sample']}_disp.pdb"
                        prf = HERE / p["protein_file"] if p.get("protein_file") else None
                        if prf and prf.exists():
                            remap_pdb(prf, SYS / s["id"] / f"protein-{p['sample']}_disp.pdb", R, t)
                            p["protein_disp_file"] = f"systems/{s['id']}/protein-{p['sample']}_disp.pdb"

        # Training overlays the canonical copy. train_ligand.pdb sits at copy j; remap j->ia if different.
        if have_train:
            s["train_ligand_file"] = f"systems/{s['id']}/train_ligand.pdb"
            tpf = SYS / s["id"] / "train_protein.pdb"
            if tpf.exists():
                s["train_protein_file"] = f"systems/{s['id']}/train_protein.pdb"
            if ia != j:
                R, t = kabsch(copies[j], copies[ia])            # copy_j -> canonical copy_ia
                remap_pdb(tf, SYS / s["id"] / "train_ligand_disp.pdb", R, t)
                s["train_ligand_file"] = f"systems/{s['id']}/train_ligand_disp.pdb"
                s["train_focus"] = [round(float(x), 2) for x in cent[ia]]
                if tpf.exists():
                    remap_pdb(tpf, SYS / s["id"] / "train_protein_disp.pdb", R, t)
                    s["train_protein_file"] = f"systems/{s['id']}/train_protein_disp.pdb"
            # If the closest-fold training ligand binds a DIFFERENT pocket (overlap ~0, lands far from
            # this site), don't render a far purple blob — keep the match in the info panel only.
            dl = read_lig_pdb(HERE / s["train_ligand_file"])[0]
            s["train_offsite"] = bool(len(dl) and np.linalg.norm(dl.mean(0) - cent[ia]) > 12.0)
            if s["train_offsite"]:
                s.pop("train_ligand_file", None)
                s.pop("train_protein_file", None)
            n += 1
            print(f"  {s['id']} ({len(copies)}cp, anchor@copy{ia}): "
                  f"cryst {s['train_shape_overlap']:.3f}  pred {s['train_pred_overlap']:.3f}  "
                  f"gap {s['train_memo_gap']:+.3f}")
        else:
            print(f"  {s['id']} ({len(copies)}cp, anchor@copy{ia}): novel (clean crystal only)")

        # Honesty flag: high-copy crystals (often free amino acids) whose correct poses match a
        # crystallographic symmetry mate absent from the deposited AU — display is approximate.
        s["n_copies"] = len(copies)
        worst = 0.0
        for p in s["poses"]:
            if not p.get("correct"):
                continue
            pf = HERE / (p.get("pose_disp_file") or p["pose_file"])
            if pf.exists():
                pp, _ = read_lig_pdb(pf)
                if len(pp):
                    worst = max(worst, float(np.linalg.norm(pp.mean(0) - cent[ia])))
        s["display_approx"] = bool(worst > 6.0)
    (HERE / "systems.json").write_text(json.dumps(man, indent=2, allow_nan=False))
    print(f"\nrecomputed copy-aware overlaps + crystal-anchored display for {n} train systems")


if __name__ == "__main__":
    main()
