"""Surprising-win analysis: AF3 CORRECT despite LOW training similarity (after Foldseek search).

A "surprise" = correct prediction + no close training example. Search can only DESTROY surprises
(one close hit kills it), so survivors under the stronger Foldseek search are the defensible ones.

Definitions (per system, using the final Foldseek-based systems.json):
  correct        = oracle: at least one of the 5 poses < 2 Å (has a correct pose)
  ligand-novel   = train_pdb is None (no pre-cutoff drug-like ligand found at all)
                   OR train_shape_overlap < LIG_THR (closest training ligand doesn't fill the pocket)
  protein-known  = train_max_protein_identity >= PROT_HI   (the fold IS in training)
  protein-novel  = train_max_protein_identity <  PROT_LO  (or None)

Flavours of surprise:
  F1  novel LIGAND on a KNOWN protein  (correct + ligand-novel + protein-known)  -- drug-discovery-relevant
  F2  novel PROTEIN and ligand         (correct + ligand-novel + protein-novel)  -- rare/profound

Reports the surprise set stratified by flavour, plus the BEFORE->AFTER attrition vs sequence search
(A2A targets were false-novels under seq-search; Foldseek resolves them -> those false surprises die).
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIG_THR = 0.20
PROT_HI = 0.60
PROT_LO = 0.40

# before (sequence search) reference counts, from the pre-Foldseek run:
BEFORE = {"with_ligand": 18, "novel": 28, "note": "A2A (7IN*/7IO*/7IP*) were mostly false-novels"}


def correct(s):
    return any(p.get("correct") for p in s["poses"])


def main():
    s = json.loads((HERE / "systems.json").read_text())["systems"]
    have = [x for x in s if x.get("train_pdb")]
    novel = [x for x in s if "train_pdb" in x and not x.get("train_pdb")]
    a2a = [x for x in s if x["id"][:3] in ("7IN", "7IO", "7IP")]
    a2a_resolved = [x for x in a2a if x.get("train_pdb")]

    print(f"=== AFTER Foldseek: {len(have)} with training ligand, {len(novel)} novel, of {len(s)} ===")
    print(f"    BEFORE (seq search): {BEFORE['with_ligand']} with ligand, {BEFORE['novel']} novel  ({BEFORE['note']})")
    print(f"    A2A targets: {len(a2a)} total, {len(a2a_resolved)} now resolved (were false-novels) "
          f"-> {len(a2a)-len(a2a_resolved)} still novel\n")

    def ligand_novel(x):
        if not x.get("train_pdb"):
            return True
        ov = x.get("train_shape_overlap")
        return ov is not None and ov < LIG_THR
    def prot_id(x):
        return x.get("train_max_protein_identity")

    surprises = [x for x in s if correct(x) and ligand_novel(x)]
    f1 = [x for x in surprises if (prot_id(x) is not None and prot_id(x) >= PROT_HI)]
    f2 = [x for x in surprises if (prot_id(x) is None or prot_id(x) < PROT_LO)]
    mid = [x for x in surprises if x not in f1 and x not in f2]

    def row(x):
        pid = prot_id(x); pid = f"{round(100*pid)}%" if pid is not None else "—"
        ov = x.get("train_shape_overlap")
        ov = ov if ov is not None else "noLig"
        return f"  {x['id']} ({x['ligand']}): protein-id {pid}, closest-train {x.get('train_pdb') or 'NONE'}, ligand-overlap {ov}"

    print(f"SURPRISES (correct + ligand-novel): {len(surprises)}")
    print(f"\n F1  novel LIGAND on KNOWN protein (id>={int(100*PROT_HI)}%): {len(f1)}")
    for x in sorted(f1, key=lambda x: (x.get('train_shape_overlap') or 0)): print(row(x))
    print(f"\n F2  novel PROTEIN + ligand (id<{int(100*PROT_LO)}% or none): {len(f2)}")
    for x in sorted(f2, key=lambda x: -(prot_id(x) or 0)): print(row(x))
    if mid:
        print(f"\n (mid protein identity {int(100*PROT_LO)}-{int(100*PROT_HI)}%): {len(mid)}")
        for x in mid: print(row(x))

    # which A2A are in the surprise set (i.e. fragments still novel-shaped even with A2A protein found)?
    a2a_surp = [x for x in surprises if x in a2a]
    print(f"\n A2A in surprise set: {len(a2a_surp)} (novel-shaped fragments on the now-found A2A protein)")
    print("\nNote: survivors here have passed Foldseek's exhaustive structural search; push further by")
    print("checking ligand shape vs ALL Foldseek neighbours' ligands, not just the closest protein's.")


if __name__ == "__main__":
    main()
