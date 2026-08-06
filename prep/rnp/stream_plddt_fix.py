#!/usr/bin/env python3
# Re-process only files that had missing ligand_plddt (protenix chain suffix issue + chai 86).
import tarfile, json, csv, sys, time, re
from collections import defaultdict

TAR="/Users/rafalwiewiora/rnp_data/prediction_files.tar.gz"
index=json.load(open('/tmp/cif_index.json'))
TABLE="/Users/rafalwiewiora/rnp_data/xmethod_plddt_table.csv"

# find paths still missing: build set of (path) where some entry's method in {protenix} or chai-miss.
# Simpler: reprocess any path whose entries belong to method protenix or chai.
want={}
for path,entries in index.items():
    m=entries[0]['method']
    if m in ('protenix',):
        want[path]=entries
print("paths to reprocess:",len(want))

def chain_variants(pred):
    # match pred exactly, or pred+digit suffix (protenix A0/B0), or cif chain stripped of trailing digits
    return pred

def parse(data, pred_chains):
    lines=data.splitlines()
    order=[];body_start=None
    for i,l in enumerate(lines):
        if l.startswith('_atom_site.'):
            order.append(l.split('.',1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')):
            body_start=i;break
    if body_start is None: return {}
    ci={n:k for k,n in enumerate(order)}
    i_ts=ci.get('type_symbol');i_b=ci.get('B_iso_or_equiv')
    i_auth=ci.get('auth_asym_id');i_lab=ci.get('label_asym_id')
    sums=defaultdict(float);cnts=defaultdict(int);pc=set(pred_chains)
    def norm(ch):
        # strip a single trailing run of digits -> 'B0'->'B'
        return re.sub(r'\d+$','',ch)
    for l in lines[body_start:]:
        if not (l.startswith('ATOM') or l.startswith('HETATM')): continue
        f=l.split()
        if len(f)<len(order): continue
        cha=f[i_auth] if i_auth is not None else None
        chl=f[i_lab] if i_lab is not None else None
        cand=None
        for raw in (cha,chl):
            if raw is None: continue
            if raw in pc: cand=raw; break
            if norm(raw) in pc: cand=norm(raw); break
        if cand is None: continue
        el=f[i_ts]
        if el in ('H','D'): continue
        try: b=float(f[i_b])
        except: continue
        sums[cand]+=b;cnts[cand]+=1
    return {c:sums[c]/cnts[c] for c in sums if cnts[c]>0}

# load existing table rows into memory, keyed for update
rowsout=[]
fixed={}  # (target,instchain,method,seed,sample,pred_chain)->plddt
t0=time.time();matched=0;processed=0;stillmiss=0
tf=tarfile.open(TAR,mode='r|gz')
for member in tf:
    processed+=1
    if member.name not in want: continue
    fobj=tf.extractfile(member)
    if fobj is None: continue
    data=fobj.read().decode('utf-8','replace')
    entries=want[member.name]
    pcs=set(e['pred_chain'] for e in entries)
    pl=parse(data,pcs)
    for e in entries:
        v=pl.get(e['pred_chain'])
        key=(e['target'],e['instchain'],e['method'],e['seed'],e['sample'],e['pred_chain'])
        if v is None: stillmiss+=1
        else: fixed[key]=v
    matched+=1
    if matched%5000==0:
        sys.stderr.write(f"[{int(time.time()-t0)}s] {matched}/{len(want)}\n");sys.stderr.flush()
tf.close()
sys.stderr.write(f"fix DONE matched={matched} stillmiss={stillmiss}\n")
json.dump({f"{'|'.join(k)}":v for k,v in fixed.items()}, open('/tmp/fixed_plddt.json','w'))
print("saved fixed:",len(fixed))
