#!/usr/bin/env python3
"""Search interpretable decision-rule cascades + shallow decision tree to pick the
correct pose from the pooled multi-method Runs-n-Poses ensemble.

Loads features_table.pkl (per-system blind features + candidate poses with correctness).
Evaluates accuracy on POOL-ORACLE systems (oracle=100% by construction).
Reports ALL and NOVEL, vs baselines. Honest grouped-CV for the learned tree.
"""
import pickle, json, sys, numpy as np
from collections import defaultdict

PKL="/Users/rafalwiewiora/rnp_data/features_table.pkl"
OUT="/Users/rafalwiewiora/rnp_data/rule_search_results.json"
METHODS=['af3','boltz','chai','protenix']

D=pickle.load(open(PKL,'rb'))
rows=D['rows']; AVAIL=D['avail_methods']
print(f"loaded {len(rows)} systems, methods={AVAIL}", file=sys.stderr)

def grp(target):  # protein group = first PDB-id token before '__'
    return target.split('__')[0]

# ---------- helpers
def is_correct_candidate(r, src):
    c=r['candidates'].get(src)
    return None if c is None else c['correct']==1

def acc_of_picker(picker, subset=None):
    """picker(r)->candidate source string. Accuracy over subset rows."""
    cor=0;tot=0;misses=0
    for r in rows:
        if subset=='novel' and r['novel']!=True: continue
        if subset=='familiar' and r['novel']!=False: continue
        src=picker(r)
        if src is None or src not in r['candidates']:
            # picker failed to choose -> count as wrong (it had to pick something)
            tot+=1; misses+=1; continue
        tot+=1
        if r['candidates'][src]['correct']==1: cor+=1
    return (round(100*cor/tot,1) if tot else None, tot)

# ---------- BASELINES (sanity)
def pick_global_maxplddt(r): return 'global_maxplddt'
def pick_consensus(r): return 'consensus_medoid'

# rank methods by reliability of each baseline-ish candidate on ALL systems
# (data-driven method ordering for cascades)
def candidate_acc(src):
    cor=tot=0
    for r in rows:
        c=r['candidates'].get(src)
        if c is None: continue
        tot+=1; cor+=c['correct']
    return (round(100*cor/tot,1),tot) if tot else (None,0)

# accuracy of "M toprank pose" and "M single->M toprank" per method
print("=== per-candidate raw accuracy (ALL pool-oracle) ===", file=sys.stderr)
cand_accs={}
for src in (sorted(set().union(*[set(r['candidates']) for r in rows]))):
    cand_accs[src]=candidate_acc(src)
    print(f"  {src}: {cand_accs[src]}", file=sys.stderr)

# reliability of "method single-cluster -> its toprank pose" : among systems where M is single,
# how often is M's toprank correct? (this is the cascade trigger quality)
single_reliability={}
for M in METHODS:
    cor=tot=0
    for r in rows:
        if r.get(f'{M}_single')==1 and r.get(f'{M}_present')==1:
            src=f'{M}_toprank'
            c=r['candidates'].get(src)
            if c is None: continue
            tot+=1; cor+=c['correct']
    single_reliability[M]=(round(100*cor/tot,1) if tot else None, tot)
print("=== single-cluster->toprank reliability per method ===", file=sys.stderr)
for M in METHODS: print(f"  {M}: {single_reliability[M]}", file=sys.stderr)

# order methods by single-reliability (desc), ignoring None
order_methods=sorted([M for M in METHODS if single_reliability[M][0] is not None],
                     key=lambda M:single_reliability[M][0], reverse=True)
print("method reliability order:",order_methods, file=sys.stderr)

# ---------- HAND-CRAFTED CASCADES
def best_single_method_pick(r):
    """among methods that are single-cluster, pick the one highest in reliability order;
    tie broken by toprank plddt."""
    cands=[M for M in order_methods if r.get(f'{M}_single')==1 and r.get(f'{M}_present')==1]
    if not cands: return None
    # reliability order already sorted; first is most reliable
    return f'{cands[0]}_toprank'

def cascade_a(r):
    # if af3 single -> af3 toprank ; else global max plddt
    if r.get('af3_single')==1 and r.get('af3_present')==1: return 'af3_toprank'
    return 'global_maxplddt'

