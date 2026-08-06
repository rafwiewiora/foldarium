"""Build quiz_items.json from labeled AF3 ensembles (the corrected viewer systems for now; the CAMEO
3-month pull feeds many more via the same schema).

Pipeline per ensemble:
  1. keep drug-like, oracle-correct ones with >=1 wrong pose,
  2. CLUSTER the poses by ligand-RMSD (< CLUSTER_THRESH = near-duplicates merged); each cluster gets a
     MEDOID representative. The quiz shows one representative per cluster by default; the UI can
     "uncluster" to expose every member (in case the medoid looks slightly off-centre),
  3. require >=2 clusters (>=2 genuinely distinct answers) AND >=1 correct + >=1 wrong cluster,
  4. require SINGLE POCKET: all cluster representatives within SINGLE_POCKET A (centroids) — multi-pocket
     scatter is parked in HARD_multipocket.md as a separate, harder problem for later.

Self-contained: copies into quiz/data/<id>/ the apo crystal protein (ligand hidden in UI), each pose
ligand, per-pose pocket residues (protein within POCKET_R A of that pose) + a union pocket — so the UI
can show binding-site sticks around whichever pose(s) are on screen.
"""
import json, shutil
from pathlib import Path
import numpy as np
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "viewer"))
import compute_overlaps as co

HERE = Path(__file__).resolve().parent
VIEWER = HERE.parent / "viewer"
DATA = HERE / "data"
CLUSTER_THRESH = 2.0    # poses within this ligand-RMSD are the "same" answer -> one cluster
SINGLE_POCKET = 8.0     # max pairwise representative-centroid distance to count as ONE pocket
POCKET_R = 5.0          # protein residues within this of a pose = its binding-site sticks


def rmsd(a, b):
    return float(np.sqrt(((a - b) ** 2).sum(1).mean()))


def cluster(cs):
    """Greedy single-pass clustering by ligand-RMSD. Returns labels[] and medoid index per cluster."""
    labels = [-1] * len(cs)
    cid = 0
    for i in range(len(cs)):
        if labels[i] >= 0:
            continue
        labels[i] = cid
        for j in range(i + 1, len(cs)):
            if labels[j] < 0 and rmsd(cs[i], cs[j]) < CLUSTER_THRESH:
                labels[j] = cid
        cid += 1
    medoid = {}
    for c in range(cid):
        members = [i for i in range(len(cs)) if labels[i] == c]
        medoid[c] = min(members, key=lambda i: sum(rmsd(cs[i], cs[j]) for j in members))
    return labels, medoid


def read_protein_atoms(path):
    out = []
    for ln in open(path):
        if ln[:4] == "ATOM":
            xyz = np.array([float(ln[30:38]), float(ln[38:46]), float(ln[46:54])])
            out.append((ln[21] + ln[22:27], ln, xyz))   # chain+resSeq+iCode key
    return out


def write_pocket(prot, lig_pos, dest):
    res = {}
    for key, ln, xyz in prot:
        res.setdefault(key, []).append((ln, xyz))
    near = [key for key, al in res.items()
            if np.min(np.linalg.norm(np.array([x for _, x in al])[:, None] - lig_pos[None], axis=2)) < POCKET_R]
    dest.write_text("".join(ln for key in near for ln, _ in res[key]) + "END\n")


def classify(s):
    if s.get("display_approx"):
        return "skip", None, None, None
    rmsds = [p["rmsd"] for p in s["poses"] if p.get("rmsd") is not None]
    if len(rmsds) < 3 or min(rmsds) >= 2 or max(rmsds) < 2:
        return "skip", None, None, None
    cs = [co.read_lig_pdb(str(VIEWER / p["pose_file"]))[0] for p in s["poses"]]
    n = len(cs[0]) if cs and len(cs[0]) else 0
    if not n or any(len(c) != n for c in cs):
        return "skip", None, None, None
    labels, medoid = cluster(cs)
    nclu = len(medoid)
    if nclu < 2:
        return "trivial", None, None, None            # all poses are basically one answer
    rep_cent = [cs[medoid[c]].mean(0) for c in range(nclu)]
    spread = max(float(np.linalg.norm(rep_cent[i] - rep_cent[j]))
                 for i in range(nclu) for j in range(i + 1, nclu))
    reps_correct = [s["poses"][medoid[c]]["rmsd"] < 2 for c in range(nclu)]
    if not (any(reps_correct) and not all(reps_correct)):
        return "trivial", None, None, None            # need >=1 correct AND >=1 wrong cluster
    if spread >= SINGLE_POCKET:
        return "multipocket", round(spread, 1), None, None
    return "eligible", round(spread, 1), (cs, labels, medoid), nclu


