#!/usr/bin/env python3
"""Stage A (planning) for the Runs-n-Poses pose-triage quiz.

REUSES consensus.py's alignment + clustering VERBATIM (pocket-CA Kabsch onto an
af3 lowest-seed sample-0 reference, greedy ligand-RMSD<2 clustering, atom-index
matched across methods).

For EVERY system (not just gameable):
  - pool all methods' poses, transform ligands into the ref frame
  - greedy cluster at 2.0 A
  - SINGLE-POCKET filter: max pairwise distance between cluster CENTROIDS < 8.0 A
    (multi-pocket systems dropped, counted separately)
  - per-pose: correct = table rmsd<2 ; plddt = ligand_plddt
  - bucket: 'game-able' (>=1 correct AND >=1 wrong), 'all-wrong' (0 correct),
    skip 'all-correct-trivial'
  - record the reference tar PATH (so Stage B can stream just those CIFs for protein.pdb)
  - record transformed ligand coords for every pose (so Stage B doesn't realign)

Writes a plan pickle: list of system dicts. Resumable (skips if plan exists & RESUME=1).
"""
import json, pickle, csv, sys, time, os, re
from collections import defaultdict
import numpy as np

RNP="/Users/rafalwiewiora/rnp_data"
CACHE=os.environ.get("CACHE","/tmp/coords_cache_v2.pkl")
IDX="/Users/rafalwiewiora/rnp_data/cif_index_v2.json"
TABLE=f"{RNP}/xmethod_plddt_table_v2.csv"
ANNOT="/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/sucos/rnp_annotations.csv"
OUT=f"{RNP}/rnp_quiz_plan_v2.pkl"
CLUSTER_THRESH=2.0
POCKET_CA_R=12.0
SINGLE_POCKET_SPREAD=8.0
TOPK_PER_METHOD=int(os.environ.get("TOPK","3"))  # realistic ensemble: top-K ranking_score poses per method

KNOWN2={'CL','BR','NA','MG','ZN','FE','CA','MN','SE','SI','LI','CU','CO','NI','PT','AS','SB','SN','HG','CD','AG','AU','PD','BA','SR','CS','RB','TE','GE','GA','AL','TI','CR','MO','RU','RH','IR','OS','RE','HF','TA','ZR','NB','PB','BI','TL','IN','BE','SC'}
def elem_of(name):
    s=re.match(r'([A-Za-z]+)',name).group(1).upper()
    if len(s)>=2 and s[:2] in KNOWN2: return s[:2]
    return s[0]

# ---- table
def key(r): return (r['target'],r['instchain'],r['method'],r['seed'],r['sample'],r['pred_chain'])
tbl={}; sys2rows=defaultdict(list)
for r in csv.DictReader(open(TABLE)):
    rr={'target':r['target'],'instchain':r['instchain'],'method':r['method'],
        'seed':r['seed'],'sample':r['sample'],'pred_chain':r['pred_chain'],
        'rmsd':float(r['rmsd']),
        'plddt':float(r['ligand_plddt']) if r['ligand_plddt'] not in('','nan') else None,
        'ranking':float(r['ranking_score']) if r['ranking_score'] not in('','nan') else None}
    tbl[key(r)]=rr
    sys2rows[(r['target'],r['instchain'])].append(rr)

# ---- novelty + ccd
nov={}; ccd={}
for r in csv.DictReader(open(ANNOT)):
    sid=r['system_id']; lic=r['ligand_instance_chain']
    ccd[(sid,lic)]=r['ligand_ccd_code']
    v=r.get('sucos_shape_pocket_qcov','')
    try: v=float(v); nov[(sid,lic)]=(v<25.0)
    except: pass
def is_novel(t,i): return nov.get((t,i))

idx=json.load(open(IDX))
print("loading cache...",file=sys.stderr)
cache=pickle.load(open(CACHE,'rb'))
print(f"cache paths: {len(cache)}",file=sys.stderr)

# ---- per-system poses (with tar path so Stage B can find the ref CIF)
sys2poses=defaultdict(list)
for path,entries in idx.items():
    cc=cache.get(path)
    if cc is None: continue
    for e in entries:
        pc=e['pred_chain']; lig=cc['lig'].get(pc)
        if not lig: continue
        k=(e['target'],e['instchain'],e['method'],e['seed'],e['sample'],pc)
        rr=tbl.get(k)
        if rr is None: continue
        sys2poses[(e['target'],e['instchain'])].append({
            'path':path,'method':e['method'],'seed':e['seed'],'sample':e['sample'],
            'pred_chain':pc,'rmsd':rr['rmsd'],'plddt':rr['plddt'],'ranking':rr['ranking'],
            'lig':lig,'ca':cc['ca']})

