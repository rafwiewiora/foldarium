#!/usr/bin/env python3
"""Forensic classification of the 74 novel RnP items that did NOT reach quiz plan v2.

Replicates build_rnp_quiz_plan_v2.py per-system logic EXACTLY over only the novel keys,
recording the branch each dropped item hits. Read-only; writes only this scratch + a JSON report.
"""
import json, pickle, csv, sys, os, re
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

def norm(x): return x.replace('.','_')

KNOWN2={'CL','BR','NA','MG','ZN','FE','CA','MN','SE','SI','LI','CU','CO','NI','PT','AS','SB','SN','HG','CD','AG','AU','PD','BA','SR','CS','RB','TE','GE','GA','AL','TI','CR','MO','RU','RH','IR','OS','RE','HF','TA','ZR','NB','PB','BI','TL','IN','BE','SC'}
def elem_of(name):
    s=re.match(r'([A-Za-z]+)',name).group(1).upper()
    if len(s)>=2 and s[:2] in KNOWN2: return s[:2]
    return s[0]

# ---- build the novel universe (annotation key -> (target,instchain) tuple used by build)
# build uses target=system_id, instchain=ligand_instance_chain
nov={}; ccd={}
novel_universe=set()   # keys
key2ti={}              # normkey -> (system_id, ligand_instance_chain)
for r in csv.DictReader(open(ANNOT)):
    sid=r['system_id']; lic=r['ligand_instance_chain']
    ccd[(sid,lic)]=r['ligand_ccd_code']
    v=r.get('sucos_shape_pocket_qcov','')
    try:
        v=float(v)
    except:
        continue
    nov[(sid,lic)]=(v<25.0)
    if v<25.0:
        k=norm(sid)+'__'+norm(lic)
        novel_universe.add(k)
        key2ti[k]=(sid,lic)
print(f"novel universe (sucos_shape_pocket_qcov<25): {len(novel_universe)}", file=sys.stderr)

# ---- survivors (plan novel)
plan=pickle.load(open(PLAN,'rb'))
plan_novel_keys=set()
for p in plan:
    if p.get('novel')==True:
        plan_novel_keys.add(norm(p['target'])+'__'+norm(p['instchain']))
print(f"plan novel keys: {len(plan_novel_keys)}", file=sys.stderr)

dropped=sorted(novel_universe - plan_novel_keys)
print(f"dropped = novel - plan = {len(dropped)}", file=sys.stderr)

# ---- table
def key(r): return (r['target'],r['instchain'],r['method'],r['seed'],r['sample'],r['pred_chain'])
tbl={}
for r in csv.DictReader(open(TABLE)):
    rr={'target':r['target'],'instchain':r['instchain'],'method':r['method'],
        'seed':r['seed'],'sample':r['sample'],'pred_chain':r['pred_chain'],
        'rmsd':float(r['rmsd']),
        'plddt':float(r['ligand_plddt']) if r['ligand_plddt'] not in('','nan') else None,
        'ranking':float(r['ranking_score']) if r['ranking_score'] not in('','nan') else None}
    tbl[key(r)]=rr

# ---- index (path -> entries) but only keep entries for our dropped (target,instchain)
dropped_ti=set(key2ti[k] for k in dropped)
idx=json.load(open(IDX))

# Which dropped (t,i) appear in the index at ALL (any entry)?
ti_in_index=set()
for path,entries in idx.items():
    for e in entries:
        ti=(e['target'],e['instchain'])
        if ti in dropped_ti:
            ti_in_index.add(ti)

print(f"dropped ti present in cif_index: {len(ti_in_index)} / {len(dropped_ti)}", file=sys.stderr)

print("loading cache...", file=sys.stderr)
cache=pickle.load(open(CACHE,'rb'))
print(f"cache paths: {len(cache)}", file=sys.stderr)

# ---- build per-system poses (only for dropped ti), same as build
sys2poses=defaultdict(list)
for path,entries in idx.items():
    cc=cache.get(path)
    if cc is None: continue
    for e in entries:
        ti=(e['target'],e['instchain'])
        if ti not in dropped_ti: continue
        pc=e['pred_chain']; lig=cc['lig'].get(pc)
        if not lig: continue
        k=(e['target'],e['instchain'],e['method'],e['seed'],e['sample'],pc)
        rr=tbl.get(k)
        if rr is None: continue
        sys2poses[ti].append({
            'path':path,'method':e['method'],'seed':e['seed'],'sample':e['sample'],
            'pred_chain':pc,'rmsd':rr['rmsd'],'plddt':rr['plddt'],'ranking':rr['ranking'],
            'lig':lig,'ca':cc['ca']})

