#!/usr/bin/env python3
"""Single vs multi-pocket bias analysis for the RnP pose-triage quiz.

Read-only. Reuses build_rnp_quiz_plan_v2.py geometry VERBATIM to classify EVERY
(target,instchain) system as single/multi-pocket (spread<8), then computes
oracle / AF3-top1 / any-method-top1 success from the FULL pose table.

Classification pose set = top-3 ranking_score per method (matches the real build filter).
Oracle / top-1 metrics = over ALL poses in the table (all methods, all seeds/samples).
"""
import json, pickle, csv, sys, os, re, math
from collections import defaultdict
import numpy as np

RNP="/Users/rafalwiewiora/rnp_data"
CACHE="/tmp/coords_cache_v2.pkl"
IDX=f"{RNP}/cif_index_v2.json"
TABLE=f"{RNP}/xmethod_plddt_table_v2.csv"
ANNOT="/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/sucos/rnp_annotations.csv"
PLAN=f"{RNP}/rnp_quiz_plan_v2.pkl"
CLUSTER_THRESH=2.0
POCKET_CA_R=12.0
SINGLE_POCKET_SPREAD=8.0
TOPK_PER_METHOD=3
RMSD_OK=2.0

def norm(x): return x.replace('.','_')

KNOWN2={'CL','BR','NA','MG','ZN','FE','CA','MN','SE','SI','LI','CU','CO','NI','PT','AS','SB','SN','HG','CD','AG','AU','PD','BA','SR','CS','RB','TE','GE','GA','AL','TI','CR','MO','RU','RH','IR','OS','RE','HF','TA','ZR','NB','PB','BI','TL','IN','BE','SC'}
def elem_of(name):
    s=re.match(r'([A-Za-z]+)',name).group(1).upper()
    if len(s)>=2 and s[:2] in KNOWN2: return s[:2]
    return s[0]

# ---- table (keep ALL poses; also keyed for cache join)
def key(r): return (r['target'],r['instchain'],r['method'],r['seed'],r['sample'],r['pred_chain'])
tbl={}
sys_allrows=defaultdict(list)  # (target,instchain) -> ALL table rows (for oracle/top1)
for r in csv.DictReader(open(TABLE)):
    rr={'target':r['target'],'instchain':r['instchain'],'method':r['method'],
        'seed':r['seed'],'sample':r['sample'],'pred_chain':r['pred_chain'],
        'rmsd':float(r['rmsd']),
        'plddt':float(r['ligand_plddt']) if r['ligand_plddt'] not in('','nan') else None,
        'ranking':float(r['ranking_score']) if r['ranking_score'] not in('','nan') else None}
    tbl[key(r)]=rr
    sys_allrows[(r['target'],r['instchain'])].append(rr)
print(f"table systems: {len(sys_allrows)}",file=sys.stderr)

# ---- novelty (real values only; blanks = no-analog)
nov={}; ccd={}
for r in csv.DictReader(open(ANNOT)):
    sid=r['system_id']; lic=r['ligand_instance_chain']
    ccd[(sid,lic)]=r['ligand_ccd_code']
    v=r.get('sucos_shape_pocket_qcov','')
    try:
        v=float(v); nov[(sid,lic)]=(v<25.0)
    except:
        pass  # blank -> no-analog, not in nov dict
def novel_of(t,i): return nov.get((t,i))  # True / False / None(no-analog)

# ---- plan: reuse stored maxspread for KEPT systems (avoids recompute)
plan=pickle.load(open(PLAN,'rb'))
plan_spread={}  # (target,instchain) -> maxspread (all single_pocket=True)
for p in plan:
    plan_spread[(p['target'],p['instchain'])]=p['maxspread']
print(f"plan (single-pocket kept) systems: {len(plan_spread)}",file=sys.stderr)

# ---- coords cache + index for systems NOT in plan
idx=json.load(open(IDX))
all_ti=set(sys_allrows.keys())
need_ti=all_ti - set(plan_spread.keys())
print(f"need geometry recompute for: {len(need_ti)}",file=sys.stderr)

print("loading cache...",file=sys.stderr)
cache=pickle.load(open(CACHE,'rb'))
print(f"cache paths: {len(cache)}",file=sys.stderr)

