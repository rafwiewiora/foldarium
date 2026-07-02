#!/usr/bin/env python3
"""Stream the tar, parse ONLY protenix CIFs, STOP at end of protenix block (member ~258k)
so we don't waste time on the 154k post-protenix members.
PROTENIX CHAIN FIX: protenix auth_asym_id has a model suffix (e.g. 'B0','A0') while the
index pred_chain is 'B'. Match by stripping trailing digits from the file chain id.
Merge into /tmp/coords_cache.pkl (4-method).
"""
import tarfile, json, pickle, sys, time, re

TAR="/Users/rafalwiewiora/rnp_data/prediction_files.tar.gz"
IDX="/tmp/cif_index.json"
CACHE="/tmp/coords_cache.pkl"

idx=json.load(open(IDX))
want={}
for path,entries in idx.items():
    if entries and entries[0]['method']=='protenix':
        want[path]={e['pred_chain'] for e in entries}
print(f"protenix paths wanted: {len(want)}", file=sys.stderr)

def strip_suffix(ch):
    # 'B0'->'B', 'A0'->'A', 'AB0'->'AB'; leave plain 'B' as 'B'
    m=re.match(r'^([A-Za-z]+)\d*$', ch)
    return m.group(1) if m else ch

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
    lig={pc:[] for pc in pred_chains}; ca={}
    for l in lines[bs:]:
        if not (l.startswith('ATOM') or l.startswith('HETATM')): continue
        f=l.split()
        if len(f)<len(order): continue
        el=f[i_ts]
        cha_raw=f[i_auth]; chl_raw=f[i_lab] if i_lab is not None else None
        cha=strip_suffix(cha_raw); chl=strip_suffix(chl_raw) if chl_raw else None
        try: x=float(f[i_x]); y=float(f[i_y]); z=float(f[i_z])
        except: continue
        hit=None
        if cha in pred_chains: hit=cha
        elif chl in pred_chains: hit=chl
        if hit is not None and l.startswith('HETATM'):
            if el in ('H','D'): continue
            lig[hit].append((f[i_an],(x,y,z))); continue
        if l.startswith('ATOM') and f[i_an]=='CA':
            ca.setdefault(cha,{})[f[i_seq]]=(x,y,z)
    lig={k:v for k,v in lig.items() if v}
    if not lig: return None
    return {'lig':lig,'ca':ca}

cache=pickle.load(open(CACHE,'rb'))
print(f"existing cache: {len(cache)}", file=sys.stderr)
t0=time.time()
tf=tarfile.open(TAR,mode='r|gz')
processed=0; got=0; nwant=len(want); seen_protenix=False; post_protenix=0
for member in tf:
    processed+=1
    n=member.name
    isprot='/protenix/' in n
    if isprot: seen_protenix=True
    else:
        if seen_protenix:
            post_protenix+=1
            # once we've left the protenix block for a while, stop
            if post_protenix>200:
                sys.stderr.write(f"left protenix block at processed={processed}, stopping\n"); break
        continue
    post_protenix=0
    if n not in want: continue
    fobj=tf.extractfile(member)
    if fobj is None: continue
    data=fobj.read().decode('utf-8','replace')
    res=parse(data, want[n])
    if res is not None:
        cache[n]=res; got+=1
    if got and got%3000==0:
        sys.stderr.write(f"[{int(time.time()-t0)}s] processed={processed} protenix got={got}/{nwant}\n"); sys.stderr.flush()
    if got>=nwant:
        sys.stderr.write(f"all protenix collected at processed={processed}\n"); break
tf.close()
pickle.dump(cache, open(CACHE,'wb'), protocol=4)
sys.stderr.write(f"DONE protenix got={got} total cache={len(cache)} in {int(time.time()-t0)}s\n")