def main():
    man = json.loads((VIEWER / "systems.json").read_text())["systems"]
    items, multipocket = [], []
    for s in man:
        kind, info, packed, nclu = classify(s)
        if kind == "multipocket":
            multipocket.append((s["id"], s["ligand"], info)); continue
        if kind != "eligible":
            continue
        sid = s["id"]; cs, labels, medoid = packed
        d = DATA / sid; d.mkdir(parents=True, exist_ok=True)
        shutil.copy(VIEWER / s["xtal_1copy_file"], d / "protein.pdb")
        prot = read_protein_atoms(d / "protein.pdb")
        write_pocket(prot, np.vstack(cs), d / "pocket-union.pdb")
        rep_of = {labels[medoid[c]]: medoid[c] for c in medoid}   # cluster -> medoid pose index
        choices = []
        for i, (p, lig) in enumerate(zip(s["poses"], cs)):
            shutil.copy(VIEWER / (p.get("pose_disp_file") or p["pose_file"]), d / f"pose-{p['sample']}.pdb")
            write_pocket(prot, lig, d / f"pocket-{p['sample']}.pdb")
            choices.append({"af3_sample": p["sample"],
                            "pose_file": f"data/{sid}/pose-{p['sample']}.pdb",
                            "pocket_file": f"data/{sid}/pocket-{p['sample']}.pdb",
                            "rmsd": p["rmsd"], "correct": bool(p["rmsd"] < 2),
                            "cluster": labels[i], "is_rep": medoid[labels[i]] == i})
        items.append({
            "id": sid, "ligand": s["ligand"], "protein_file": f"data/{sid}/protein.pdb",
            "pocket_union_file": f"data/{sid}/pocket-union.pdb",
            "choices": choices, "n_clusters": nclu, "af3_top_sample": 1,
            "n_correct": sum(c["correct"] for c in choices),
            "source": "cameo-af3", "week": s.get("week"),
        })
    (HERE / "quiz_items.json").write_text(json.dumps({"items": items}, indent=2))
    note = ("# Multi-pocket ensembles — parked as 'hard' (poses scattered across different sites)\n\n"
            "Oracle-correct, multi-cluster ensembles EXCLUDED because the cluster representatives sit in "
            f"different pockets (max pairwise centroid distance >= {SINGLE_POCKET} A). Discriminating "
            "ORIENTATIONS within one pocket is the current quiz; cross-pocket scatter is a separate, "
            "easier-to-spot problem to revisit later.\n\n| id | ligand | centroid spread (A) |\n"
            "|----|--------|---------------------|\n"
            + "".join(f"| {i} | {l} | {sp} |\n" for i, l, sp in sorted(multipocket, key=lambda x: -x[2])))
    (HERE / "HARD_multipocket.md").write_text(note)
    print(f"wrote {len(items)} SINGLE-POCKET quiz items; parked {len(multipocket)} multi-pocket")
    for it in items:
        reps = [c for c in it["choices"] if c["is_rep"]]
        af3c = next(c["correct"] for c in it["choices"] if c["af3_sample"] == it["af3_top_sample"])
        print(f"  {it['id']} ({it['ligand']}): {it['n_clusters']} clusters / {len(it['choices'])} poses, "
              f"{sum(c['correct'] for c in reps)} correct clusters, AF3 top-pick {'RIGHT' if af3c else 'WRONG'}")
    print("parked multi-pocket:", [m[0] for m in multipocket])


if __name__ == "__main__":
    main()
