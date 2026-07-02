#!/usr/bin/env python3
"""Stage B-1: stream prediction_files.tar.gz ONCE to pull the FULL protein atoms
(all atoms, all residues of the pocket chain) for each item's REFERENCE prediction.
The coords cache only kept CA; for protein.pdb/pocket.pdb we need all atoms.

Streams r|gz, never extracts. Writes /tmp/ref_protein_atoms.pkl:
  ref_path -> { 'atoms': [ (auth_asym_id, auth_seq_id, resname, atom_name, element, (x,y,z)) ... protein only ] }
Resumable: if OUT exists and has all wanted paths, skip.
"""
import tarfile, json, pickle, sys, time, os, re

def strip_suffix(ch):
    # protenix auth_asym_id has a model suffix ('B0'->'B'); leave plain ids unchanged
    m=re.match(r'^([A-Za-z]+)\d*$', ch)
    return m.group(1) if m else ch

TAR="/Users/rafalwiewiora/rnp_data/prediction_files.tar.gz"
PLAN="/Users/rafalwiewiora/rnp_data/rnp_quiz_plan_v2.pkl"
OUT="/tmp/ref_protein_atoms_v2.pkl"

plan=pickle.load(open(PLAN,'rb'))
want={p['ref_path'] for p in plan}
print(f"want {len(want)} ref CIFs",file=sys.stderr)

if os.path.exists(OUT):
    have=pickle.load(open(OUT,'rb'))
    if want.issubset(set(have.keys())):
        print("all ref proteins already cached; nothing to do",file=sys.stderr); sys.exit(0)
    print(f"resume: have {len(have)}, still need {len(want-set(have.keys()))}",file=sys.stderr)
else:
    have={}

def parse(data):
    lines=data.splitlines(); order=[]; bs=None
    for i,l in enumerate(lines):
        if l.startswith('_atom_site.'):
            order.append(l.split('.',1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')):
            bs=i;break
    if bs is None: return None
    ci={n:k for k,n in enumerate(order)}
    i_ts=ci['type_symbol']; i_an=ci['label_atom_id']
    i_auth=ci['auth_asym_id']; i_seq=ci['auth_seq_id']
    i_comp=ci.get('auth_comp_id', ci.get('label_comp_id'))
    i_x=ci['Cartn_x']; i_y=ci['Cartn_y']; i_z=ci['Cartn_z']
    atoms=[]
    for l in lines[bs:]:
        if not l.startswith('ATOM'): continue   # protein polymer atoms only
        f=l.split()
        if len(f)<len(order): continue
        try: x=float(f[i_x]); y=float(f[i_y]); z=float(f[i_z])
        except: continue
        el=f[i_ts]
        if el=='H': continue
        atoms.append((strip_suffix(f[i_auth]), f[i_seq], f[i_comp], f[i_an], el, (x,y,z)))
    return atoms

t0=time.time(); tf=tarfile.open(TAR,mode='r|gz')
need=want-set(have.keys()); processed=0; got=0; nneed=len(need)
for member in tf:
    processed+=1
    if member.name not in need: continue
    fobj=tf.extractfile(member)
    if fobj is None: continue
    atoms=parse(fobj.read().decode('utf-8','replace'))
    if atoms: have[member.name]={'atoms':atoms}; got+=1
    if got and got%200==0:
        pickle.dump(have, open(OUT+'.partial','wb'), protocol=4)
        sys.stderr.write(f"[{int(time.time()-t0)}s] processed={processed} got={got}/{nneed}\n"); sys.stderr.flush()
    if got>=nneed:
        sys.stderr.write(f"all ref proteins collected at processed={processed}\n"); break
tf.close()
pickle.dump(have, open(OUT,'wb'), protocol=4)
sys.stderr.write(f"DONE got={got} total_cached={len(have)} in {int(time.time()-t0)}s -> {OUT}\n")