# ---- geometry (verbatim)
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

# ---- classify each dropped key
classification={}   # normkey -> (bucket, detail dict)
for k in dropped:
    ti=key2ti[k]
    # bucket 1: no predictions/coords in index at all OR no poses survived cache/table join
    poses=sys2poses.get(ti,[])
    if ti not in ti_in_index:
        classification[k]=('no-predictions/no-coords',{'reason':'not in cif_index at all'})
        continue
    if not poses:
        # present in index but no usable coords (no lig in cache OR no matching table row)
        classification[k]=('no-predictions/no-coords',{'reason':'in index but no coords/table poses'})
        continue
    # reference selection (verbatim)
    af3=[p for p in poses if p['method']=='af3']; ref=None
    if af3:
        af3s=sorted(af3,key=lambda p:(int(p['seed']) if p['seed'].lstrip('-').isdigit() else 0,int(p['sample'])))
        for p in af3s:
            if p['sample']=='0': ref=p;break
        if ref is None: ref=af3s[0]
    if ref is None:
        ref=sorted(poses,key=lambda p:(p['method'],p['seed'],p['sample']))[0]
    refpc=pocket_chain_for(ref)
    if refpc is None:
        classification[k]=('no-reference',{'reason':'no_ref_pocket','n_pose':len(poses)})
        continue
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
            transformed.append({'coords':(elems,names,xyz),'pose':p,'nmatch':len(ref_resids)})
            continue
        res=superpose_ligand(p, ref_resids, ref_ca_xyz)
        if res is None: transformed.append(None); continue
        out,nm=res
        transformed.append({'coords':out,'pose':p,'nmatch':nm})
    valid=[t for t in transformed if t is not None]
    if len(valid)<2:
        classification[k]=('too-few-aligned-poses',{'n_pose':len(poses),'n_valid':len(valid)})
        continue
    coords=[t['coords'] for t in valid]
    labels,ncl=cluster(coords)
    cmembers=defaultdict(list)
    for i,l in enumerate(labels): cmembers[l].append(i)
    centroids={}
    for l,mem in cmembers.items():
        cen=np.mean([coords[i][2].mean(0) for i in mem],axis=0)
        centroids[l]=cen
    cl=list(centroids.keys())
    maxspread=0.0
    for a in range(len(cl)):
        for b in range(a+1,len(cl)):
            d=float(np.linalg.norm(centroids[cl[a]]-centroids[cl[b]]))
            if d>maxspread: maxspread=d
    single_pocket=(maxspread<SINGLE_POCKET_SPREAD)
    if not single_pocket:
        classification[k]=('multi-pocket',{'maxspread':round(maxspread,2),'n_valid':len(valid),'n_clusters':ncl})
        continue
    # If we reach here, this item WOULD have been kept -> unexplained
    n_correct=sum(1 for t in valid if t['pose']['rmsd']<2)
    classification[k]=('other/unexplained',{'maxspread':round(maxspread,2),'n_valid':len(valid),
                        'n_clusters':ncl,'n_correct':n_correct})

# ---- report
buckets=defaultdict(list)
for k,(b,d) in classification.items():
    buckets[b].append((k,d))

order=['no-predictions/no-coords','no-reference','too-few-aligned-poses','multi-pocket','other/unexplained']
print("\n================ CLASSIFICATION OF THE 74 DROPPED NOVEL ITEMS ================")
print(f"novel universe: {len(novel_universe)}  |  plan novel: {len(plan_novel_keys)}  |  dropped: {len(dropped)}")
total=0
for b in order:
    items=buckets.get(b,[])
    total+=len(items)
    print(f"\n[{b}] : {len(items)}")
    for k,d in items[:6]:
        print(f"    {k}   {d}")
print(f"\nSUM = {total}  (should be {len(dropped)})")

json.dump({
    'novel_universe':len(novel_universe),
    'plan_novel':len(plan_novel_keys),
    'dropped':len(dropped),
    'buckets':{b:[k for k,_ in buckets.get(b,[])] for b in order},
    'detail':{k:{'bucket':bb,'detail':dd} for k,(bb,dd) in classification.items()},
}, open(f"{RNP}/dropped_74_classification.json",'w'), indent=2)
print("\nwrote",f"{RNP}/dropped_74_classification.json", file=sys.stderr)
