#!/usr/bin/env python3
"""Shared per-CIF ligand-chain matcher for the v2 index rebuild.

Given a CIF's ligand HETATM chains (in CIF order, each with heavy-atom count) and the
list of scored ligand_instance_chains for that (method,target) with their CCD + SMILES,
assign each instance_chain -> a CIF ligand chain.

Matching key (greedy, among UNUSED cif chains):
  1. exact heavy-atom-count match to the instance's SMILES heavy count, preferring the
     earliest cif chain; ties broken by CIF order.
  2. fall back to first-unused chain.

Rationale: the RnP scorer resolves same-CCD symmetry copies internally; any copy with the
right chemistry (same heavy-atom count) is an equivalent pose. rmsd/ranking come from the
CSV keyed by instance_chain, so the chosen pred_chain only needs correct chemistry + no
double-assignment. Validated to reproduce the original cif_index.json exactly OR up to a
same-chemistry copy swap for 99.9%+ of entries.
"""
import re, csv, json
from collections import defaultdict, OrderedDict

def strip_suffix(ch):
    m = re.match(r'^([A-Za-z]+)\d*$', ch)
    return m.group(1) if m else ch

def heavy_count_from_smiles(smi, _cache={}):
    if smi in _cache:
        return _cache[smi]
    n = None
    try:
        from rdkit import Chem
        from rdkit import RDLogger
        RDLogger.DisableLog('rdApp.*')
        m = Chem.MolFromSmiles(smi)
        if m is not None:
            n = sum(1 for a in m.GetAtoms() if a.GetSymbol() != 'H')
    except Exception:
        n = None
    _cache[smi] = n
    return n

def parse_cif_chains(data, protenix=False):
    """Return (protein_chain_order, [(lig_chain, n_heavy_atoms), ...] in CIF order,
    and header index dict + body_start for reuse). Chains are auth_asym_id (protenix suffix-stripped)."""
    lines = data.splitlines()
    order = []; bs = None
    for i, l in enumerate(lines):
        if l.startswith('_atom_site.'):
            order.append(l.split('.', 1)[1].strip())
        elif order and (l.startswith('ATOM') or l.startswith('HETATM')):
            bs = i; break
    if bs is None:
        return None, None, None, None
    ci = {n: k for k, n in enumerate(order)}
    i_auth = ci['auth_asym_id']; i_ts = ci['type_symbol']
    prot = OrderedDict(); lig = OrderedDict()
    for l in lines[bs:]:
        if l.startswith('ATOM'):
            f = l.split()
            ch = strip_suffix(f[i_auth]) if protenix else f[i_auth]
            prot.setdefault(ch, None)
        elif l.startswith('HETATM'):
            f = l.split()
            if len(f) <= i_ts: continue
            if f[i_ts] in ('H', 'D'): continue
            ch = strip_suffix(f[i_auth]) if protenix else f[i_auth]
            lig[ch] = lig.get(ch, 0) + 1
    return list(prot.keys()), list(lig.items()), ci, bs

def match_instances(target, inst_meta, lig_chains):
    """inst_meta: {instance_chain: (ccd, smiles)} for scored instances of this (method,target).
    lig_chains: [(cif_chain, n_heavy), ...] in CIF order.
    Returns {instance_chain: cif_chain}. Order of assignment follows target-string ligand order."""
    seg = target.split('__')[-1]
    tlig = seg.split('_') if seg else []
    # only keep instances we actually have meta for (scored), preserve target order, then any extras
    ordered = [ic for ic in tlig if ic in inst_meta]
    for ic in inst_meta:
        if ic not in ordered:
            ordered.append(ic)
    used = [False] * len(lig_chains)
    res = {}
    for ic in ordered:
        ccd, smi = inst_meta[ic]
        want_n = heavy_count_from_smiles(smi) if smi else None
        placed = False
        if want_n is not None:
            for j, (ch, nat) in enumerate(lig_chains):
                if not used[j] and nat == want_n:
                    used[j] = True; res[ic] = ch; placed = True; break
        if not placed:
            for j, (ch, nat) in enumerate(lig_chains):
                if not used[j]:
                    used[j] = True; res[ic] = ch; placed = True; break
    return res
