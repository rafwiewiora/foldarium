#!/usr/bin/env python3
"""Build BLIND per-system features + per-candidate-pose correctness labels for rule search.

Reuses consensus.py geometry: superpose each prediction's pocket-CA onto an af3 reference,
transform ligand heavy atoms, greedy ligand-RMSD<2 clustering.

Produces:
  - per-system feature rows (no ground truth used in features)
  - per-system candidate pose set with correctness labels (uses table rmsd for labels only)
Writes /Users/rafalwiewiora/rnp_data/features_table.pkl and features_table.csv
"""
import json, pickle, csv, sys, time, os, re
from collections import defaultdict
import numpy as np

KNOWN2={'CL','BR','NA','MG','ZN','FE','CA','MN','SE','SI','LI','CU','CO','NI','PT','AS','SB','SN','HG','CD','AG','AU','PD','BA','SR','CS','RB','TE','GE','GA','AL','TI','CR','MO','RU','RH','IR','OS','RE','HF','TA','ZR','NB','PB','BI','TL','IN','BE','SC'}
def elem_of(name):
    s=re.match(r'([A-Za-z]+)',name).group(1).upper()
    if len(s)>=2 and s[:2] in KNOWN2: return s[:2]
    return s[0]

CACHE=os.environ.get("CACHE","/tmp/coords_cache.pkl")
IDX="/tmp/cif_index.json"
TABLE="/Users/rafalwiewiora/rnp_data/xmethod_plddt_table.csv"
ANNOT="/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/sucos/rnp_annotations.csv"
OUTPKL="/Users/rafalwiewiora/rnp_data/features_table.pkl"
OUTCSV="/Users/rafalwiewiora/rnp_data/features_table.csv"
CLUSTER_THRESH=2.0
POCKET_CA_R=12.0
METHODS=['af3','boltz','chai','protenix']

# ---------- load table
def key(r): return (r['target'],r['instchain'],r['method'],r['seed'],r['sample'],r['pred_chain'])
tbl={}
sys2rows=defaultdict(list)
for r in csv.DictReader(open(TABLE)):
    rr={'target':r['target'],'instchain':r['instchain'],'method':r['method'],
        'seed':r['seed'],'sample':r['sample'],'pred_chain':r['pred_chain'],
        'rmsd':float(r['rmsd']),
        'plddt':float(r['ligand_plddt']) if r['ligand_plddt'] not in('','nan') else None,
        'rank':float(r['ranking_score']) if r['ranking_score'] not in('','nan') else None}
    tbl[key(r)]=rr
    sys2rows[(r['target'],r['instchain'])].append(rr)

# ---------- novelty
nov={}
for r in csv.DictReader(open(ANNOT)):
    sid=r['system_id']; lic=r['ligand_instance_chain']
    v=r.get('sucos_shape_pocket_qcov','')
    try: v=float(v)
    except: continue
    nov[(sid,lic)]=(v<25.0)
def is_novel(target,instchain):
    return nov.get((target,instchain), None)

# ---------- index + cache
idx=json.load(open(IDX))
print("loading cache...", file=sys.stderr)
cache=pickle.load(open(CACHE,'rb'))
print(f"cache paths: {len(cache)}", file=sys.stderr)

sys2poses=defaultdict(list)
for path,entries in idx.items():
    cc=cache.get(path)
    if cc is None: continue
    for e in entries:
        pc=e['pred_chain']
        lig=cc['lig'].get(pc)
        if not lig: continue
        k=(e['target'],e['instchain'],e['method'],e['seed'],e['sample'],pc)
        rr=tbl.get(k)
        if rr is None: continue
        sys2poses[(e['target'],e['instchain'])].append({
            'path':path,'method':e['method'],'seed':e['seed'],'sample':e['sample'],
            'pred_chain':pc,'rmsd':rr['rmsd'],'plddt':rr['plddt'],'rank':rr['rank'],
            'lig':lig,'ca':cc['ca']})

AVAIL_METHODS=sorted({p['method'] for poses in sys2poses.values() for p in poses})
print(f"methods available in coords: {AVAIL_METHODS}", file=sys.stderr)

# ---------- geometry (from consensus.py)
def kabsch(P,Q):
    Pc=P.mean(0); Qc=Q.mean(0)
    A=(P-Pc).T@(Q-Qc)
    V,S,Wt=np.linalg.svd(A)
    d=np.sign(np.linalg.det(Wt.T@V.T))
    D=np.diag([1,1,d])
    R=(Wt.T@D@V.T); t=Qc - R@Pc
    return R,t
