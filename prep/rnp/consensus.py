#!/usr/bin/env python3
"""Spatial-consensus selector on Runs-n-Poses pooled multi-method ensemble.

Stage 2: uses /tmp/coords_cache.pkl (ligand atoms + protein CA per prediction).
For each gameable system:
  - pool all poses (all methods)
  - pick reference (af3 lowest-seed sample-0; fallback first available)
  - for each pose: superpose its pocket-chain protein CA onto reference pocket-chain CA
    (matched by auth_seq_id, restricted to residues whose ref CA is within POCKET_CA_R of ref ligand),
    apply transform to ligand heavy atoms.
  - ligand RMSD between poses via atom-name matching (intersection of atom names).
  - greedy cluster at 2.0 A (bq.cluster style).
  - consensus pick = medoid of most-populated cluster (ties -> higher mean pLDDT).
  - correct if that pose's table-rmsd < 2.
Outputs consensus_results.json.
"""
import json, pickle, csv, sys, time, random, os, re
from collections import defaultdict
import numpy as np

KNOWN2={'CL','BR','NA','MG','ZN','FE','CA','MN','SE','SI','LI','CU','CO','NI','PT','AS','SB','SN','HG','CD','AG','AU','PD','BA','SR','CS','RB','TE','GE','GA','AL','TI','CR','MO','RU','RH','IR','OS','RE','HF','TA','ZR','NB','PB','BI','TL','IN','BE','SC'}
def elem_of(name):
    s=re.match(r'([A-Za-z]+)',name).group(1).upper()
    if len(s)>=2 and s[:2] in KNOWN2: return s[:2]
    return s[0]

CACHE=os.environ.get("CACHE","/tmp/coords_cache.pkl")
REQUIRE_FULL=True  # only score systems whose ALL table poses are present in cache
IDX="/tmp/cif_index.json"
TABLE="/Users/rafalwiewiora/rnp_data/xmethod_plddt_table.csv"
ANNOT="/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/sucos/rnp_annotations.csv"
OUT="/Users/rafalwiewiora/rnp_data/consensus_results.json"
CLUSTER_THRESH=2.0
POCKET_CA_R=12.0   # residues whose ref CA within this of ref ligand centroid-ish used for superpose
SUBSET=None        # set int to subsample gameable systems (novel always kept)

# ---------- load table: per-pose rmsd + plddt, keyed by (target,instchain,method,seed,sample,pred_chain)
def key(r): return (r['target'],r['instchain'],r['method'],r['seed'],r['sample'],r['pred_chain'])
tbl={}
sys2rows=defaultdict(list)
for r in csv.DictReader(open(TABLE)):
    rr={'target':r['target'],'instchain':r['instchain'],'method':r['method'],
        'seed':r['seed'],'sample':r['sample'],'pred_chain':r['pred_chain'],
        'rmsd':float(r['rmsd']),
        'plddt':float(r['ligand_plddt']) if r['ligand_plddt'] not in('','nan') else None}
    tbl[key(r)]=rr
    sys2rows[(r['target'],r['instchain'])].append(rr)

# ---------- novelty: system_id|ligand_instance_chain  (convert instchain '1.F' matches ligand_instance_chain)
# annotation key = system_id|ligand_instance_chain; novel = sucos_shape_pocket_qcov < 25
nov={}  # (system_id, ligand_instance_chain) -> bool novel
for r in csv.DictReader(open(ANNOT)):
    sid=r['system_id']; lic=r['ligand_instance_chain']
    v=r.get('sucos_shape_pocket_qcov','')
    try: v=float(v)
    except: continue
    nov[(sid,lic)]=(v<25.0)

def is_novel(target,instchain):
    # target IS the system_id; instchain is ligand_instance_chain
    return nov.get((target,instchain), None)

# ---------- index: path -> entries (for method/seed/sample/pred_chain per path)
idx=json.load(open(IDX))

# ---------- coords cache
print("loading cache...", file=sys.stderr)
cache=pickle.load(open(CACHE,'rb'))
print(f"cache paths: {len(cache)}", file=sys.stderr)

# ---------- build per-system list of poses with: path, entry, coords
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
            'pred_chain':pc,'rmsd':rr['rmsd'],'plddt':rr['plddt'],
            'lig':lig,'ca':cc['ca']})