sys2poses=defaultdict(list)
for path,entries in idx.items():
    cc=cache.get(path)
    if cc is None: continue
    for e in entries:
        ti=(e['target'],e['instchain'])
        if ti not in need_ti: continue
        pc=e['pred_chain']; lig=cc['lig'].get(pc)
        if not lig: continue
        k=(e['target'],e['instchain'],e['method'],e['seed'],e['sample'],pc)
        rr=tbl.get(k)
        if rr is None: continue
        sys2poses[ti].append({
            'path':path,'method':e['method'],'seed':e['seed'],'sample':e['sample'],
            'pred_chain':pc,'rmsd':rr['rmsd'],'plddt':rr['plddt'],'ranking':rr['ranking'],
            'lig':lig,'ca':cc['ca']})

# ---- geometry (verbatim from build)
def kabsch(P,Q):
    Pc=P.mean(0); Qc=Q.mean(0); A=(P-Pc).T@(Q-Qc)
    V,S,Wt=np.linalg.svd(A); d=np.sign(np.linalg.det(Wt.T@V.T))
    D=np.diag([1,1,d]); R=(Wt.T@D@V.T); t=Qc-R@Pc; return R,t
def pocket_chain_for(pose):
    ligxyz=np.array([xyz for _,xyz in pose['lig']]); best=None;bestd=1e9
    for ch,resd in pose['ca'].items():
        if not resd: continue
        caxyz=np.array(list(resd.values()))
        d=np.min(np.linalg.norm(caxyz[:,None,:]-ligxyz[None,:,:],axis=2))
        if d<bestd: bestd=d;best=ch
    return best
def superpose_ligand(pose, ref_resids, ref_ca_xyz):
    pc=pocket_chain_for(pose)
    if pc is None: return None
    resd=pose['ca'].get(pc,{}); matched_p=[];matched_q=[]
    for rid in ref_resids:
        if rid in resd: matched_p.append(resd[rid]); matched_q.append(ref_ca_xyz[rid])
    if len(matched_p)<4:
        bestn=0;bestpc=None
        for ch,rd in pose['ca'].items():
            n=sum(1 for rid in ref_resids if rid in rd)
            if n>bestn: bestn=n;bestpc=ch
        if bestpc is None or bestn<4: return None
        rd=pose['ca'][bestpc]; matched_p=[];matched_q=[]
        for rid in ref_resids:
            if rid in rd: matched_p.append(rd[rid]); matched_q.append(ref_ca_xyz[rid])
    P=np.array(matched_p); Q=np.array(matched_q); R,t=kabsch(P,Q)
    L=np.array([xyz for _,xyz in pose['lig']]); Lt=(R@L.T).T+t
    elems=tuple(elem_of(nm) for nm,_ in pose['lig'])
    names=tuple(nm for nm,_ in pose['lig'])
    return (elems,names,Lt),len(matched_p)
def lrmsd(a,b):
    ea,na,xa=a; eb,nb,xb=b
    if ea!=eb: return None
    d=xa-xb; return float(np.sqrt((d*d).sum(1).mean()))
def cluster(coords):
    n=len(coords); labels=[-1]*n; cid=0
    for i in range(n):
        if labels[i]>=0: continue
        labels[i]=cid
        for j in range(i+1,n):
            if labels[j]<0:
                r=lrmsd(coords[i],coords[j])
                if r is not None and r<CLUSTER_THRESH: labels[j]=cid
        cid+=1
    return labels,cid

# ---- top-K per method (verbatim)
for s,poses in list(sys2poses.items()):
    by=defaultdict(list)
    for p in poses: by[p['method']].append(p)
    kept=[]
    for m,v in by.items():
        v.sort(key=lambda p:(p['ranking'] if p['ranking'] is not None else -1e9), reverse=True)
        kept+=v[:TOPK_PER_METHOD]
    sys2poses[s]=kept

