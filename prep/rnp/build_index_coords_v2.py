#!/usr/bin/env python3
"""v2 COMBINED pass: rebuild the full index AND extract coords in ONE stream over the
39 GB prediction_files.tar.gz, covering ALL 5 methods (af3, boltz, boltz2, chai, protenix).

Non-destructive: writes only *_v2 outputs; never touches the v1 cif_index.json / caches.

Outputs:
  cif_index_v2.json                : path -> [ {method,target,instchain,seed,sample,pred_chain,
                                               ranking_score,rmsd}, ... ]   (same schema as v1)
  xmethod_plddt_table_v2.csv       : per-pose table (same columns as v1 xmethod_plddt_table.csv)
  /tmp/coords_cache_v2.pkl         : path -> {'lig': {pred_chain:[(name,(x,y,z))...]}, 'ca': {...}}

Method is taken from the TAR DIRECTORY (boltz vs boltz2 both have method-col 'boltz' in CSV,
so the dir is authoritative). CSV metadata (rmsd, ranking_score) joined by
(target, seed, sample, ligand_instance_chain) from the matching per-method CSV.
pred_chain assigned by rnp_matcher_v2.match_instances (heavy-atom-count matching; validated
to reproduce v1 index up to same-chemistry copy swaps).
"""
import tarfile, json, csv, pickle, sys, time, os, re
from collections import defaultdict
from rnp_matcher_v2 import parse_cif_chains, match_instances, strip_suffix

RNP = "/Users/rafalwiewiora/rnp_data"
TAR = f"{RNP}/prediction_files.tar.gz"
PRED_DIR = f"{RNP}/predictions"
OUT_IDX = f"{RNP}/cif_index_v2.json"
OUT_TABLE = f"{RNP}/xmethod_plddt_table_v2.csv"
OUT_CACHE = "/tmp/coords_cache_v2.pkl"

METHODS = ['af3', 'boltz', 'boltz2', 'chai', 'protenix']
CSV = {m: f"{PRED_DIR}/{m}.csv" for m in METHODS}

# ---- load CSV metadata: (method,target,seed,sample,instchain) -> (rmsd, ranking_score)
#      and inst_meta[(method,target)] = {instchain: (ccd, smiles)} (scored rows only for matching)
print("loading CSVs...", file=sys.stderr)
meta = {}            # full join key -> (rmsd_str, ranking_str)
inst_meta = defaultdict(dict)   # (method,target) -> {instchain:(ccd,smiles)}
row_present = defaultdict(set)   # (method,target,seed,sample) -> set(instchain)  (scored)
for m in METHODS:
    for r in csv.DictReader(open(CSV[m])):
        t = r['target']; seed = r['seed']; samp = r['sample']; ic = r['ligand_instance_chain']
        rmsd = r.get('rmsd', ''); rank = r.get('ranking_score', '')
        scored = False
        try:
            float(rmsd); scored = True
        except Exception:
            pass
        if scored:
            meta[(m, t, seed, samp, ic)] = (rmsd, rank)
            inst_meta[(m, t)][ic] = (r.get('ligand_ccd_code', ''), r.get('model_ligand_smiles', ''))
            row_present[(m, t, seed, samp)].add(ic)
print(f"CSV scored rows: {len(meta)}", file=sys.stderr)

def parse_member(name):
    """.../prediction_files/<method>/<target>/seed-<seed>_sample-<sample>.cif"""
    if '/prediction_files/' not in name or not name.endswith('.cif'):
        return None
    parts = name.split('/')
    try:
        i = parts.index('prediction_files')
    except ValueError:
        return None
    if i + 3 >= len(parts):
        return None
    method = parts[i + 1]; target = parts[i + 2]; fn = parts[i + 3]
    mm = re.match(r'seed-(-?\w+)_sample-(\d+)\.cif$', fn)
    if not mm:
        return None
    return method, target, mm.group(1), mm.group(2)