# ---------- geometry helpers
def kabsch(P,Q):
    # returns R,t mapping P onto Q (minimize ||R P + t - Q||)
    Pc=P.mean(0); Qc=Q.mean(0)
    A=(P-Pc).T@(Q-Qc)
    V,S,Wt=np.linalg.svd(A)
    d=np.sign(np.linalg.det(Wt.T@V.T))
    D=np.diag([1,1,d])
    R=(Wt.T@D@V.T)
    t=Qc - R@Pc
    return R,t

def lig_array(lig, names):
    d=dict(lig)  # name->xyz (last wins; ligands unique names)
    return np.array([d[n] for n in names])

def pocket_chain_for(pose, lig_centroid_chain=None):
    # choose protein chain whose CA nearest ligand atoms
    ligxyz=np.array([xyz for _,xyz in pose['lig']])
    best=None;bestd=1e9
    for ch,resd in pose['ca'].items():
        if not resd: continue
        caxyz=np.array(list(resd.values()))
        d=np.min(np.linalg.norm(caxyz[:,None,:]-ligxyz[None,:,:],axis=2))
        if d<bestd: bestd=d;best=ch
    return best

def superpose_ligand(pose, ref_chain, ref_resids, ref_ca_xyz):
    """Match this pose's protein chain (its own pocket chain) CA to reference pocket residues by auth_seq_id.
    Build matched arrays, Kabsch, apply to ligand. Return transformed ligand atoms list[(name,xyz)] or None."""
    # which chain in this pose is the pocket chain? use chain nearest its ligand
    pc=pocket_chain_for(pose)
    if pc is None: return None
    resd=pose['ca'].get(pc,{})
    matched_p=[];matched_q=[]
    for rid in ref_resids:
        if rid in resd:
            matched_p.append(resd[rid]); matched_q.append(ref_ca_xyz[rid])
    if len(matched_p)<4:
        # fallback: try ALL chains of this pose, pick the one giving most matches
        bestn=0;bestpc=None
        for ch,rd in pose['ca'].items():
            n=sum(1 for rid in ref_resids if rid in rd)
            if n>bestn: bestn=n;bestpc=ch
        if bestpc is None or bestn<4: return None
        rd=pose['ca'][bestpc]
        matched_p=[];matched_q=[]
        for rid in ref_resids:
            if rid in rd:
                matched_p.append(rd[rid]); matched_q.append(ref_ca_xyz[rid])
    P=np.array(matched_p); Q=np.array(matched_q)
    R,t=kabsch(P,Q)
    L=np.array([xyz for _,xyz in pose['lig']])
    Lt=(R@L.T).T + t
    elems=tuple(elem_of(nm) for nm,_ in pose['lig'])
    return (elems, Lt), len(matched_p)

def lrmsd(a,b):
    # a,b: (elems_tuple, Nx3 array). Index correspondence valid iff element sequences identical.
    ea,xa=a; eb,xb=b
    if ea!=eb: return None
    d=xa-xb
    return float(np.sqrt((d*d).sum(1).mean()))

def cluster(coords_dicts):
    n=len(coords_dicts)
    labels=[-1]*n; cid=0
    for i in range(n):
        if labels[i]>=0: continue
        labels[i]=cid
        for j in range(i+1,n):
            if labels[j]<0:
                r=lrmsd(coords_dicts[i],coords_dicts[j])
                if r is not None and r<CLUSTER_THRESH:
                    labels[j]=cid
        cid+=1
    return labels,cid

# ---------- main loop
# Only af3/boltz/chai are present in the tar (protenix absent from archive).
AVAIL_METHODS={p['method'] for poses in sys2poses.values() for p in poses}
print(f"methods available in coords: {sorted(AVAIL_METHODS)}", file=sys.stderr)
systems=sorted(sys2poses.keys())
# covered: for every AVAILABLE-method pose the table lists for this system, coords are present.
# (Systems whose only correct poses are protenix-only would lose oracle=100%; handled by oracle filter below.)
COVER_FRAC=float(os.environ.get("COVER_FRAC","0.9"))
def covered(s):
    want_avail=sum(1 for r in sys2rows[s] if r['method'] in AVAIL_METHODS)
    have=len(sys2poses[s])
    # require we captured >=COVER_FRAC of the available-method poses, and still have a correct one in-pool
    return want_avail>0 and have>=COVER_FRAC*want_avail and any(p['rmsd']<2 for p in sys2poses[s])