def cascade_b(r):
    # if ANY method single (best by reliability) -> that method toprank; else consensus; else maxplddt
    s=best_single_method_pick(r)
    if s is not None: return s
    if 'consensus_medoid' in r['candidates']: return 'consensus_medoid'
    return 'global_maxplddt'

def make_cascade_c(K):
    def f(r):
        if r.get('n_methods_in_top',0)>=K: return 'consensus_medoid'
        if r.get('af3_single')==1 and r.get('af3_present')==1: return 'af3_toprank'
        return 'global_maxplddt'
    return f

def cascade_d(r):
    # reliability-ordered single; else if n_methods_in_top>=2 consensus; else global maxplddt
    s=best_single_method_pick(r)
    if s is not None: return s
    if r.get('n_methods_in_top',0)>=2: return 'consensus_medoid'
    return 'global_maxplddt'

def cascade_e(r):
    # plurality strong -> consensus medoid; elif af3 single -> af3 ; else global maxplddt
    if r.get('plurality',0)>=0.6: return 'consensus_medoid'
    if r.get('af3_single')==1 and r.get('af3_present')==1: return 'af3_toprank'
    return 'global_maxplddt'

def cascade_f(r):
    # best single -> that; elif plurality>=0.6 consensus; else global maxplddt
    s=best_single_method_pick(r)
    if s is not None: return s
    if r.get('plurality',0)>=0.6: return 'consensus_medoid'
    return 'global_maxplddt'

CASCADES={
 'consensus(baseline)':pick_consensus,
 'global_maxplddt(baseline)':pick_global_maxplddt,
 '(a) af3_single->af3 else gmaxplddt':cascade_a,
 '(b) bestsingle->else consensus->else gmaxpl':cascade_b,
 '(c2) ntop>=2->consensus elif af3single->af3 else gmaxpl':make_cascade_c(2),
 '(c3) ntop>=3->consensus elif af3single->af3 else gmaxpl':make_cascade_c(3),
 '(d) bestsingle->ntop>=2 consensus->gmaxpl':cascade_d,
 '(e) plur>=.6 consensus->af3single->gmaxpl':cascade_e,
 '(f) bestsingle->plur>=.6 consensus->gmaxpl':cascade_f,
}

# ---- GRID of threshold cascades: trigger feature/threshold -> consensus, else fallback
def make_grid_cascade(trigfeat, thr, primary, fallback):
    def f(r):
        v=r.get(trigfeat)
        if v is not None and v>=thr: return primary
        if fallback=='af3single':
            if r.get('af3_single')==1 and r.get('af3_present')==1: return 'af3_toprank'
            return 'global_maxplddt'
        return fallback
    return f
GRID={}
for tf_ in ['plurality','n_methods_in_top']:
    for thr in ([0.4,0.5,0.6,0.7,0.8] if tf_=='plurality' else [2,3]):
        for prim in ['consensus_medoid']:
            for fb in ['global_maxplddt','consensus_medoid','af3single']:
                GRID[f'grid:{tf_}>={thr}->{prim} else {fb}']=make_grid_cascade(tf_,thr,prim,fb)
CASCADES.update(GRID)

cascade_results={}
for name,fn in CASCADES.items():
    cascade_results[name]={
        'all':acc_of_picker(fn,None),
        'novel':acc_of_picker(fn,'novel'),
        'familiar':acc_of_picker(fn,'familiar'),
    }

# ---------- DECISION TREE: route to best candidate-source
# Frame: features -> predict which candidate SOURCE to take. We learn a classifier over a
# fixed candidate-source vocabulary; target label = a source that is correct (if any), chosen
# by a fixed priority so labels are deterministic. Then evaluate by the picked source's correctness.
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.model_selection import GroupKFold

# candidate source vocabulary (only sources that exist for most systems)
SRC_VOCAB=['af3_toprank','boltz_toprank','chai_toprank','protenix_toprank',
           'af3_maxplddt','boltz_maxplddt','chai_maxplddt','protenix_maxplddt',
           'consensus_medoid','global_maxplddt']
SRC_VOCAB=[s for s in SRC_VOCAB if any(s in r['candidates'] for r in rows)]

# numeric feature columns (blind)
FEATCOLS=['n_pose','n_methods_present','pooled_nclusters','plurality','n_methods_in_top',
          'global_maxplddt','consensus_medoid_plddt']
