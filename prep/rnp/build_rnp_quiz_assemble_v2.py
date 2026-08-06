#!/usr/bin/env python3
"""Stage C: assemble the quiz from the plan + streamed ref proteins + ground truth.

Per item:
  - protein.pdb : full pocket-chain protein (apo-equivalent: protein only, ligand removed) of the REFERENCE prediction
  - pocket.pdb  : pocket-chain residues whose any atom is within POCKET_R of any pose atom (union pocket sticks)
  - pose-N.pdb  : ANONYMIZED ligand-only PDB, already in the common ref frame (from plan)
  - xtal_lig.pdb: true ligand aligned into the common frame via GT-receptor pocket-CA -> ref pocket-CA Kabsch
Writes quiz_items_rnp.json (list) + data_rnp/<id>/*. Resumable: skips items whose dir + files exist.
"""
import json, pickle, os, sys, re
from collections import defaultdict
import numpy as np

RNP="/Users/rafalwiewiora/rnp_data"
QUIZ="/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/quiz"
DATA=f"{QUIZ}/data_rnp_v2"
DATA_REL="data_rnp_v2"
PLAN=f"{RNP}/rnp_quiz_plan_v2.pkl"
REFPROT="/tmp/ref_protein_atoms_v2.pkl"
GTPATH="/tmp/groundtruth_v2.pkl"
OUTJSON=f"{QUIZ}/quiz_items_rnp_v2.json"
POCKET_R=5.0
POCKET_CA_R=12.0

os.makedirs(DATA, exist_ok=True)
plan=pickle.load(open(PLAN,'rb'))
refprot=pickle.load(open(REFPROT,'rb'))
gt=pickle.load(open(GTPATH,'rb'))
print(f"plan items: {len(plan)}; ref proteins: {len(refprot)}; gt systems: {len(gt)}",file=sys.stderr)

def kabsch(P,Q):
    Pc=P.mean(0); Qc=Q.mean(0); A=(P-Pc).T@(Q-Qc)
    V,S,Wt=np.linalg.svd(A); d=np.sign(np.linalg.det(Wt.T@V.T))
    R=(Wt.T@np.diag([1,1,d])@V.T); t=Qc-R@Pc; return R,t

def write_protein(atoms, dest, near=None):
    # atoms: list[(chain,seq,resname,atomname,element,(x,y,z))] already restricted to ONE chain
    out=[]; i=0
    # group by residue to allow whole-residue pocket selection
    if near is not None:
        byres=defaultdict(list)
        for a in atoms: byres[(a[1],a[2])].append(a)
        keep=[]
        for rk,ra in byres.items():
            xyz=np.array([a[5] for a in ra])
            if np.min(np.linalg.norm(xyz[:,None,:]-near[None,:,:],axis=2))<POCKET_R:
                keep.extend(ra)
        atoms=keep
    for ch,seq,resn,an,el,(x,y,z) in atoms:
        i+=1
        try: sn=int(seq)
        except: sn=i
        out.append(f"ATOM  {i:>5d} {an[:4]:<4s} {resn[:3]:>3s} A{sn:>4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {el:>2s}")
    out.append("END")
    open(dest,'w').write("\n".join(out)+"\n")
    return len(atoms)

def write_lig(atoms, dest):  # atoms: list[(element,name,(x,y,z))]
    out=[f"HETATM{i+1:>5d} {nm[:4]:<4s} LIG X   1    {p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {el:>2s}"
         for i,(el,nm,p) in enumerate(atoms)]
    out.append("END"); open(dest,'w').write("\n".join(out)+"\n")