# ---- compute spread for each need_ti system
pocket_label={}   # (t,i) -> 'single' / 'multi' / None(unclassifiable) ; store maxspread too
spread_val={}
unclass_reason=defaultdict(int)
todo=sorted(sys2poses.keys())
for gi,s in enumerate(todo):
    poses=sys2poses[s]
    if len(poses)<2:
        unclass_reason['too_few_poses']+=1; continue
    af3=[p for p in poses if p['method']=='af3']; ref=None
    if af3:
        af3s=sorted(af3,key=lambda p:(int(p['seed']) if p['seed'].lstrip('-').isdigit() else 0,int(p['sample'])))
        for p in af3s:
            if p['sample']=='0': ref=p;break
        if ref is None: ref=af3s[0]
    if ref is None:
        ref=sorted(poses,key=lambda p:(p['method'],p['seed'],p['sample']))[0]
    refpc=pocket_chain_for(ref)
    if refpc is None: unclass_reason['no_ref_pocket']+=1; continue
    ref_lig_xyz=np.array([xyz for _,xyz in ref['lig']]); ref_resd=ref['ca'][refpc]
    ref_ca_xyz={}; ref_resids=[]
    for rid,xyz in ref_resd.items():
        ax=np.array(xyz)
        if np.min(np.linalg.norm(ref_lig_xyz-ax,axis=1))<POCKET_CA_R:
            ref_ca_xyz[rid]=xyz; ref_resids.append(rid)
    if len(ref_resids)<4:
        for rid,xyz in ref_resd.items(): ref_ca_xyz[rid]=xyz; ref_resids.append(rid)
    transformed=[]
    for p in poses:
        if p is ref:
            elems=tuple(elem_of(nm) for nm,_ in p['lig'])
            names=tuple(nm for nm,_ in p['lig'])
            xyz=np.array([c for _,c in p['lig']])
            transformed.append({'coords':(elems,names,xyz)}); continue
        res=superpose_ligand(p, ref_resids, ref_ca_xyz)
        if res is None: transformed.append(None); continue
        out,nm=res
        transformed.append({'coords':out})
    valid=[t for t in transformed if t is not None]
    if len(valid)<2: unclass_reason['too_few_aligned']+=1; continue
    coords=[t['coords'] for t in valid]
    labels,ncl=cluster(coords)
    cmembers=defaultdict(list)
    for i,l in enumerate(labels): cmembers[l].append(i)
    centroids={}
    for l,mem in cmembers.items():
        centroids[l]=np.mean([coords[i][2].mean(0) for i in mem],axis=0)
    cl=list(centroids.keys()); maxspread=0.0
    for a in range(len(cl)):
        for b in range(a+1,len(cl)):
            d=float(np.linalg.norm(centroids[cl[a]]-centroids[cl[b]]))
            if d>maxspread: maxspread=d
    spread_val[s]=maxspread
    pocket_label[s]='single' if maxspread<SINGLE_POCKET_SPREAD else 'multi'
    if (gi+1)%200==0:
        print(f"  classified {gi+1}/{len(todo)}",file=sys.stderr)

# plan systems are all single
for s,ms in plan_spread.items():
    pocket_label[s]='single'; spread_val[s]=ms

print(f"classified single/multi: {len(pocket_label)} / {len(all_ti)}",file=sys.stderr)
print(f"unclassifiable: {dict(unclass_reason)}",file=sys.stderr)

# ---- oracle / top-1 from FULL pose table (all poses)
def metrics_for(rows):
    """rows = ALL table rows for a (t,i). Returns oracle, af3_top1, any_top1."""
    # oracle: any pose rmsd<2
    oracle = any(r['rmsd']<RMSD_OK for r in rows)
    # per-method top-1: highest ranking_score pose; missing ranking -> -inf
    by=defaultdict(list)
    for r in rows: by[r['method']].append(r)
    method_top1={}
    for m,v in by.items():
        ranked=[r for r in v if r['ranking'] is not None]
        pool=ranked if ranked else v  # if no ranking at all, fall back to all (can't rank)
        # pick max ranking; ties -> first (stable)
        best=max(pool,key=lambda r:(r['ranking'] if r['ranking'] is not None else -1e18))
        method_top1[m]=(best['rmsd']<RMSD_OK, best['ranking'] is not None)
    af3_top1 = method_top1.get('af3',(None,None))[0]
    any_top1 = any(v[0] for v in method_top1.values()) if method_top1 else None
    return oracle, af3_top1, any_top1, method_top1