for M in METHODS:
    FEATCOLS += [f'{M}_present',f'{M}_single',f'{M}_nclusters',f'{M}_maxplddt',
                 f'{M}_top_self_frac',f'{M}_npose',f'{M}_toprank_plddt']

def featvec(r):
    v=[]
    for c in FEATCOLS:
        x=r.get(c)
        v.append(np.nan if x is None else float(x))
    return v

X=np.array([featvec(r) for r in rows])
# impute NaN with column medians (simple)
col_med=np.nanmedian(X,axis=0)
inds=np.where(np.isnan(X))
X[inds]=np.take(col_med,inds[1])
groups=np.array([grp(r['target']) for r in rows])
novel_mask=np.array([r['novel']==True for r in rows])

# deterministic training label: among SRC_VOCAB sources present & correct, pick by priority order
LABEL_PRIORITY=['consensus_medoid','af3_toprank','protenix_toprank','chai_toprank','boltz_toprank',
                'global_maxplddt','af3_maxplddt','protenix_maxplddt','chai_maxplddt','boltz_maxplddt']
LABEL_PRIORITY=[s for s in LABEL_PRIORITY if s in SRC_VOCAB]
def train_label(r):
    for s in LABEL_PRIORITY:
        c=r['candidates'].get(s)
        if c is not None and c['correct']==1: return s
    # no correct candidate among vocab (shouldn't happen often since pool-oracle) -> default consensus
    return 'consensus_medoid' if 'consensus_medoid' in SRC_VOCAB else SRC_VOCAB[0]

y=np.array([train_label(r) for r in rows])

def eval_picks(pred_srcs, mask=None):
    cor=tot=0
    for i,r in enumerate(rows):
        if mask is not None and not mask[i]: continue
        s=pred_srcs[i]
        c=r['candidates'].get(s)
        tot+=1
        if c is not None and c['correct']==1: cor+=1
    return (round(100*cor/tot,1) if tot else None, tot)

# grouped CV (by protein)
def cv_tree(depth, min_leaf=10, n_splits=5):
    gkf=GroupKFold(n_splits=n_splits)
    preds=np.array(['?']*len(rows),dtype=object)
    for tr,te in gkf.split(X,y,groups):
        clf=DecisionTreeClassifier(max_depth=depth,min_samples_leaf=min_leaf,random_state=0)
        clf.fit(X[tr],y[tr])
        preds[te]=clf.predict(X[te])
    return preds

tree_results={}
best_tree=None
for depth in [2,3]:
    for ml in [10,20,30]:
        preds=cv_tree(depth,ml)
        all_acc=eval_picks(preds,None)
        nov_acc=eval_picks(preds,novel_mask)
        tree_results[f'depth{depth}_minleaf{ml}']={'all_cv':all_acc,'novel_cv':nov_acc}
        if best_tree is None or all_acc[0]>best_tree[1]:
            best_tree=(f'depth{depth}_minleaf{ml}',all_acc[0],depth,ml)
print("=== tree CV results ===", file=sys.stderr)
for k,v in tree_results.items(): print(f"  {k}: {v}", file=sys.stderr)

# fit best tree on ALL data for interpretable rule export
bt_name,_,bt_depth,bt_ml=best_tree
clf=DecisionTreeClassifier(max_depth=bt_depth,min_samples_leaf=bt_ml,random_state=0)
clf.fit(X,y)
tree_text=export_text(clf,feature_names=FEATCOLS)

# ---------- SIGNIFICANCE: bootstrap CI on best cascade vs baselines (per-system resample, grouped by protein)
def per_system_correct(picker, subset=None):
    """return list of (group, correct01) over subset for a picker."""
    out=[]
    for r in rows:
        if subset=='novel' and r['novel']!=True: continue
        s=picker(r)
        c=r['candidates'].get(s) if (s and s in r['candidates']) else None
        out.append((grp(r['target']), 1 if (c is not None and c['correct']==1) else 0))
    return out