# gameable on the AVAILABLE pool: >=1 correct pose among coords we actually have
gameable=[s for s in systems if any(p['rmsd']<2 for p in sys2poses[s])]
n_before=len(gameable)
if REQUIRE_FULL:
    gameable=[s for s in gameable if covered(s)]
print(f"gameable(avail-pool) systems with coords: {n_before}; covered: {len(gameable)}", file=sys.stderr)
print(f"novel covered: {sum(1 for s in gameable if is_novel(*s)==True)}", file=sys.stderr)

# subsetting
if SUBSET and len(gameable)>SUBSET:
    novel_sys=[s for s in gameable if is_novel(*s)]
    rest=[s for s in gameable if not is_novel(*s)]
    random.seed(0); random.shuffle(rest)
    keep=set(novel_sys)|set(rest[:max(0,SUBSET-len(novel_sys))])
    gameable=[s for s in gameable if s in keep]
    print(f"subset to {len(gameable)} (novel kept {len(novel_sys)})", file=sys.stderr)

# accounting: gameable systems in table excluded for lack of af3/boltz/chai coverage
all_gameable_table=[s for s in sys2rows if any(r['rmsd']<2 for r in sys2rows[s])]
excluded_protenix_only=[s for s in all_gameable_table
                        if not any(r['method'] in AVAIL_METHODS for r in sys2rows[s])]
excl_protenix_only_novel=[s for s in excluded_protenix_only if is_novel(*s)==True]

results=[]
t0=time.time()
for gi,s in enumerate(gameable):
    poses=sys2poses[s]
    target,instchain=s
    # reference: af3 lowest seed sample-0; fallback lowest (method,seed,sample)
    af3=[p for p in poses if p['method']=='af3']
    ref=None
    if af3:
        af3s=sorted(af3,key=lambda p:(int(p['seed']) if p['seed'].lstrip('-').isdigit() else 0, int(p['sample'])))
        for p in af3s:
            if p['sample']=='0': ref=p;break
        if ref is None: ref=af3s[0]
    if ref is None:
        ref=sorted(poses,key=lambda p:(p['method'],p['seed'],p['sample']))[0]
    # ref pocket chain + pocket residues (CA within POCKET_CA_R of ref ligand)
    refpc=pocket_chain_for(ref)
    if refpc is None:
        results.append({'system':target+'|'+instchain,'skip':'no_ref_pocket'}); continue
    ref_lig_xyz=np.array([xyz for _,xyz in ref['lig']])
    ref_resd=ref['ca'][refpc]
    ref_ca_xyz={}; ref_resids=[]
    for rid,xyz in ref_resd.items():
        ax=np.array(xyz)
        if np.min(np.linalg.norm(ref_lig_xyz-ax,axis=1))<POCKET_CA_R:
            ref_ca_xyz[rid]=xyz; ref_resids.append(rid)
    if len(ref_resids)<4:
        # use all CA of ref chain
        for rid,xyz in ref_resd.items(): ref_ca_xyz[rid]=xyz; ref_resids.append(rid)
    # transform every pose's ligand into ref frame (ref maps to itself via identity? still superpose for consistency)
    transformed=[]  # per pose: coords=(elems_tuple, Nx3 array), plus meta
    for p in poses:
        if p is ref:
            elems=tuple(elem_of(nm) for nm,_ in p['lig'])
            xyz=np.array([c for _,c in p['lig']])
            transformed.append({'coords':(elems,xyz),'rmsd':p['rmsd'],'plddt':p['plddt'],'method':p['method'],'nmatch':len(ref_resids)})
            continue
        res=superpose_ligand(p, refpc, ref_resids, ref_ca_xyz)
        if res is None:
            transformed.append(None); continue
        out,nm=res
        transformed.append({'coords':out,'rmsd':p['rmsd'],'plddt':p['plddt'],'method':p['method'],'nmatch':nm})
    valid=[t for t in transformed if t is not None]
    n_failed=sum(1 for t in transformed if t is None)
    if len(valid)<2:
        results.append({'system':target+'|'+instchain,'skip':'too_few_aligned','n_failed':n_failed,'n_pose':len(poses)}); continue
    coords=[t['coords'] for t in valid]
    labels,ncl=cluster(coords)
    # cluster sizes
    csize=defaultdict(list)
    for i,l in enumerate(labels): csize[l].append(i)
    # most populated; ties -> higher mean plddt
    def clu_meanplddt(members):
        ps=[valid[i]['plddt'] for i in members if valid[i]['plddt'] is not None]
        return sum(ps)/len(ps) if ps else -1
    order=sorted(csize.keys(), key=lambda c:(len(csize[c]), clu_meanplddt(csize[c])), reverse=True)
    topc=order[0]; members=csize[topc]
    # medoid of top cluster
    def med(members):
        if len(members)==1: return members[0]
        best=None;bestsum=1e18
        for i in members:
            s_=0.0;ok=True
            for j in members:
                r=lrmsd(coords[i],coords[j])
                if r is None: continue
                s_+=r
            if s_<bestsum: bestsum=s_;best=i
        return best
    pick=med(members)
    pick_correct=valid[pick]['rmsd']<2
    plur=len(members)/len(valid)
    results.append({
        'system':target+'|'+instchain,'target':target,'instchain':instchain,
        'novel':is_novel(target,instchain),
        'n_methods':len(set(p['method'] for p in poses)),
        'n_pose':len(poses),'n_aligned':len(valid),'n_failed':n_failed,
        'n_clusters':ncl,'top_cluster_size':len(members),'plurality':plur,
        'consensus_correct':pick_correct,
        'consensus_pick_rmsd':valid[pick]['rmsd'],
        'oracle_correct':any(t['rmsd']<2 for t in valid),
    })
    if (gi+1)%100==0:
        el=time.time()-t0
        print(f"[{int(el)}s] {gi+1}/{len(gameable)} ({el/(gi+1):.2f}s/sys)", file=sys.stderr); sys.stderr.flush()