def _ligand_plddt(data, pred_chains, protenix=False):
    """Mean B_iso_or_equiv over heavy ligand atoms per pred_chain (parity with v1 stream_plddt)."""
    lines = data.splitlines()
    order = []; bs = None
    for i, l in enumerate(lines):
        if l.startswith('_atom_site.'):
            order.append(l.split('.', 1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')):
            bs = i; break
    if bs is None:
        return {}
    ci = {n: k for k, n in enumerate(order)}
    i_ts = ci.get('type_symbol'); i_b = ci.get('B_iso_or_equiv')
    i_auth = ci.get('auth_asym_id')
    if i_b is None or i_auth is None:
        return {}
    sums = defaultdict(float); cnts = defaultdict(int)
    for l in lines[bs:]:
        if not l.startswith('HETATM'):
            continue
        f = l.split()
        if len(f) < len(order):
            continue
        ch = strip_suffix(f[i_auth]) if protenix else f[i_auth]
        if ch not in pred_chains:
            continue
        if f[i_ts] in ('H', 'D'):
            continue
        try:
            b = float(f[i_b])
        except Exception:
            continue
        sums[ch] += b; cnts[ch] += 1
    return {c: sums[c] / cnts[c] for c in sums if cnts[c] > 0}

def parse_coords(data, pred_chains, protenix=False):
    """Return {'lig': {pred_chain:[(name,(x,y,z))...]}, 'ca': {auth_asym:{seq:(x,y,z)}}}
    Only heavy ligand atoms of pred_chains; all protein CA."""
    lines = data.splitlines()
    order = []; bs = None
    for i, l in enumerate(lines):
        if l.startswith('_atom_site.'):
            order.append(l.split('.', 1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')):
            bs = i; break
    if bs is None:
        return None
    ci = {n: k for k, n in enumerate(order)}
    i_ts = ci['type_symbol']; i_an = ci['label_atom_id']
    i_auth = ci['auth_asym_id']; i_seq = ci['auth_seq_id']
    i_x = ci['Cartn_x']; i_y = ci['Cartn_y']; i_z = ci['Cartn_z']
    lig = {pc: [] for pc in pred_chains}; ca = {}
    for l in lines[bs:]:
        if not (l.startswith('ATOM') or l.startswith('HETATM')):
            continue
        f = l.split()
        if len(f) < len(order):
            continue
        cha = strip_suffix(f[i_auth]) if protenix else f[i_auth]
        try:
            x = float(f[i_x]); y = float(f[i_y]); z = float(f[i_z])
        except Exception:
            continue
        if l.startswith('HETATM') and cha in pred_chains:
            if f[i_ts] in ('H', 'D'):
                continue
            lig[cha].append((f[i_an], (x, y, z)))
        elif l.startswith('ATOM') and f[i_an] == 'CA':
            ca.setdefault(cha, {})[f[i_seq]] = (x, y, z)
    lig = {k: v for k, v in lig.items() if v}
    if not lig:
        return None
    return {'lig': lig, 'ca': ca}

# ---- output table
fout = open(OUT_TABLE, 'w', newline='')
w = csv.writer(fout)
w.writerow(['target', 'instchain', 'method', 'seed', 'sample', 'pred_chain',
            'rmsd', 'ranking_score', 'ligand_plddt', 'n_heavy'])

index = {}
cache = {}
t0 = time.time()
processed = 0; matched = 0; no_scored = 0; parse_fail = 0
tf = tarfile.open(TAR, mode='r|gz')
for member in tf:
    processed += 1
    if processed % 50000 == 0:
        sys.stderr.write(f"[{int(time.time()-t0)}s] scanned {processed} members, indexed {matched} CIFs\n")
        sys.stderr.flush()
        json.dump(index, open(OUT_IDX + '.partial', 'w'))
        pickle.dump(cache, open(OUT_CACHE + '.partial', 'wb'), protocol=4)
        fout.flush()
    pm = parse_member(member.name)
    if pm is None:
        continue
    method, target, seed, samp = pm
    if method not in METHODS:
        continue
    scored_ics = row_present.get((method, target, seed, samp))
    if not scored_ics:
        no_scored += 1
        continue
    fobj = tf.extractfile(member)
    if fobj is None:
        continue
    data = fobj.read().decode('utf-8', 'replace')
    protenix = (method == 'protenix')
    prot, lig_chains, ci, bs = parse_cif_chains(data, protenix=protenix)
    if lig_chains is None:
        parse_fail += 1
        continue
    # match only the scored instances present in THIS seed/sample
    im = {ic: inst_meta[(method, target)][ic] for ic in scored_ics
          if ic in inst_meta.get((method, target), {})}
    if not im:
        continue
    assign = match_instances(target, im, lig_chains)  # instchain -> pred_chain
    entries = []
    pred_chains = set()
    for ic, pc in assign.items():
        rmsd, rank = meta.get((method, target, seed, samp, ic), ('', ''))
        entries.append({
            'method': method, 'target': target, 'instchain': ic,
            'seed': seed, 'sample': samp, 'pred_chain': pc,
            'ranking_score': rank,
            'rmsd': (float(rmsd) if rmsd not in ('', 'nan') else ''),
        })
        pred_chains.add(pc)
    if not entries:
        continue
    index[member.name] = entries
    # coords
    cc = parse_coords(data, pred_chains, protenix=protenix)
    if cc is not None:
        cache[member.name] = cc
    # table rows (ligand_plddt left blank; computed by v1 stream_plddt separately if needed -
    #  the plan only uses rmsd/ranking from the table + plddt if present. We fill plddt too below.)
    # compute per-chain mean B_iso (ligand pLDDT) for parity with v1 table
    plddt = _ligand_plddt(data, pred_chains, protenix)
    for e in entries:
        pc = e['pred_chain']
        val = plddt.get(pc)
        w.writerow([e['target'], e['instchain'], e['method'], e['seed'], e['sample'], pc,
                    e['rmsd'], e['ranking_score'],
                    (f"{val:.4f}" if val is not None else ''), ''])
    matched += 1
    if matched % 10000 == 0:
        fout.flush()
tf.close()

json.dump(index, open(OUT_IDX, 'w'))
pickle.dump(cache, open(OUT_CACHE, 'wb'), protocol=4)
fout.close()
sys.stderr.write(f"DONE indexed={matched} CIFs, cache={len(cache)}, "
                 f"no_scored_skipped={no_scored}, parse_fail={parse_fail}, "
                 f"in {int(time.time()-t0)}s\n")
sys.stderr.write(f"-> {OUT_IDX}\n-> {OUT_TABLE}\n-> {OUT_CACHE}\n")
