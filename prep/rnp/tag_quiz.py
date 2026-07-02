#!/usr/bin/env python
"""Re-tag all RnP quiz items with novel + train_qcov from authoritative computed novelty.
Drop 178 malformed items (id ends '__', no crystal ref). Assert zero untagged."""
import json, pandas as pd

QF = '/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/quiz/quiz_items_rnp.json'

nov = pd.read_parquet('/Users/rafalwiewiora/rnp_data/novelty_computed.parquet')
# build itemid -> (novel, qcov)
def mkid(s, l): return (str(s) + '__' + str(l)).replace('.', '_')
id2 = {}
for s, l, q, qf, n in zip(nov['query_system'], nov['query_ligand_instance_chain'],
                          nov['train_qcov'], nov['train_qcov_filled'], nov['novel']):
    id2[mkid(s, l)] = (bool(n), float(qf), (None if pd.isna(q) else float(q)))

items = json.load(open(QF))
print(f"loaded {len(items)} items")

malformed = [i for i in items if i['id'].endswith('__')]
wellformed = [i for i in items if not i['id'].endswith('__')]
print(f"malformed (drop): {len(malformed)}  well-formed: {len(wellformed)}")

# --- audit what we drop: would any malformed have been demo-eligible novel? ---
# malformed have no query_ligand_instance_chain we can map; try to recover system+lic anyway
# by matching against parquet keys ignoring the trailing empty lic is impossible (lic empty).
# Report their build_bucket / has_correct / n_heavy so nothing novel is silently lost.
def demo_eligible(it):
    return (it.get('n_heavy', 0) >= 15 and it.get('has_correct') is not None)
mal_summary = {}
for it in malformed:
    bb = it.get('build_bucket') or it.get('bucket')
    mal_summary[bb] = mal_summary.get(bb, 0) + 1
mal_nheavy15 = sum(1 for it in malformed if it.get('n_heavy', 0) >= 15)
mal_xtal = sum(1 for it in malformed if it.get('xtal_lig_file'))
print(f"malformed by bucket: {mal_summary}")
print(f"malformed with n_heavy>=15: {mal_nheavy15}  with xtal_lig_file(non-null): {mal_xtal}")

# --- tag well-formed ---
unmatched = []
for it in wellformed:
    rec = id2.get(it['id'])
    if rec is None:
        unmatched.append(it['id'])
        continue
    n, qf, q = rec
    it['novel'] = n
    it['train_qcov'] = qf  # filled (0.0 when no surviving target)
print(f"unmatched well-formed ids: {len(unmatched)}")
if unmatched:
    print("  sample:", unmatched[:10])

# assert zero untagged among wellformed
still_null = [it['id'] for it in wellformed if it.get('novel') is None]
assert not still_null, f"UNTAGGED REMAIN: {len(still_null)} e.g. {still_null[:5]}"
print("ASSERT PASSED: zero untagged among well-formed items")

# counts
n_novel = sum(1 for it in wellformed if it['novel'] is True)
n_not = sum(1 for it in wellformed if it['novel'] is False)
print(f"\nFINAL: {len(wellformed)} items | novel={n_novel} not-novel={n_not}")

# demo-eligible novel: n_heavy>=15 AND game-able bucket (all-wrong / all-correct)
def gameable(it):
    bb = (it.get('build_bucket') or it.get('bucket') or '')
    return bb in ('all-wrong', 'all-correct', 'mixed')
demo_novel = [it for it in wellformed if it['novel'] and it.get('n_heavy', 0) >= 15]
demo_novel_game = [it for it in demo_novel if gameable(it)]
print(f"demo-eligible novel (n_heavy>=15): {len(demo_novel)}")
by_b = {}
for it in demo_novel:
    bb = it.get('build_bucket') or it.get('bucket')
    by_b[bb] = by_b.get(bb, 0) + 1
print(f"  by bucket: {by_b}")

json.dump(wellformed, open(QF, 'w'), indent=1)
print(f"\nwrote {len(wellformed)} items to {QF} (dropped {len(malformed)} malformed)")