# ---- geometry (verbatim from consensus.py)
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

# ---- sub-pool: keep only the top-K ranking_score poses per method (realistic 'review the top picks' ensemble).
#      ranking_score may be missing -> treat as -inf so genuinely-ranked poses win.
for s,poses in list(sys2poses.items()):
    by=defaultdict(list)
    for p in poses: by[p['method']].append(p)
    kept=[]
    for m,v in by.items():
        v.sort(key=lambda p:(p['ranking'] if p['ranking'] is not None else -1e9), reverse=True)
        kept+=v[:TOPK_PER_METHOD]
    sys2poses[s]=kept

systems=sorted(sys2poses.keys())
print(f"systems with coords: {len(systems)} (top-{TOPK_PER_METHOD} ranked per method)",file=sys.stderr)

plan=[]
counts=defaultdict(int)
t0=time.time()
for gi,s in enumerate(systems):
    target,instchain=s; poses=sys2poses[s]
    # reference: af3 lowest seed sample-0
    af3=[p for p in poses if p['method']=='af3']; ref=None
    if af3:
        af3s=sorted(af3,key=lambda p:(int(p['seed']) if p['seed'].lstrip('-').isdigit() else 0,int(p['sample'])))
        for p in af3s:
            if p['sample']=='0': ref=p;break
        if ref is None: ref=af3s[0]
    if ref is None:
        ref=sorted(poses,key=lambda p:(p['method'],p['seed'],p['sample']))[0]
    refpc=pocket_chain_for(ref)
    if refpc is None: counts['no_ref_pocket']+=1; continue
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
    n_failed=sum(1 for t in transformed if t is None)
    if len(valid)<2: counts['too_few_aligned']+=1; continue
    coords=[t['coords'] for t in valid]
    labels,ncl=cluster(coords)
    # cluster centroids
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
    n_correct=sum(1 for t in valid if t['pose']['rmsd']<2)
    n_wrong=len(valid)-n_correct
    if not single_pocket:
        counts['multi_pocket_dropped']+=1; continue
    # v2 amendment: KEEP the trivial / all-correct ensembles (positive control for Hard
    # detect-game). Tag with build_bucket='all-correct' instead of skipping.
    if n_correct==len(valid):
        bucket='all-correct'
    elif n_correct>=1 and n_wrong>=1:
        bucket='game-able'
    else:
        bucket='all-wrong'
    counts[bucket]+=1
    # build choice records (rep = medoid of cluster, lowest within-cluster rmsd-sum)
    def medoid(mem):
        if len(mem)==1: return mem[0]
        best=None;bs=1e18
        for i in mem:
            ssum=0.0
            for j in mem:
                r=lrmsd(coords[i],coords[j])
                if r is not None: ssum+=r
            if ssum<bs: bs=ssum;best=i
        return best
    rep_idx={l:medoid(mem) for l,mem in cmembers.items()}
    choices=[]
    for i,t in enumerate(valid):
        p=t['pose']; el,nm,xyz=t['coords']
        choices.append({
            'cluster':labels[i],'is_rep':(i==rep_idx[labels[i]]),
            'rmsd':round(p['rmsd'],3),'correct':bool(p['rmsd']<2),
            'plddt':round(p['plddt'],3) if p['plddt'] is not None else None,
            'ranking':p['ranking'],'_method':p['method'],
            'elems':list(el),'names':list(nm),'xyz':xyz.tolist(),
        })
    plan.append({
        'target':target,'instchain':instchain,
        'ccd':ccd.get((target,instchain)),
        'novel':is_novel(target,instchain),
        'bucket':bucket,'build_bucket':bucket,'single_pocket':True,'has_correct':bool(n_correct>0),
        'n_clusters':ncl,'n_correct':n_correct,'n_pose':len(valid),'n_failed':n_failed,
        'n_methods':len(set(p['method'] for p in poses)),
        'maxspread':round(maxspread,2),
        'ref_path':ref['path'],'ref_pred_chain':ref['pred_chain'],
        'ref_pocket_resids':ref_resids,        # for protein.pdb pocket selection
        'choices':choices,
    })
    if (gi+1)%200==0:
        print(f"[{int(time.time()-t0)}s] {gi+1}/{len(systems)} game={counts['game-able']} allwrong={counts['all-wrong']} allcorrect={counts['all-correct']} multi={counts['multi_pocket_dropped']}",file=sys.stderr)

pickle.dump(plan, open(OUT,'wb'), protocol=4)
counts['total_systems']=len(systems); counts['kept']=len(plan)
print(json.dumps(dict(counts),indent=2))
print("wrote",OUT,file=sys.stderr)
