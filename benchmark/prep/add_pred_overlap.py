"""Add prediction<->training ligand shape overlap to each system (the 'did AF3 reproduce the
memorized pose' number). Both the training ligand and the AF3 poses are already in the crystal
frame, so this is a pure local recompute — no downloads.

Adds: train_pred_overlap (max over the 5 poses of vdW-volume Tanimoto vs the training ligand).
"""
import json
from pathlib import Path
import numpy as np
from build_training_similarity import vol_tanimoto, vdw

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"


def read_lig_pdb(path):
    pos, rad = [], []
    for ln in open(path):
        if ln[:6] in ("HETATM", "ATOM  "):
            t = ln.split()
            el = t[-1]
            if el == "H":
                continue
            pos.append([float(t[6]), float(t[7]), float(t[8])]); rad.append(vdw(el))
    return np.array(pos), np.array(rad)


def main():
    man = json.loads((HERE / "systems.json").read_text())
    n = 0
    for s in man["systems"]:
        tf = SYS / s["id"] / "train_ligand.pdb"
        if not s.get("train_pdb") or not tf.exists():
            continue
        tpos, trad = read_lig_pdb(tf)
        best = 0.0
        for p in s["poses"]:
            pp = SYS / Path(p["pose_file"]).name.replace(Path(p["pose_file"]).name, "") / Path(p["pose_file"]).name
            pfile = HERE / p["pose_file"]
            if not pfile.exists():
                continue
            ppos, prad = read_lig_pdb(pfile)
            if len(ppos) and len(tpos):
                best = max(best, vol_tanimoto(ppos, prad, tpos, trad))
        s["train_pred_overlap"] = round(best, 3)
        s["train_memo_gap"] = round(best - (s.get("train_shape_overlap") or 0), 3)
        n += 1
        print(f"  {s['id']}: cryst-train {s.get('train_shape_overlap')}  pred-train {s['train_pred_overlap']}  gap {s['train_memo_gap']}")
    (HERE / "systems.json").write_text(json.dumps(man, indent=2, allow_nan=False))
    print(f"\nadded pred-train overlap to {n} systems")


if __name__ == "__main__":
    main()