def pocket_chain_for(pose):
    ligxyz=np.array([xyz for _,xyz in pose['lig']])
    best=None;bestd=1e9
    for ch,resd in pose['ca'].items():
        if not resd: continue
        caxyz=np.array(list(resd.values()))
        d=np.min(np.linalg.norm(caxyz[:,None,:]-ligxyz[None,:,:],axis=2))
        if d<bestd: bestd=d;best=ch
    return best
def superpose_ligand(pose, ref_resids, ref_ca_xyz):
    pc=pocket_chain_for(pose)
    if pc is None: return None
    resd=pose['ca'].get(pc,{})
    matched_p=[];matched_q=[]
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
    P=np.array(matched_p); Q=np.array(matched_q)
    R,t=kabsch(P,Q)
    L=np.array([xyz for _,xyz in pose['lig']])
    Lt=(R@L.T).T + t
    elems=tuple(elem_of(nm) for nm,_ in pose['lig'])
    return (elems, Lt)
def lrmsd(a,b):
    ea,xa=a; eb,xb=b
    if ea!=eb: return None
    d=xa-xb
    return float(np.sqrt((d*d).sum(1).mean()))
def greedy_cluster(coords):
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
def medoid(members,coords):
    if len(members)==1: return members[0]
    best=None;bestsum=1e18
    for i in members:
        s_=0.0
        for j in members:
            r=lrmsd(coords[i],coords[j])
            if r is not None: s_+=r
        if s_<bestsum: bestsum=s_;best=i
    return best

