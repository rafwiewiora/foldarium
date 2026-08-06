#!/usr/bin/env python
"""Authoritative RnP novelty computation using RCSB initial_release_date for the
post-cutoff exclusion (matches authors' new_pdb_ids.txt). Validates vs annotations.csv."""
import json, sys
import pandas as pd, numpy as np

CUTOFF = pd.to_datetime('2021-09-30')
cache = json.load(open('/Users/rafalwiewiora/rnp_data/rcsb_release_dates.json'))

# post-cutoff PDB set from RCSB initial_release_date. Unknown -> treat as pre-cutoff (not excluded).
post_cutoff = set()
missing = []
for pdb, d in cache.items():
    if not d:
        missing.append(pdb)
        continue
    if pd.to_datetime(d).tz_localize(None) > CUTOFF:
        post_cutoff.add(pdb.upper())
print(f"post_cutoff PDBs: {len(post_cutoff)}  missing(no date): {len(missing)}", flush=True)

df = pd.read_parquet('/tmp/rnp_allsim.parquet',
    columns=['query_system','target_system','query_ligand_instance_chain','sucos_shape_pocket_qcov','target_release_date'])
df['qpdb'] = df['query_system'].str[:4].str.upper()
df['tpdb'] = df['target_system'].str[:4].str.upper()

# post-cutoff by EITHER RCSB initial_release_date OR parquet target_release_date (deposit).
# UNION reproduces authors' new_pdb_ids.txt exactly (100% match on annotations.csv).
post_by_pq = df['target_release_date'] > CUTOFF
post_by_rcsb = df['tpdb'].isin(post_cutoff)
# exclusion: target not post-cutoff (by either), target pdb != query pdb (self)
mask = (~(post_by_pq | post_by_rcsb)) & (df['qpdb'] != df['tpdb'])
sub = df[mask]
agg = sub.groupby(['query_system','query_ligand_instance_chain'])['sucos_shape_pocket_qcov'].max().reset_index()
agg.columns = ['query_system','query_ligand_instance_chain','train_qcov']
print(f"query-lic keys with >=1 surviving pre-cutoff target: {len(agg)}", flush=True)

# all keys
allkeys = df[['query_system','query_ligand_instance_chain']].dropna().drop_duplicates()
full = allkeys.merge(agg, on=['query_system','query_ligand_instance_chain'], how='left')
# no surviving target -> qcov effectively 0 -> novel
full['train_qcov_filled'] = full['train_qcov'].fillna(0.0)
full['novel'] = full['train_qcov_filled'] < 25.0
print(f"total keys: {len(full)}  no-surviving-target: {full['train_qcov'].isna().sum()}", flush=True)
full.to_parquet('/Users/rafalwiewiora/rnp_data/novelty_computed.parquet', index=False)

# ---- VALIDATION ----
ann = pd.read_csv('/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/sucos/rnp_annotations.csv',
    usecols=['query_system','query_ligand_instance_chain','sucos_shape_pocket_qcov'])
ann_f = ann[ann['sucos_shape_pocket_qcov'].notna()].copy()
m = ann_f.merge(agg, on=['query_system','query_ligand_instance_chain'], how='left')
present = m['train_qcov'].notna()
print(f"\n=== VALIDATION (RCSB initial_release_date) ===", flush=True)
print(f"annotations filled: {len(ann_f)}  computed present: {present.sum()}  missing: {(~present).sum()}", flush=True)
mm = m[present]
diff = (mm['sucos_shape_pocket_qcov'] - mm['train_qcov']).abs()
print(f"within 1.0: {(diff<=1.0).sum()}/{len(mm)} = {100*(diff<=1.0).mean():.2f}%", flush=True)
print(f"maxerr: {diff.max():.4f}  mean: {diff.mean():.6f}", flush=True)
worst = mm.assign(diff=diff).sort_values('diff', ascending=False).head(8)
print(worst[['query_system','query_ligand_instance_chain','sucos_shape_pocket_qcov','train_qcov','diff']].to_string(), flush=True)

# blank hypothesis
blank = ann[ann['sucos_shape_pocket_qcov'].isna()]
bm = blank.merge(agg, on=['query_system','query_ligand_instance_chain'], how='left')
print(f"\nblank annotation rows: {len(blank)}  -> no surviving target: {bm['train_qcov'].isna().sum()}  contradiction(has target): {bm['train_qcov'].notna().sum()}", flush=True)
