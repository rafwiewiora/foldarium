#!/usr/bin/env python3
import csv, json, statistics
from collections import defaultdict

TABLE="/Users/rafalwiewiora/rnp_data/xmethod_plddt_table.csv"
ANNOT="/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/sucos/rnp_annotations.csv"
OUT="/Users/rafalwiewiora/rnp_data/xmethod_plddt_results.json"
METHODS=['af3','boltz','chai','protenix']

# ---- load table ----
rows=[]
with open(TABLE) as f:
    for r in csv.DictReader(f):
        if r['ligand_plddt']=='' : continue
        rows.append({'target':r['target'],'instchain':r['instchain'],'method':r['method'],
                     'seed':r['seed'],'sample':r['sample'],
                     'rmsd':float(r['rmsd']),'rs':float(r['ranking_score']) if r['ranking_score'] not in('','nan') else None,
                     'plddt':float(r['ligand_plddt'])})

# ---- novelty map: system_id -> sucos_shape_pocket_qcov (min across rows = most novel) ----
nov={}
with open(ANNOT) as f:
    for r in csv.DictReader(f):
        sid=r['system_id']; v=r.get('sucos_shape_pocket_qcov','')
        try: v=float(v)
        except: continue
        # keep min (most conservative novelty per system)
        if sid not in nov or v<nov[sid]: nov[sid]=v

def med_iqr(vals):
    if not vals: return (None,None,None,0)
    vals=sorted(vals); n=len(vals)
    q=statistics.quantiles(vals,n=4) if n>=2 else [vals[0],vals[0],vals[0]]
    return (round(statistics.median(vals),2), round(q[0],2), round(q[2],2), n)

# ===== STEP 3: distribution comparability =====
dist={}
for m in METHODS:
    cor=[r['plddt'] for r in rows if r['method']==m and r['rmsd']<2]
    wro=[r['plddt'] for r in rows if r['method']==m and r['rmsd']>=2]
    # spearman-ish: correlation of plddt vs (rmsd<2) within method, and point-biserial
    allp=[r['plddt'] for r in rows if r['method']==m]
    allc=[1 if r['rmsd']<2 else 0 for r in rows if r['method']==m]
    # pearson point-biserial
    import math
    n=len(allp)
    if n>1 and len(set(allc))>1:
        mp=sum(allp)/n; mc=sum(allc)/n
        num=sum((a-mp)*(b-mc) for a,b in zip(allp,allc))
        den=math.sqrt(sum((a-mp)**2 for a in allp)*sum((b-mc)**2 for b in allc))
        pb=num/den if den else None
    else: pb=None
    dist[m]={'correct_med_q1_q3_n':med_iqr(cor),'wrong_med_q1_q3_n':med_iqr(wro),
             'pointbiserial_plddt_vs_correct':round(pb,3) if pb is not None else None,
             'separation_med_corr_minus_wrong':round((statistics.median(cor)-statistics.median(wro)),2) if cor and wro else None}

# ===== group rows by (method, system) where system=(target,instchain) =====
def group(rows_subset):
    g=defaultdict(list)
    for r in rows_subset:
        g[(r['method'],r['target'],r['instchain'])].append(r)
    return g

# gameable systems per method (recompute from table: min<2 and max>=2)
def gameable_systems(rows_subset):
    rm=defaultdict(list)
    for r in rows_subset:
        rm[(r['method'],r['target'],r['instchain'])].append(r['rmsd'])
    gs=set()
    for k,v in rm.items():
        if min(v)<2 and max(v)>=2: gs.add(k)
    return gs

# ===== STEP 4: per-method selection =====
def selection_stats(rows_subset, gs):
    g=group(rows_subset)
    res={m:{'n':0,'rs_correct':0,'plddt_correct':0,'oracle':0} for m in METHODS}
    for key in gs:
        m=key[0]; poses=g[key]
        if not poses: continue
        res[m]['n']+=1
        # ranking_score: higher = better (af3/boltz/chai/protenix all higher-better)
        rs_poses=[p for p in poses if p['rs'] is not None]
        if rs_poses:
            top_rs=max(rs_poses,key=lambda p:p['rs'])
            if top_rs['rmsd']<2: res[m]['rs_correct']+=1
        top_pl=max(poses,key=lambda p:p['plddt'])
        if top_pl['rmsd']<2: res[m]['plddt_correct']+=1
        if min(p['rmsd'] for p in poses)<2: res[m]['oracle']+=1
    out={}
    for m in METHODS:
        n=res[m]['n']
        if n==0: out[m]=None; continue
        out[m]={'n':n,
                'rs_top1_pct':round(100*res[m]['rs_correct']/n,1),
                'plddt_top1_pct':round(100*res[m]['plddt_correct']/n,1),
                'oracle_pct':round(100*res[m]['oracle']/n,1),
                'lift_plddt_over_rs_pp':round(100*(res[m]['plddt_correct']-res[m]['rs_correct'])/n,1)}
    return out

gs_all=gameable_systems(rows)
step4=selection_stats(rows, gs_all)

# ===== STEP 5: cross-method pooled selection =====
# pool all poses per (target,instchain) across methods; pick global max plddt.
# pooled-gameable system = union over methods where that (target,instchain) is gameable for >=1 method,
# OR define on pooled poses. We'll use: systems that are gameable for at least one method (per task union),
# and also report pooled-oracle (any method,any pose <2).
pool=defaultdict(list)
for r in rows:
    pool[(r['target'],r['instchain'])].append(r)

# union gameable (target,instchain) ignoring method
union_sys=set((t,c) for (m,t,c) in gs_all)
n5=0; xmethod_correct=0; pooled_oracle=0
permethod_best_oracle=0
for sysk in union_sys:
    poses=pool.get(sysk,[])
    if not poses: continue
    n5+=1
    top=max(poses,key=lambda p:p['plddt'])
    if top['rmsd']<2: xmethod_correct+=1
    if min(p['rmsd'] for p in poses)<2: pooled_oracle+=1
step5={'n_systems':n5,
       'xmethod_maxplddt_correct_pct':round(100*xmethod_correct/n5,1) if n5 else None,
       'pooled_oracle_any_method_pct':round(100*pooled_oracle/n5,1) if n5 else None}

# ===== STEP 6: novelty split (novel = sucos_shape_pocket_qcov < 25) =====
def is_novel(target):
    v=nov.get(target)
    return (v is not None and v<25)
rows_novel=[r for r in rows if is_novel(r['target'])]
rows_fam=[r for r in rows if (r['target'] in nov and not is_novel(r['target']))]
gs_nov=gameable_systems(rows_novel)
gs_fam=gameable_systems(rows_fam)
step6={'novel':selection_stats(rows_novel,gs_nov),
       'familiar':selection_stats(rows_fam,gs_fam),
       'n_systems_with_novelty_annot': len(set((r['target'],r['instchain']) for r in rows if r['target'] in nov)),
       'n_total_systems': len(set((r['target'],r['instchain']) for r in rows))}

results={'methods':METHODS,
         'n_gameable_per_method':{m:sum(1 for k in gs_all if k[0]==m) for m in METHODS},
         'n_union_gameable_systems': len(union_sys),
         'step3_distribution_comparability':dist,
         'step4_per_method_selection':step4,
         'step5_crossmethod_pooled':step5,
         'step6_novelty_split':step6}
json.dump(results, open(OUT,'w'), indent=2)
print(json.dumps(results, indent=2))