records=[]
for s in sorted(all_ti):
    lab=pocket_label.get(s)
    oracle,af3t1,anyt1,mt1 = metrics_for(sys_allrows[s])
    records.append({'target':s[0],'instchain':s[1],'pocket':lab,
                    'spread':spread_val.get(s),'novel':novel_of(*s),
                    'oracle':oracle,'af3_top1':af3t1,'any_top1':anyt1,
                    'has_af3':'af3' in mt1})

# ---- crosstab helper
def pct(vals):
    vals=[v for v in vals if v is not None]
    if not vals: return (None,0)
    return (round(100*sum(vals)/len(vals),1), len(vals))

def two_prop_z(k1,n1,k2,n2):
    if n1==0 or n2==0: return None,None
    p1=k1/n1; p2=k2/n2; p=(k1+k2)/(n1+n2)
    se=math.sqrt(p*(1-p)*(1/n1+1/n2))
    if se==0: return 0.0,1.0
    z=(p1-p2)/se
    # two-sided p via erfc
    pval=math.erfc(abs(z)/math.sqrt(2))
    return round(z,3),round(pval,4)

def report(recs,title):
    print(f"\n===== {title} (n={len(recs)}) =====")
    for grp in ['single','multi']:
        sub=[r for r in recs if r['pocket']==grp]
        o=pct([r['oracle'] for r in sub])
        a=pct([r['af3_top1'] for r in sub if r['has_af3']])
        y=pct([r['any_top1'] for r in sub])
        print(f"  {grp:6s} n={len(sub):4d} | oracle {str(o[0]):>5}% (n={o[1]:4d}) | "
              f"AF3-top1 {str(a[0]):>5}% (n={a[1]:4d}) | any-top1 {str(y[0]):>5}% (n={y[1]:4d})")
    # sig tests single vs multi
    sg=[r for r in recs if r['pocket']=='single']; mu=[r for r in recs if r['pocket']=='multi']
    for metric,mask in [('oracle',lambda r:True),('af3_top1',lambda r:r['has_af3']),('any_top1',lambda r:True)]:
        sv=[r[metric] for r in sg if mask(r) and r[metric] is not None]
        mv=[r[metric] for r in mu if mask(r) and r[metric] is not None]
        if sv and mv:
            z,p=two_prop_z(sum(sv),len(sv),sum(mv),len(mv))
            print(f"    {metric:9s}: single {round(100*sum(sv)/len(sv),1)}% (n{len(sv)}) vs "
                  f"multi {round(100*sum(mv)/len(mv),1)}% (n{len(mv)})  z={z} p={p}")

allrecs=[r for r in records if r['pocket'] in ('single','multi')]
report(allrecs,"ALL classified items")

# novel/familiar breakdown
novelrecs=[r for r in allrecs if r['novel']==True]
famrecs=[r for r in allrecs if r['novel']==False]
noanalog=[r for r in allrecs if r['novel'] is None]
report(novelrecs,"NOVEL only (sucos_shape_pocket_qcov<25)")
report(famrecs,"FAMILIAR only (>=25)")
print(f"\n[no-analog items (blank sucos): n={len(noanalog)}] "
      f"single={sum(1 for r in noanalog if r['pocket']=='single')} "
      f"multi={sum(1 for r in noanalog if r['pocket']=='multi')}")

# 4-way counts
print("\n===== 4-way counts (pocket x novelty) =====")
for grp in ['single','multi']:
    for nv,lab in [(True,'novel'),(False,'familiar'),(None,'no-analog')]:
        sub=[r for r in allrecs if r['pocket']==grp and r['novel']==nv]
        print(f"  {grp:6s} {lab:9s}: n={len(sub)}")

# unclassifiable
unc=[r for r in records if r['pocket'] is None]
print(f"\n[unclassifiable (no valid geometry): n={len(unc)}] reasons={dict(unclass_reason)}")

# save scratch
json.dump({'records':[{k:(v if not isinstance(v,np.generic) else float(v)) for k,v in r.items()} for r in records],
           'unclass_reason':dict(unclass_reason)},
          open(f"{RNP}/pocket_bias_records.json",'w'),indent=1,default=str)
print(f"\nwrote {RNP}/pocket_bias_records.json",file=sys.stderr)
