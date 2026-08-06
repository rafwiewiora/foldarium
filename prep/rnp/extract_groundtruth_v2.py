#!/usr/bin/env python3
"""Stage B-2: from ground_truth.tar.gz pull, for each plan system (system_id=target):
  - receptor CA: {auth_asym_id: {auth_seq_id: (x,y,z)}}  (from receptor.cif)
  - true ligand heavy atoms for the item's instchain: list[(element,name,(x,y,z))] (from ligand_files/<instchain>.sdf)
Write /tmp/groundtruth.pkl: target -> {'ca':..., 'lig': {instchain: [(el,name,xyz)...]}}
"""
import tarfile, pickle, sys, re, os
RNP="/Users/rafalwiewiora/rnp_data"
GT=f"{RNP}/ground_truth.tar.gz"
PLAN=f"{RNP}/rnp_quiz_plan_v2.pkl"
OUT="/tmp/groundtruth_v2.pkl"

plan=pickle.load(open(PLAN,'rb'))
want_sys={p['target'] for p in plan}
want_lig={}  # target -> set(instchain)
for p in plan: want_lig.setdefault(p['target'],set()).add(p['instchain'])
print(f"want {len(want_sys)} systems",file=sys.stderr)

def parse_cif_ca(data):
    lines=data.splitlines(); order=[]; bs=None
    for i,l in enumerate(lines):
        if l.startswith('_atom_site.'): order.append(l.split('.',1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')): bs=i;break
    if bs is None: return {}
    ci={n:k for k,n in enumerate(order)}
    i_an=ci['label_atom_id']; i_auth=ci['auth_asym_id']; i_seq=ci['auth_seq_id']
    i_x=ci['Cartn_x']; i_y=ci['Cartn_y']; i_z=ci['Cartn_z']
    ca={}
    for l in lines[bs:]:
        if not l.startswith('ATOM'): continue
        f=l.split()
        if len(f)<len(order): continue
        if f[i_an]!='CA': continue
        try: x=float(f[i_x]);y=float(f[i_y]);z=float(f[i_z])
        except: continue
        ca.setdefault(f[i_auth],{})[f[i_seq]]=(x,y,z)
    return ca

def parse_sdf(data):
    # V2000 molfile (first molecule). Return list[(element,name,(x,y,z))], names synthesized el+idx.
    lines=data.splitlines()
    if len(lines)<4: return []
    counts=lines[3]
    try: na=int(counts[0:3])
    except: return []
    atoms=[]
    for i in range(4,4+na):
        if i>=len(lines): break
        l=lines[i]
        try:
            x=float(l[0:10]); y=float(l[10:20]); z=float(l[20:30]); el=l[31:34].strip()
        except: continue
        if el=='H': continue
        atoms.append((el,(x,y,z)))
    # synthesize unique names
    cnt={}; out=[]
    for el,xyz in atoms:
        cnt[el]=cnt.get(el,0)+1; out.append((el,f"{el}{cnt[el]}",xyz))
    return out

if os.path.exists(OUT):
    gt=pickle.load(open(OUT,'rb'))
    if want_sys.issubset(set(gt.keys())):
        print("groundtruth already cached",file=sys.stderr); sys.exit(0)
else:
    gt={}

tf=tarfile.open(GT,'r:gz')
cur=None; got=0
for m in tf:
    parts=m.name.split('/')
    if len(parts)<2: continue
    tgt=parts[1]
    if tgt not in want_sys: continue
    gt.setdefault(tgt,{'ca':None,'lig':{}})
    if m.name.endswith('receptor.cif'):
        f=tf.extractfile(m)
        if f: gt[tgt]['ca']=parse_cif_ca(f.read().decode('utf-8','replace'))
    elif '/ligand_files/' in m.name and m.name.endswith('.sdf'):
        instchain=parts[-1][:-4]  # '1.F'
        if instchain in want_lig.get(tgt,set()):
            f=tf.extractfile(m)
            if f:
                lig=parse_sdf(f.read().decode('utf-8','replace'))
                if lig: gt[tgt]['lig'][instchain]=lig
    if gt[tgt]['ca'] is not None and gt[tgt]['lig']:
        pass
tf.close()
# count fully-resolved
full=sum(1 for t in want_sys if t in gt and gt[t]['ca'] and gt[t]['lig'])
pickle.dump(gt, open(OUT,'wb'), protocol=4)
print(f"cached {len(gt)} systems; {full} have CA+lig -> {OUT}",file=sys.stderr)