# ---------- aggregate
def acc(rs):
    rs=[r for r in rs if 'consensus_correct' in r]
    return (round(100*sum(r['consensus_correct'] for r in rs)/len(rs),1), len(rs)) if rs else (None,0)

scored=[r for r in results if 'consensus_correct' in r]
skipped=[r for r in results if 'skip' in r]
all_acc=acc(scored)
novel_scored=[r for r in scored if r.get('novel')==True]
familiar_scored=[r for r in scored if r.get('novel')==False]
novel_acc=acc(novel_scored)
fam_acc=acc(familiar_scored)

# raw max plddt on the SAME scored systems (sanity). methods_filter=None -> all table methods (4, incl protenix);
# methods_filter=AVAIL -> only af3/boltz/chai (matched to consensus pool).
def rawmax_on(sysset, methods_filter=None):
    cor=0;tot=0
    for r in scored:
        if sysset is not None and r['novel']!=sysset: continue
        rows=sys2rows[(r['target'],r['instchain'])]
        rows=[x for x in rows if x['plddt'] is not None and (methods_filter is None or x['method'] in methods_filter)]
        if not rows: continue
        if not any(x['rmsd']<2 for x in rows): continue
        tot+=1
        best=max(rows,key=lambda x:x['plddt'])
        if best['rmsd']<2: cor+=1
    return (round(100*cor/tot,1),tot) if tot else (None,0)

# 4-method (incl protenix) raw-max — the published pLDDT baseline, on scored systems
raw_all=rawmax_on(None,None)
raw_novel=rawmax_on(True,None)
# 3-method (af3/boltz/chai) raw-max — matched to the consensus pool
raw_all_3m=rawmax_on(None,AVAIL_METHODS)
raw_novel_3m=rawmax_on(True,AVAIL_METHODS)

# random baseline (expected acc = mean fraction of correct poses per system)
def rand_on(rs):
    fr=[]
    for r in rs:
        rows=sys2rows[(r['target'],r['instchain'])]
        if not rows: continue
        fr.append(sum(1 for x in rows if x['rmsd']<2)/len(rows))
    return round(100*sum(fr)/len(fr),1) if fr else None