def bootstrap_diff(pickerA, pickerB, subset=None, nboot=2000, seed=0):
    """cluster bootstrap by protein group: resample groups with replacement, compute accA-accB."""
    rng=np.random.default_rng(seed)
    A=per_system_correct(pickerA,subset); B=per_system_correct(pickerB,subset)
    # group -> indices
    g2i=defaultdict(list)
    for i,(g,_) in enumerate(A): g2i[g].append(i)
    glist=list(g2i.keys())
    diffs=[]
    aA=np.array([c for _,c in A]); aB=np.array([c for _,c in B])
    obs=100*(aA.mean()-aB.mean())
    for _ in range(nboot):
        samp=rng.choice(len(glist),len(glist),replace=True)
        idx=[]
        for gi in samp: idx.extend(g2i[glist[gi]])
        idx=np.array(idx)
        diffs.append(100*(aA[idx].mean()-aB[idx].mean()))
    lo,hi=np.percentile(diffs,[2.5,97.5])
    p_gt0=float(np.mean(np.array(diffs)<=0))  # one-sided: P(diff<=0) ~ prob no improvement
    return {'obs_diff':round(obs,2),'ci95':[round(lo,2),round(hi,2)],'p_no_improve':round(p_gt0,3)}

# best cascade by 'all' (excluding baselines)
nonbase={k:v for k,v in CASCADES.items() if 'baseline' not in k}
best_casc_name=max(nonbase, key=lambda k: cascade_results[k]['all'][0])
best_casc_fn=CASCADES[best_casc_name]
best_novel_name=max(nonbase, key=lambda k: (cascade_results[k]['novel'][0], cascade_results[k]['all'][0]))
sig={
 'best_cascade':best_casc_name,
 'vs_consensus_all':bootstrap_diff(best_casc_fn,pick_consensus,None),
 'vs_consensus_novel':bootstrap_diff(best_casc_fn,pick_consensus,'novel'),
 'vs_gmaxplddt_all':bootstrap_diff(best_casc_fn,pick_global_maxplddt,None),
 'vs_gmaxplddt_novel':bootstrap_diff(best_casc_fn,pick_global_maxplddt,'novel'),
 'best_by_novel':best_novel_name,
 'best_by_novel_acc':{'all':cascade_results[best_novel_name]['all'],'novel':cascade_results[best_novel_name]['novel']},
 'best_by_novel_vs_gmaxplddt_novel':bootstrap_diff(CASCADES[best_novel_name],pick_global_maxplddt,'novel'),
}
print("=== SIGNIFICANCE (cluster bootstrap by protein) ===", file=sys.stderr)
print(json.dumps(sig,indent=1), file=sys.stderr)

# also: restrict to MULTI-METHOD systems (where consensus has real signal)
def acc_picker_multi(fn, subset=None):
    cor=tot=0
    for r in rows:
        if r['n_methods_present']<2: continue
        if subset=='novel' and r['novel']!=True: continue
        s=fn(r); c=r['candidates'].get(s) if (s and s in r['candidates']) else None
        tot+=1; cor+= 1 if (c and c['correct']==1) else 0
    return (round(100*cor/tot,1) if tot else None, tot)
multi_cmp={
 'best_cascade_all':acc_picker_multi(best_casc_fn),
 'best_cascade_novel':acc_picker_multi(best_casc_fn,'novel'),
 'consensus_all':acc_picker_multi(pick_consensus),
 'consensus_novel':acc_picker_multi(pick_consensus,'novel'),
 'gmaxplddt_all':acc_picker_multi(pick_global_maxplddt),
 'gmaxplddt_novel':acc_picker_multi(pick_global_maxplddt,'novel'),
}

# ---------- assemble + report
def fmt(t): return f"{t[0]}% (n={t[1]})"
summary={
 'n_systems_pool_oracle':len(rows),
 'n_novel':int(novel_mask.sum()),
 'n_familiar':int((~novel_mask & np.array([r['novel']==False for r in rows])).sum()),
 'methods_in_pool':AVAIL,
 'protenix_in_pool': 'protenix' in AVAIL,
 'baselines':{
   'consensus':cascade_results['consensus(baseline)'],
   'global_maxplddt':cascade_results['global_maxplddt(baseline)'],
 },
 'per_candidate_raw_acc':cand_accs,
 'single_cluster_toprank_reliability':single_reliability,
 'method_reliability_order':order_methods,
 'cascade_results':cascade_results,
 'tree_cv_results':tree_results,
 'best_tree':bt_name,
 'best_tree_rule_text':tree_text,
 'significance':sig,
 'multimethod_only_comparison':multi_cmp,
}

