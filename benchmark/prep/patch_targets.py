"""Patch mislabeled targets in systems.json (caffeine TEP -> screening fragment).

For each system, re-read its CAMEO m3 ligand_pose.json across all models and apply
the corrected target-selection rule:
  - candidate ligands = those passing build_systems.drug_like(het, atom_count) AND
    having >=1 non-null rmsd.
  - EXCLUDE caffeine TEP from being the target IF at least one other candidate
    exists (TEP is the co-crystallized A2A reference, never the screening target).
  - among remaining candidates pick the one with the largest atom_count (original
    tie-break preserved: first-wins on equal atom_count, iteration order).
  - per-model rmsd = median over that ligand's copies; correct = median < 2.0.
  - system n_atoms = atom_count.

Applies ONLY where the selected target differs from the current s["ligand"].
"""
import json
import glob
import re
from pathlib import Path
from collections import defaultdict
from statistics import median

import build_systems

HERE = Path(__file__).resolve().parent
M3 = Path("/tmp/cameo_week/m3")
TARGET_EXCLUDE = {"TEP"}


def collect_system(sid):
    """het -> {atoms, rmsd:{model:[rmsd,...]}} reading m3 across all models."""
    ligs = defaultdict(lambda: {"atoms": None, "rmsd": defaultdict(list)})
    files = sorted(glob.glob(str(
        M3 / "modeling" / "*" / sid / "servers" / "server993" /
        "model-*" / "scores" / "ligand_pose.json")))
    for f in files:
        mdl = int(re.search(r"model-(\d+)", f).group(1))
        try:
            d = json.load(open(f))["results"]["details"]["ligand_pose"]["ligands"]
        except Exception:
            continue
        for k, v in d.items():
            r = v.get("rmsd")
            if r is None:
                continue
            het = k.split(".")[-1].upper()
            L = ligs[het]
            L["atoms"] = v.get("atom_count")
            L["rmsd"][mdl].append(float(r))
    return ligs


def select_target(ligs):
    """Apply corrected target-selection rule. Returns (het, atoms, {mdl: medrmsd})."""
    cands = [het for het, L in ligs.items()
             if build_systems.drug_like(het, L["atoms"]) and L["rmsd"]]
    if not cands:
        return None
    # exclude TEP only if another candidate exists
    non_excl = [h for h in cands if h not in TARGET_EXCLUDE]
    pool = non_excl if non_excl else cands
    # original tie-break: keep first het that has the strictly-largest atom_count,
    # iterating in dict insertion order (same as build_systems.candidates()).
    best = None
    for het in ligs:  # preserve original iteration order over the ligand dict
        if het not in pool:
            continue
        if best is None or (ligs[het]["atoms"] or 0) > (ligs[best]["atoms"] or 0):
            best = het
    L = ligs[best]
    med = {mdl: median(rs) for mdl, rs in L["rmsd"].items()}
    return best, L["atoms"], med


def main():
    sysjson = HERE / "systems.json"
    manifest = json.loads(sysjson.read_text())
    changes = []
    for s in manifest["systems"]:
        sid = s["id"]
        ligs = collect_system(sid)
        sel = select_target(ligs)
        if sel is None:
            continue
        new_het, new_atoms, med = sel
        if new_het == s["ligand"]:
            continue  # no change
        old_het = s["ligand"]
        # update poses: per-model rmsd = median over copies; correct = <2.0
        new_med_list, new_correct_list = [], []
        for p in s["poses"]:
            mdl = p["sample"]
            if mdl in med:
                r = round(med[mdl], 2)
                p["rmsd"] = r
                p["correct"] = bool(med[mdl] < 2.0)
            new_med_list.append(p["rmsd"])
            new_correct_list.append(p["correct"])
        s["ligand"] = new_het
        s["n_atoms"] = new_atoms
        nc = sum(p["correct"] for p in s["poses"])
        n = len(s["poses"])
        best_rmsd = min(p["rmsd"] for p in s["poses"])
        s["blurb"] = f"{nc}/{n} correct, best {best_rmsd}Å"
        changes.append((sid, old_het, new_het, new_med_list, new_correct_list))

    print("=== GATE A: target changes ===")
    for sid, old, new, meds, corr in changes:
        print(f"{sid}: {old}->{new} newMedRMSDs={meds} newCorrect={corr}")
    print(f"\nTOTAL changes: {len(changes)}")
    print("changed SIDs:", sorted(c[0] for c in changes))

    sysjson.write_text(json.dumps(manifest, indent=2, allow_nan=False))
    print("wrote systems.json")


if __name__ == "__main__":
    main()
