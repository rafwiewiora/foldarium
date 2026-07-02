#!/usr/bin/env python3
import tarfile, json, csv, sys, time
from collections import defaultdict

TAR="/Users/rafalwiewiora/rnp_data/prediction_files.tar.gz"
index=json.load(open('/tmp/cif_index.json'))   # path -> [ {method,target,instchain,seed,sample,pred_chain,ranking_score,rmsd}, ... ]
want=set(index.keys())
OUT="/Users/rafalwiewiora/rnp_data/xmethod_plddt_table.csv"

def parse_cif_ligand_plddt(data, pred_chains):
    """Return dict pred_chain -> mean B_iso over heavy (non-H) atoms with auth_asym_id==chain."""
    # find atom_site header order
    lines=data.splitlines()
    order=[]
    in_loop=False
    body_start=None
    for i,l in enumerate(lines):
        if l.startswith('_atom_site.'):
            order.append(l.split('.',1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')):
            body_start=i; break
    if body_start is None:
        return {}
    ci={n:k for k,n in enumerate(order)}
    i_ts=ci.get('type_symbol'); i_b=ci.get('B_iso_or_equiv')
    i_auth=ci.get('auth_asym_id'); i_lab=ci.get('label_asym_id')
    sums=defaultdict(float); cnts=defaultdict(int)
    pc=set(pred_chains)
    for l in lines[body_start:]:
        if not (l.startswith('ATOM') or l.startswith('HETATM')):
            if l.startswith('#') or l.startswith('loop_') or l.startswith('_'):
                continue
            if l.strip()=='' :
                continue
            # stop if a new category begins
            if l.startswith('data_') or l.startswith('save_'):
                break
            continue
        f=l.split()
        if len(f)<len(order): continue
        # match on auth_asym_id primarily; also accept label_asym_id
        ch=f[i_auth] if i_auth is not None else None
        chl=f[i_lab] if i_lab is not None else None
        if ch not in pc and chl not in pc:
            continue
        use=ch if ch in pc else chl
        el=f[i_ts]
        if el=='H' or el=='D': continue
        try: b=float(f[i_b])
        except: continue
        sums[use]+=b; cnts[use]+=1
    return {c: sums[c]/cnts[c] for c in sums if cnts[c]>0}

# open output, write header
fout=open(OUT,'w',newline='')
w=csv.writer(fout)
w.writerow(['target','instchain','method','seed','sample','pred_chain','rmsd','ranking_score','ligand_plddt','n_heavy'])

t0=time.time()
matched=0; processed=0; missing_chain=0
tf=tarfile.open(TAR, mode='r|gz')
for member in tf:
    processed+=1
    if processed % 200000 == 0:
        sys.stderr.write(f"[{int(time.time()-t0)}s] scanned {processed} members, matched {matched}\n"); sys.stderr.flush()
    name=member.name
    if name not in want:
        continue
    f=tf.extractfile(member)
    if f is None: continue
    data=f.read().decode('utf-8','replace')
    entries=index[name]
    pred_chains=set(e['pred_chain'] for e in entries)
    plddt=parse_cif_ligand_plddt(data, pred_chains)
    # also need n_heavy per chain -> recompute counts quickly via parse returning counts? store separately
    for e in entries:
        pc=e['pred_chain']
        val=plddt.get(pc)
        if val is None:
            missing_chain+=1
            w.writerow([e['target'],e['instchain'],e['method'],e['seed'],e['sample'],pc,e['rmsd'],e['ranking_score'],'',''])
        else:
            w.writerow([e['target'],e['instchain'],e['method'],e['seed'],e['sample'],pc,e['rmsd'],e['ranking_score'],f"{val:.4f}",''])
    matched+=1
    if matched % 5000 == 0:
        fout.flush()
        sys.stderr.write(f"[{int(time.time()-t0)}s] matched {matched}/{len(want)} files\n"); sys.stderr.flush()
fout.close()
tf.close()
sys.stderr.write(f"DONE matched={matched} want={len(want)} missing_chain_rows={missing_chain} time={int(time.time()-t0)}s\n")
