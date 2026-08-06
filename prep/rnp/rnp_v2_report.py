#!/usr/bin/env python3
"""Final diff report: v1 (current) vs v2 quiz items, novelty splits, 80-target recovery,
per-stage attrition of newly-added systems, and the all-correct positive-control class."""
import json, pickle, csv, os
from collections import Counter, defaultdict

QUIZ = "/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/quiz"
RNP = "/Users/rafalwiewiora/rnp_data"
V1 = f"{QUIZ}/quiz_items_rnp.json"
V2 = f"{QUIZ}/quiz_items_rnp_v2.json"
PLAN2 = f"{RNP}/rnp_quiz_plan_v2.pkl"
ANNOT = f"{QUIZ.rsplit('/',1)[0]}/sucos/rnp_annotations.csv"

CORRECT = 1.5   # strict game-able: has pose <1.5
WRONG = 3.0     # strict: >3

def load(p):
    return json.load(open(p)) if os.path.exists(p) else None

def strict_bucket(item):
    rmsds = [c['rmsd'] for c in item['choices']]
    has_close = any(r < CORRECT for r in rmsds)
    has_far = any(r > WRONG for r in rmsds)
    all_far = all(r > WRONG for r in rmsds)
    all_close = all(r < CORRECT for r in rmsds)
    if has_close and has_far:
        return 'gameable'
    if all_far:
        return 'allwrong'
    if all_close:
        return 'allcorrect_clean'
    return 'other'

def summarize(items, label):
    n = len(items)
    nov = sum(1 for i in items if i.get('novel') is True)
    print(f"\n=== {label}: {n} items, {nov} novel ===")
    # novel strict split
    nov_items = [i for i in items if i.get('novel') is True]
    sb = Counter(strict_bucket(i) for i in nov_items)
    print(f"  novel strict split: gameable={sb['gameable']} allwrong={sb['allwrong']} "
          f"allcorrect_clean={sb['allcorrect_clean']} other={sb['other']}")
    # build_bucket distribution (v2 only)
    if items and 'build_bucket' in items[0]:
        bb = Counter(i.get('build_bucket') for i in items)
        bbn = Counter(i.get('build_bucket') for i in items if i.get('novel') is True)
        print(f"  build_bucket all: {dict(bb)}")
        print(f"  build_bucket novel: {dict(bbn)}")
    # demo-eligible novel (n_heavy>=15)
    demo = sum(1 for i in nov_items if i.get('n_heavy', 0) >= 15)
    print(f"  novel with n_heavy>=15 (demo-eligible): {demo}")
    return n, nov

v1 = load(V1); v2 = load(V2)
print("#" * 70)
print("RnP QUIZ v1 (current) vs v2 (full-archive rebuild)")
print("#" * 70)
n1, nov1 = summarize(v1, "v1 CURRENT")
if v2 is not None:
    n2, nov2 = summarize(v2, "v2 REBUILD")

    # all-correct positive control detail
    ac = [i for i in v2 if i.get('build_bucket') == 'all-correct']
    ac_nov = [i for i in ac if i.get('novel') is True]
    ac_clean = sum(1 for i in ac if all(c['rmsd'] < CORRECT for c in i['choices']))
    ac_nov_clean = sum(1 for i in ac_nov if all(c['rmsd'] < CORRECT for c in i['choices']))
    print(f"\n=== ALL-CORRECT positive-control class (v2) ===")
    print(f"  total all-correct items: {len(ac)}  (novel: {len(ac_nov)})")
    print(f"  clean all-correct (EVERY pose <{CORRECT}): {ac_clean}  (novel: {ac_nov_clean})")
    print(f"  partial all-correct (all <2 but some >= {CORRECT}): {len(ac)-ac_clean}  "
          f"(novel: {len(ac_nov)-ac_nov_clean})")

    # 80-target recovery
    missing = [tuple(x) for x in json.load(open('/tmp/missing_novel.json'))]
    mset = set(missing)
    # which v2 items correspond to a missing (target,instchain)?  item id = target__instchain sanitized.
    # Reconstruct via plan (has target/instchain).
    plan2 = pickle.load(open(PLAN2, 'rb'))
    def iid_of(target, inst): return f"{target}__{inst}".replace('.', '_')
    plan_by_id = {iid_of(p['target'], p['instchain']): p for p in plan2}
    v2_ids = set(i['id'] for i in v2)
    # map missing (t,ic) -> iid
    recovered = []
    for (t, ic) in missing:
        iid = iid_of(t, ic)
        if iid in v2_ids:
            recovered.append((t, ic))
    print(f"\n=== 80 target-novel recovery ===")
    print(f"  of {len(missing)} missing-novel pairs, in FINAL v2 quiz: {len(recovered)}")
    # where did the rest fall out? check plan presence and cleaning
    in_plan = set(iid_of(t, ic) for (t, ic) in missing if iid_of(t, ic) in plan_by_id)
    print(f"  reached the v2 PLAN (post multi-pocket/coords): "
          f"{len(in_plan)} / {len(missing)}")
    # of those in plan, which build_bucket
    bb_missing = Counter(plan_by_id[iid_of(t, ic)].get('build_bucket')
                         for (t, ic) in missing if iid_of(t, ic) in plan_by_id)
    print(f"  plan build_bucket of recovered-into-plan: {dict(bb_missing)}")
    # fell out at cleaning = in plan but not in final json
    fell_clean = [(t, ic) for (t, ic) in missing
                  if iid_of(t, ic) in plan_by_id and iid_of(t, ic) not in v2_ids]
    print(f"  in plan but dropped by cleaning (unaligned/mixed): {len(fell_clean)}")
    # never reached plan
    never = [(t, ic) for (t, ic) in missing if iid_of(t, ic) not in plan_by_id]
    print(f"  never reached plan (multi-pocket / too-few-aligned / no coords): {len(never)}")
    for x in never:
        print("      never-in-plan:", x)
else:
    print("\n[v2 quiz JSON not present yet]")