items=[]
stats=defaultdict(int)
for pi,p in enumerate(plan):
    target=p['target']; instchain=p['instchain']
    sysid=target  # system_id
    # unique item id: target + instchain (sanitized) to avoid collisions for multi-ligand systems
    iid=f"{target}__{instchain}".replace('.','_')
    dd=f"{DATA}/{iid}"; os.makedirs(dd, exist_ok=True)

    # --- reference protein (full atoms), restrict to the reference pocket chain ---
    rp=refprot.get(p['ref_path'])
    if rp is None:
        stats['missing_ref_protein']+=1
        # can't render protein; skip item entirely
        continue
    refchain=p['ref_pred_chain']  # NOTE: this is the LIGAND pred_chain; protein chain differs.
    # choose protein chain = chain with most CA near the pose ligands (use ref pocket resids' chain)
    # group ref atoms by chain; pick the protein chain whose CA set best matches ref_pocket_resids
    by_chain=defaultdict(list)
    for a in rp['atoms']: by_chain[a[0]].append(a)
    refresids=set(p['ref_pocket_resids'])
    bestch=None;bestn=-1
    for ch,atoms in by_chain.items():
        seqs={a[1] for a in atoms if a[3]=='CA'}
        n=len(seqs & refresids)
        if n>bestn: bestn=n;bestch=ch
    if bestch is None or bestn<4:
        stats['no_protein_chain_match']+=1; continue
    prot_atoms=by_chain[bestch]

    # --- pose coords (already in ref frame) ---
    all_pose_xyz=[]
    for c in p['choices']:
        all_pose_xyz.append(np.array(c['xyz']))
    near=np.vstack(all_pose_xyz)

    write_protein(prot_atoms, f"{dd}/protein.pdb")
    np_pocket=write_protein(prot_atoms, f"{dd}/pocket.pdb", near=near)
    if np_pocket==0:
        # fallback: pocket = whole protein chain
        write_protein(prot_atoms, f"{dd}/pocket.pdb")

    # --- write anonymized poses ---
    choices=[]
    for k,c in enumerate(p['choices']):
        elems=c['elems']; names=c['names']; xyz=c['xyz']
        atoms=[(elems[j], names[j], tuple(xyz[j])) for j in range(len(elems))]
        write_lig(atoms, f"{dd}/pose-{k}.pdb")
        choices.append({
            'af3_sample':k, 'pose_file':f"{DATA_REL}/{iid}/pose-{k}.pdb",
            'rmsd':c['rmsd'], 'correct':c['correct'], 'plddt':c['plddt'],
            'cluster':c['cluster'], 'is_rep':c['is_rep'], '_method':c['_method'],
        })

    # --- crystal reveal: align GT ligand into ref frame ---
    xtal_file=None
    g=gt.get(target)
    if g and g['ca'] and instchain in g['lig']:
        # ref pocket CA (from streamed ref protein, this chain) keyed by seqid
        ref_ca={a[1]:np.array(a[5]) for a in prot_atoms if a[3]=='CA'}
        # GT receptor CA: pick the chain best matching ref pocket resids
        gtca=g['ca']; gbest=None;gn=-1
        for ch,resd in gtca.items():
            n=len(set(resd.keys()) & set(ref_ca.keys()))
            if n>gn: gn=n;gbest=ch
        if gbest is not None and gn>=6:
            gca=gtca[gbest]
            # match by seqid, restrict to ref pocket resids (near ligand) for a local superposition
            P=[];Q=[]
            for sid in ref_ca:
                if sid in gca:
                    P.append(gca[sid]); Q.append(ref_ca[sid])
            if len(P)>=6:
                R,t=kabsch(np.array(P),np.array(Q))
                ligatoms=[(el,nm,tuple((R@np.array(xyz))+t)) for el,nm,xyz in g['lig'][instchain]]
                write_lig(ligatoms, f"{dd}/xtal_lig.pdb")
                xtal_file=f"{DATA_REL}/{iid}/xtal_lig.pdb"
                stats['xtal_ok']+=1
            else: stats['xtal_too_few_ca']+=1
        else: stats['xtal_no_gt_chain']+=1
    else: stats['xtal_no_gt']+=1

    # plddt_pick_sample: the pose with highest ligand_plddt (AI baseline pick, like CAMEO)
    valid_plddt=[(k,c['plddt']) for k,c in enumerate(p['choices']) if c['plddt'] is not None]
    pick=max(valid_plddt,key=lambda z:z[1])[0] if valid_plddt else 0

    items.append({
        'id':iid, 'source':'rnp', 'ligand':p['ccd'] or 'LIG', 'single_pocket':True,
        'bucket':p['bucket'], 'build_bucket':p.get('build_bucket',p['bucket']),
        'has_correct':bool(p['has_correct']), 'novel':p['novel'],
        'protein_file':f"{DATA_REL}/{iid}/protein.pdb",
        'pocket_file':f"{DATA_REL}/{iid}/pocket.pdb",
        'xtal_lig_file':xtal_file,
        'choices':choices,
        'n_clusters':p['n_clusters'], 'n_correct':p['n_correct'],
        'plddt_pick_sample':pick,
    })
    stats['written']+=1
    if (pi+1)%100==0:
        print(f"[{pi+1}/{len(plan)}] written={stats['written']} xtal_ok={stats['xtal_ok']}",file=sys.stderr)

json.dump(items, open(OUTJSON,'w'), indent=1)
print(json.dumps(dict(stats),indent=2))
print(f"wrote {len(items)} items -> {OUTJSON}",file=sys.stderr)
