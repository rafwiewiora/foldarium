#!/usr/bin/env python3
"""ONE sequential pass over prediction_files.tar.gz.
For every CIF in the index, extract:
  - ligand heavy atoms of pred_chain: {atom_name: (x,y,z)} (ordered list of (name,xyz))
  - all protein CA atoms: {auth_asym_id: {auth_seq_id: (x,y,z)}}
Write a compact pickle keyed by tar path. Used downstream for alignment+clustering.
Streams; never extracts the whole archive.
"""
import tarfile, json, pickle, sys, time

TAR="/Users/rafalwiewiora/rnp_data/prediction_files.tar.gz"
IDX="/tmp/cif_index.json"
OUT="/tmp/coords_cache.pkl"

idx=json.load(open(IDX))
# path -> set of pred_chains we care about (usually one)
want={}
for path,entries in idx.items():
    want[path]={e['pred_chain'] for e in entries}

def parse(data, pred_chains):
    lines=data.splitlines()
    order=[];bs=None
    for i,l in enumerate(lines):
        if l.startswith('_atom_site.'):
            order.append(l.split('.',1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')):
            bs=i;break
    if bs is None: return None
    ci={n:k for k,n in enumerate(order)}
    i_ts=ci['type_symbol']; i_an=ci['label_atom_id']
    i_auth=ci['auth_asym_id']; i_lab=ci.get('label_asym_id')
    i_seq=ci['auth_seq_id']
    i_x=ci['Cartn_x']; i_y=ci['Cartn_y']; i_z=ci['Cartn_z']
    i_grp=0
    # ligand: list of (name, (x,y,z)) for atoms whose auth or label chain in pred_chains, heavy
    lig={pc:[] for pc in pred_chains}
    ca={}  # auth_asym_id -> {auth_seq_id: (x,y,z)}
    for l in lines[bs:]:
        if not (l.startswith('ATOM') or l.startswith('HETATM')): continue
        f=l.split()
        if len(f)<len(order): continue
        el=f[i_ts]
        cha=f[i_auth]; chl=f[i_lab] if i_lab is not None else None
        try:
            x=float(f[i_x]); y=float(f[i_y]); z=float(f[i_z])
        except: continue
        # ligand?
        hit=None
        if cha in pred_chains: hit=cha
        elif chl in pred_chains: hit=chl
        if hit is not None and l.startswith('HETATM'):
            if el in ('H','D'): continue
            lig[hit].append((f[i_an], (x,y,z)))
            continue
        # protein CA
        if l.startswith('ATOM') and f[i_an]=='CA':
            ca.setdefault(cha,{})[f[i_seq]]=(x,y,z)
    # drop empty ligand chains
    lig={k:v for k,v in lig.items() if v}
    if not lig: return None
    return {'lig':lig,'ca':ca}

t0=time.time()
cache={}
tf=tarfile.open(TAR,mode='r|gz')
processed=0; got=0; last_dump=0; nwant=len(want)
for member in tf:
    processed+=1
    if member.name not in want: continue
    fobj=tf.extractfile(member)
    if fobj is None: continue
    data=fobj.read().decode('utf-8','replace')
    res=parse(data, want[member.name])
    if res is not None:
        cache[member.name]=res
        got+=1
    if got and got//2000 != last_dump//2000:
        last_dump=got
        sys.stderr.write(f"[{int(time.time()-t0)}s] processed={processed} got={got}/{nwant}\n"); sys.stderr.flush()
        pickle.dump(cache, open(OUT+'.partial','wb'), protocol=4)  # incremental snapshot
    if got>=nwant:
        sys.stderr.write(f"all wanted collected at processed={processed}\n"); break
tf.close()
pickle.dump(cache, open(OUT,'wb'), protocol=4)
sys.stderr.write(f"DONE got={got} in {int(time.time()-t0)}s -> {OUT}\n")