# ---------- main: per system build features + candidates
systems=sorted(sys2poses.keys())
rows_out=[]
t0=time.time()
done=0
for s in systems:
    poses=sys2poses[s]
    target,instchain=s
    if not any(p['rmsd']<2 for p in poses):
        continue  # not pool-oracle (no correct pose in coords)
    # reference: af3 lowest seed sample0
    af3=[p for p in poses if p['method']=='af3']
    ref=None
    if af3:
        af3s=sorted(af3,key=lambda p:(int(p['seed']) if p['seed'].lstrip('-').isdigit() else 0, int(p['sample'])))
        for p in af3s:
            if p['sample']=='0': ref=p;break
        if ref is None: ref=af3s[0]
    if ref is None:
        ref=sorted(poses,key=lambda p:(p['method'],p['seed'],p['sample']))[0]
    refpc=pocket_chain_for(ref)
    if refpc is None: continue
    ref_lig_xyz=np.array([xyz for _,xyz in ref['lig']])
    ref_resd=ref['ca'][refpc]
    ref_ca_xyz={}; ref_resids=[]
    for rid,xyz in ref_resd.items():
        ax=np.array(xyz)
        if np.min(np.linalg.norm(ref_lig_xyz-ax,axis=1))<POCKET_CA_R:
            ref_ca_xyz[rid]=xyz; ref_resids.append(rid)
    if len(ref_resids)<4:
        for rid,xyz in ref_resd.items(): ref_ca_xyz[rid]=xyz; ref_resids.append(rid)
    # transform all poses
    tp=[]  # aligned poses with coords
    for p in poses:
        if p is ref:
            elems=tuple(elem_of(nm) for nm,_ in p['lig'])
            xyz=np.array([c for _,c in p['lig']])
            tp.append({**p,'coords':(elems,xyz)})
            continue
        out=superpose_ligand(p, ref_resids, ref_ca_xyz)
        if out is None: continue
        tp.append({**p,'coords':out})
    if len(tp)<2: continue
    coords=[p['coords'] for p in tp]
    # ---- pooled clustering
    labels,ncl=greedy_cluster(coords)
    csize=defaultdict(list)
    for i,l in enumerate(labels): csize[l].append(i)
    def clu_plddt(members):
        ps=[tp[i]['plddt'] for i in members if tp[i]['plddt'] is not None]
        return sum(ps)/len(ps) if ps else -1
    order=sorted(csize.keys(), key=lambda c:(len(csize[c]), clu_plddt(csize[c])), reverse=True)
    topc=order[0]; topmembers=csize[topc]
    plurality=len(topmembers)/len(tp)
    methods_in_top=sorted({tp[i]['method'] for i in topmembers})
    n_methods_in_top=len(methods_in_top)
    consensus_medoid_idx=medoid(topmembers,coords)

    # ---- per-method self-consistency (cluster each method's own poses separately)
    permethod={}
    for M in METHODS:
        midx=[i for i,p in enumerate(tp) if p['method']==M]
        if not midx:
            permethod[M]=None; continue
        mcoords=[coords[i] for i in midx]
        mlabels,mncl=greedy_cluster(mcoords)
        # top pose by ranking_score
        mrank=[(tp[i]['rank'] if tp[i]['rank'] is not None else -1e9, i) for i in midx]
        toppose_idx=max(mrank)[1]
        # max plddt pose
        mpl=[(tp[i]['plddt'] if tp[i]['plddt'] is not None else -1e9, i) for i in midx]
        maxpl_idx=max(mpl)[1]
        maxplddt=max(tp[i]['plddt'] for i in midx if tp[i]['plddt'] is not None) if any(tp[i]['plddt'] is not None for i in midx) else None
        # cluster sizes within method
        mc=defaultdict(int)
        for l in mlabels: mc[l]+=1
        top_self_cluster=max(mc.values())
        permethod[M]={
            'n_poses':len(midx),'nclusters':mncl,'single':int(mncl==1),
            'maxplddt':maxplddt,'top_pose_idx':toppose_idx,'maxpl_idx':maxpl_idx,
            'top_self_cluster_frac':top_self_cluster/len(midx),
            'toppose_rmsd':tp[toppose_idx]['rmsd'],
            'toppose_plddt':tp[toppose_idx]['plddt'],
            'maxpl_rmsd':tp[maxpl_idx]['rmsd'],
        }
    # global max plddt pose
    allpl=[(tp[i]['plddt'] if tp[i]['plddt'] is not None else -1e9, i) for i in range(len(tp))]
    gmax_idx=max(allpl)[1]
    gmax_method=tp[gmax_idx]['method']
    gmax_rmsd=tp[gmax_idx]['rmsd']
    gmax_plddt=tp[gmax_idx]['plddt']

    # ---- CANDIDATE poses: dict source-> (idx, rmsd). correct = rmsd<2
    candidates={}
    for M in METHODS:
        pm=permethod[M]
        if pm is None: continue
        candidates[f'{M}_toprank']=(pm['top_pose_idx'], pm['toppose_rmsd'])
        candidates[f'{M}_maxplddt']=(pm['maxpl_idx'], pm['maxpl_rmsd'])
    candidates['consensus_medoid']=(consensus_medoid_idx, tp[consensus_medoid_idx]['rmsd'])
    candidates['global_maxplddt']=(gmax_idx, gmax_rmsd)

    feat={
        'system':target+'|'+instchain,'target':target,'instchain':instchain,
        'novel':is_novel(target,instchain),
        'n_pose':len(tp),'n_methods_present':len({p['method'] for p in tp}),
        'pooled_nclusters':ncl,'plurality':plurality,
        'n_methods_in_top':n_methods_in_top,
        'global_maxplddt':gmax_plddt,'global_maxplddt_method':gmax_method,
        'consensus_medoid_rmsd':tp[consensus_medoid_idx]['rmsd'],
        'consensus_medoid_plddt':tp[consensus_medoid_idx]['plddt'],
        'gmax_rmsd':gmax_rmsd,
    }
    for M in METHODS:
        pm=permethod[M]
        if pm is None:
            feat[f'{M}_present']=0; feat[f'{M}_single']=0
            feat[f'{M}_nclusters']=-1; feat[f'{M}_maxplddt']=None
            feat[f'{M}_top_self_frac']=None; feat[f'{M}_npose']=0
            feat[f'{M}_toprank_plddt']=None
        else:
            feat[f'{M}_present']=1; feat[f'{M}_single']=pm['single']
            feat[f'{M}_nclusters']=pm['nclusters']; feat[f'{M}_maxplddt']=pm['maxplddt']
            feat[f'{M}_top_self_frac']=pm['top_self_cluster_frac']; feat[f'{M}_npose']=pm['n_poses']
            feat[f'{M}_toprank_plddt']=pm['toppose_plddt']
    feat['candidates']={k:{'rmsd':v[1],'correct':int(v[1]<2)} for k,v in candidates.items()}
    rows_out.append(feat)
    done+=1
    if done%150==0:
        el=time.time()-t0
        print(f"[{int(el)}s] {done} systems ({el/done:.2f}s/sys)", file=sys.stderr); sys.stderr.flush()

print(f"built {len(rows_out)} pool-oracle systems", file=sys.stderr)
pickle.dump({'rows':rows_out,'avail_methods':AVAIL_METHODS}, open(OUTPKL,'wb'))

# flat CSV (drop nested candidates)
if rows_out:
    flatkeys=[k for k in rows_out[0] if k!='candidates']
    with open(OUTCSV,'w',newline='') as f:
        w=csv.writer(f); w.writerow(flatkeys)
        for r in rows_out: w.writerow([r[k] for k in flatkeys])
print("wrote",OUTPKL,OUTCSV, file=sys.stderr)
