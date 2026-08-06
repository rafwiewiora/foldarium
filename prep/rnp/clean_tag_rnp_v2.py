#!/usr/bin/env python3
"""v2 post-build cleaning + tagging.

Reproduces the two deterministic v1 filters (per RNP_NOVEL_WATERFALL.md / TODO.md), corrected
for the v2 pooling (v2 pools top-3 poses PER METHOD => up to ~12 poses/item, vs v1's ~3):

  1. UNALIGNED (item-level, unchanged): distinct-residue-count(pocket.pdb) == that of protein.pdb
     -> pocket selection grabbed nothing (poses float 10-12 A off the crystal frame). Drop item.

  2. MIXED-COMPOUND (per-POSE, matching v1's "dropped poses whose atom count != crystal ligand
     -> 187 poses + 4 items"): a pose whose heavy-atom count differs from the crystal ligand by
     more than MIX_TOL is a different molecule (a method predicted the wrong ligand); its
     atom-index-matched RMSD is garbage. DROP THAT POSE. Then, if an item is left with <2 poses,
     drop the item. MIX_TOL=2 tolerates protonation/tautomer/terminal-atom rendering diffs
     (the +-1 noise seen across methods) while removing genuine wrong-molecule poses.

Tags on survivors:
  - novel   : carried from plan (rnp_annotations (system_id, ligand_instance_chain),
              sucos_shape_pocket_qcov < 25).
  - n_heavy : crystal ligand heavy-atom count (xtal_lig.pdb), else max surviving-pose atom count.
  - build_bucket / bucket / has_correct / n_correct recomputed after per-pose drops.

Input/Output: QUIZ/quiz_items_rnp_v2.json (overwritten with cleaned+tagged list).
Backup of pre-clean assembled items -> QUIZ/quiz_items_rnp_v2.RAW.json.
"""
import json, os
from collections import Counter

QUIZ = "/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/quiz"
DATA = f"{QUIZ}/data_rnp_v2"
JSON = f"{QUIZ}/quiz_items_rnp_v2.json"
RAW = f"{QUIZ}/quiz_items_rnp_v2.RAW.json"
MIX_TOL = 2

def distinct_res(path):
    if not os.path.exists(path):
        return None
    res = set()
    for ln in open(path):
        if ln.startswith(('ATOM', 'HETATM')):
            res.add((ln[21:22], ln[22:26].strip(), ln[17:20].strip()))
    return len(res)

def atom_count(path):
    if not os.path.exists(path):
        return None
    return sum(1 for ln in open(path) if ln.startswith(('ATOM', 'HETATM')))

items = json.load(open(JSON))
json.dump(items, open(RAW, 'w'), indent=1)   # backup raw assembled

stats = Counter()
kept = []
for it in items:
    iid = it['id']; dd = f"{DATA}/{iid}"
    prot = distinct_res(f"{dd}/protein.pdb")
    pock = distinct_res(f"{dd}/pocket.pdb")
    xn = atom_count(f"{dd}/xtal_lig.pdb")

    # (1) unaligned -> drop item
    if prot is not None and pock is not None and prot == pock:
        stats['drop_unaligned'] += 1
        if it.get('novel') is True:
            stats['drop_unaligned_novel'] += 1
        continue

    # (2) per-pose mixed-compound drop
    if xn is not None:
        surv = []
        for c in it['choices']:
            pcn = atom_count(f"{QUIZ}/{c['pose_file']}")
            if pcn is None or abs(pcn - xn) <= MIX_TOL:
                surv.append(c)
            else:
                stats['drop_pose_mixed'] += 1
        if len(surv) != len(it['choices']):
            stats['items_with_pose_drop'] += 1
        it['choices'] = surv

    if len(it['choices']) < 2:
        stats['drop_item_nonviable'] += 1
        if it.get('novel') is True:
            stats['drop_item_nonviable_novel'] += 1
        continue

    ncorr = sum(1 for c in it['choices'] if c['correct'])
    nwrong = len(it['choices']) - ncorr
    if ncorr == len(it['choices']):
        bb = 'all-correct'
    elif ncorr >= 1 and nwrong >= 1:
        bb = 'game-able'
    else:
        bb = 'all-wrong'
    it['build_bucket'] = bb
    it['bucket'] = bb
    it['has_correct'] = bool(ncorr > 0)
    it['n_correct'] = ncorr

    pose_counts = [atom_count(f"{QUIZ}/{c['pose_file']}") for c in it['choices']]
    pose_counts = [p for p in pose_counts if p is not None]
    it['n_heavy'] = xn if xn is not None else (max(pose_counts) if pose_counts else 0)

    kept.append(it)
    stats['kept'] += 1

json.dump(kept, open(JSON, 'w'), indent=1)
print(json.dumps(dict(stats), indent=2))
print(f"cleaned -> {len(kept)} items -> {JSON}")
print(f"raw backup -> {RAW}")