rand_all=rand_on(scored)
rand_novel=rand_on(novel_scored)

# plurality strength: correct vs wrong
plur_correct=[r['plurality'] for r in scored if r['consensus_correct']]
plur_wrong=[r['plurality'] for r in scored if not r['consensus_correct']]
import statistics as st, math
def msd(x): return (round(st.mean(x),3),round(st.pstdev(x),3),len(x)) if x else (None,None,0)
avg_clusters=round(st.mean([r['n_clusters'] for r in scored]),2) if scored else None
# point-biserial corr of plurality vs correctness, and accuracy by plurality bin
def pbcorr(rs):
    xs=[r['plurality'] for r in rs]; ys=[1 if r['consensus_correct'] else 0 for r in rs]
    n=len(xs)
    if n<3 or len(set(ys))<2: return None
    mx=sum(xs)/n; my=sum(ys)/n
    num=sum((a-mx)*(b-my) for a,b in zip(xs,ys))
    den=math.sqrt(sum((a-mx)**2 for a in xs)*sum((b-my)**2 for b in ys))
    return round(num/den,3) if den else None
plur_pb=pbcorr(scored)
bins=[(0,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.0001)]
plur_acc_bins={}
for lo,hi in bins:
    sub=[r for r in scored if lo<=r['plurality']<hi]
    if sub:
        plur_acc_bins[f'{lo}-{hi if hi<=1 else 1.0}']=(round(100*sum(r['consensus_correct'] for r in sub)/len(sub),1),len(sub))

multi=[r for r in scored if r.get('n_methods',1)>1]
single=[r for r in scored if r.get('n_methods',1)==1]
multi_novel=[r for r in multi if r.get('novel')==True]
summary={
  'n_scored':len(scored),'n_skipped':len(skipped),
  'consensus_acc_multimethod':acc(multi),
  'consensus_acc_multimethod_novel':acc(multi_novel),
  'consensus_acc_singlemethod':acc(single),
  'n_by_method_count':{k:sum(1 for r in scored if r.get('n_methods')==k) for k in sorted(set(r.get('n_methods') for r in scored))},
  'skip_reasons':{k:sum(1 for r in skipped if r.get('skip')==k) for k in set(r.get('skip') for r in skipped)},
  'consensus_acc_all':all_acc,
  'consensus_acc_novel':novel_acc,
  'consensus_acc_familiar':fam_acc,
  'rawmax_plddt_all_4method_SANITY':raw_all,
  'rawmax_plddt_novel_4method_SANITY':raw_novel,
  'rawmax_plddt_all_3method_pool':raw_all_3m,
  'rawmax_plddt_novel_3method_pool':raw_novel_3m,
  'methods_in_consensus_pool':sorted(AVAIL_METHODS),
  'n_gameable_in_table':len(all_gameable_table),
  'n_excluded_protenix_only':len(excluded_protenix_only),
  'n_excluded_protenix_only_novel':len(excl_protenix_only_novel),
  'cover_frac_threshold':COVER_FRAC,
  'note':'protenix predictions ABSENT from prediction_files.tar.gz (archive ends after chai); spatial consensus pools af3+boltz+chai only. Excluded systems are overwhelmingly protenix-only (no af3/boltz/chai pose at all).',
  'random_baseline_all':rand_all,
  'random_baseline_novel':rand_novel,
  'oracle':100.0,
  'avg_clusters_per_system':avg_clusters,
  'plurality_correct_mean_std_n':msd(plur_correct),
  'plurality_wrong_mean_std_n':msd(plur_wrong),
  'plurality_pointbiserial_vs_correct':plur_pb,
  'accuracy_by_plurality_bin':plur_acc_bins,
  'avg_n_pose':round(st.mean([r['n_pose'] for r in scored]),1) if scored else None,
  'avg_n_aligned':round(st.mean([r['n_aligned'] for r in scored]),1) if scored else None,
  'avg_n_failed':round(st.mean([r['n_failed'] for r in scored]),2) if scored else None,
}
json.dump({'summary':summary,'per_system':results}, open(OUT,'w'), indent=2)
print(json.dumps(summary,indent=2))
print("wrote",OUT, file=sys.stderr)
