"""Pharmacophore CLOUD: for each system, gather EVERY drug-like ligand bound in the pocket of its
Foldseek structural neighbours (not just the single best-overlapping one), all superposed into the
query pocket via Foldseek's own conserved-core alignment. The union/density of this chemical matter
is what the model has "seen" in this pocket family — even when no single training ligand matches the
query's shape, the cloud may collectively explain where/how AF3 places it. First step toward a
pharmacophore-interpretability view.

Reuses build_training_similarity's cached Foldseek hits (_fshits) + cached structures (_refcache),
so this makes ZERO new API calls. Emits systems/<id>/train_cloud.pdb (each ligand a separate LIG
residue, in the crystal copy-0 frame) and cloud_* fields in systems.json. compute_overlaps.py then
remaps the cloud into the displayed canonical copy for multi-copy crystals.
"""
import json
from pathlib import Path
import numpy as np
import build_training_similarity as b

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"
CLOUD_RADIUS = 10.0    # keep neighbour ligands whose aligned centroid is within this of the query ligand
MAX_CLOUD = 40         # cap rendered ligands per system (sorted by shape overlap to the query)
MAX_HITS = 40          # Foldseek neighbours to scan (all cached rows)


def process(s):
    sid, het = s["id"], s["ligand"]
    xtal = SYS / sid / "xtal.cif"
    if not xtal.exists():
        return None
    new_m = b.load(str(xtal))
    qlig = b.lig_atoms(new_m, het)
    if qlig is None:
        return None
    qpos, qrad = b.lig_arrays(qlig)
    qcen = qpos.mean(0)
    qpoly = b._first_poly(new_m)
    qca = b._poly_ca(qpoly) if qpoly is not None else None
    if qca is None:
        return None
    try:
        hits = b.search_pre_cutoff(b.longest_seq(new_m), sid)
    except Exception:
        return None
    if not hits:
        return None

    cloud = []   # (overlap, pdb, het, aligned_atoms[(elem,Position)])
    for h in hits[:MAX_HITS]:
        if not h.get("qAln"):
            continue
        try:
            ref_m = b.load(h["pdb"])
            sup = b.align_superpose(h, qca, qpos)
        except Exception:
            continue
        if sup is None:
            continue
        tr, rmsd, _ = sup
        if rmsd > b.MAX_LOCAL_RMSD:
            continue
        hets = sorted({r.name for ch in ref_m for r in ch
                       if not r.is_water() and r.het_flag == "H" and b.druglike(r.name)})
        for hh in hets:
            ref_res = b.lig_atoms(ref_m, hh)
            if ref_res is None:
                continue
            aligned = [(a.element.name, tr.apply(a.pos)) for a in ref_res if a.element.name != "H"]
            if not aligned:
                continue
            rpos = np.array([[p.x, p.y, p.z] for _, p in aligned])
            if np.linalg.norm(rpos.mean(0) - qcen) > CLOUD_RADIUS:
                continue                                   # ligand binds a different site in this homolog
            rrad = np.array([b.vdw(e) for e, _ in aligned])
            ov = b.vol_tanimoto(qpos, qrad, rpos, rrad)
            cloud.append((round(ov, 3), h["pdb"], hh, aligned))

    if not cloud:
        return None
    cloud.sort(key=lambda x: -x[0])               # most query-like chemical matter first
    cloud = cloud[:MAX_CLOUD]

    # write all ligands into one PDB, each its own LIG residue (incrementing resSeq), copy-0 frame
    lines, serial = [], 0
    for resi, (_ov, _pdb, _het, aligned) in enumerate(cloud, 1):
        for elem, p in aligned:
            serial += 1
            lines.append(f"HETATM{serial:>5d} {('C'+str(serial))[:4]:<4s} LIG Z{resi:>4d}    "
                         f"{b._xyz(p.x, p.y, p.z)}  1.00  0.00          {elem:>2s}")
    lines.append("END")
    (SYS / sid / "train_cloud.pdb").write_text("\n".join(lines) + "\n")

    hets = {}
    for ov, pdb, hh, _ in cloud:
        hets[hh] = hets.get(hh, 0) + 1
    return {"cloud_file": f"systems/{sid}/train_cloud.pdb",
            "cloud_n": len(cloud),
            "cloud_n_pdbs": len({c[1] for c in cloud}),
            "cloud_hets": dict(sorted(hets.items(), key=lambda x: -x[1])),
            "cloud_best_overlap": cloud[0][0]}


def main():
    man = json.loads((HERE / "systems.json").read_text())
    n = 0
    for s in man["systems"]:
        for k in ("cloud_file", "cloud_n", "cloud_n_pdbs", "cloud_hets", "cloud_best_overlap"):
            s.pop(k, None)
        try:
            res = process(s)
        except Exception as e:
            print(f"  {s['id']}: error {str(e)[:70]}", flush=True); res = None
        if res:
            s.update(res); n += 1
            print(f"  {s['id']} ({s['ligand']}): cloud {res['cloud_n']} ligands "
                  f"from {res['cloud_n_pdbs']} PDBs · hets {list(res['cloud_hets'])[:6]}", flush=True)
    (HERE / "systems.json").write_text(json.dumps(man, indent=2, allow_nan=False))
    print(f"\nbuilt ligand clouds for {n} systems")


if __name__ == "__main__":
    main()