# ---------- McNemar + mechanism decomposition for the best cascade vs consensus
from math import comb
def mcnemar_and_decomp(fn):
    nb=nc=0  # best-wins / consensus-wins discordant pairs
    for r in rows:
        bs=fn(r)
        bok=r['candidates'].get(bs,{}).get('correct',0) if (bs and bs in r['candidates']) else 0
        cok=r['candidates'].get('consensus_medoid',{}).get('correct',0)
        if bok and not cok: nb+=1
        if cok and not bok: nc+=1
    nn=nb+nc; k=min(nb,nc)
    p=min(1.0, sum(comb(nn,i) for i in range(0,k+1))/2**nn*2) if nn>0 else 1.0
    return {'best_wins':nb,'consensus_wins':nc,'n_discordant':nn,'mcnemar_exact_p2sided':round(p,4)}
mech=mcnemar_and_decomp(best_casc_fn)
# fallback-branch behaviour (ntop<2): consensus vs gmaxplddt
fb=[r for r in rows if r.get('n_methods_in_top',0)<2]
def frac_correct(rs,src):
    if not rs: return None
    return round(100*sum(r['candidates'].get(src,{}).get('correct',0) for r in rs)/len(rs),1)
mech['fallback_branch_n']=len(fb)
mech['fallback_consensus_acc']=frac_correct(fb,'consensus_medoid')
mech['fallback_gmaxplddt_acc']=frac_correct(fb,'global_maxplddt')
mech['agree_branch_n']=len(rows)-len(fb)
summary['best_cascade_mechanism']=mech
json.dump(summary, open(OUT,'w'), indent=2, default=str)

# pretty print
print("\n================ RESULTS ================")
print(f"pool-oracle systems: {len(rows)}  (novel={int(novel_mask.sum())})  methods={AVAIL}")
print("\nBASELINES:")
print("  consensus       all", fmt(cascade_results['consensus(baseline)']['all']),
      " novel", fmt(cascade_results['consensus(baseline)']['novel']))
print("  global_maxplddt all", fmt(cascade_results['global_maxplddt(baseline)']['all']),
      " novel", fmt(cascade_results['global_maxplddt(baseline)']['novel']))
print("\nCASCADES (all / novel):")
ranked=sorted(cascade_results.items(), key=lambda kv: kv[1]['all'][0] or 0, reverse=True)
for name,res in ranked:
    print(f"  {res['all'][0]:5.1f}% / {str(res['novel'][0]):>5}%  (novel n={res['novel'][1]:3d})  {name}")
print("\nLEARNED TREE (grouped-by-protein CV):")
for k,v in sorted(tree_results.items(), key=lambda kv:kv[1]['all_cv'][0] or 0, reverse=True):
    print(f"  all {v['all_cv'][0]}%  novel {v['novel_cv'][0]}% (n={v['novel_cv'][1]})  {k}")
print("\nBEST TREE RULE:\n",tree_text)
print("=== SIGNIFICANCE (cluster bootstrap by protein) best cascade:",best_casc_name,"===")
for k,v in sig.items():
    if isinstance(v,dict) and 'obs_diff' in v:
        print(f"  {k}: diff={v['obs_diff']} CI95={v['ci95']} p(no improvement)={v['p_no_improve']}")
    else:
        print(f"  {k}: {v}")
print("=== MULTI-METHOD-ONLY systems ===")
for k,v in multi_cmp.items(): print(f"  {k}: {v}")
print("=== BEST CASCADE MECHANISM (McNemar paired vs consensus) ===")
print(f"  McNemar: best-wins={mech['best_wins']} consensus-wins={mech['consensus_wins']} (discordant={mech['n_discordant']}) exact p={mech['mcnemar_exact_p2sided']}")
print(f"  AGREE branch (n_methods_in_top>=2, n={mech['agree_branch_n']}): use consensus medoid")
print(f"  FALLBACK branch (top cluster single-method, n={mech['fallback_branch_n']}): consensus={mech['fallback_consensus_acc']}% vs gmaxplddt={mech['fallback_gmaxplddt_acc']}% -> use gmaxplddt")
print("wrote",OUT, file=sys.stderr)
