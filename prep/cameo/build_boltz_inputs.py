"""Prepare Boltz-2 input YAMLs for the same ~228 quiz targets we ran AF3 on.

For each quiz item we need, per Boltz-2 YAML:
  - protein sequence(s), one per folded chain  -> taken from the AF3 model-1.cif
    (the EXACT construct AF3 folded, incl. His-tag/TEV, so Boltz-2 folds the
    identical sequence -> apples-to-apples comparison).
  - the ligand: prefer `ccd: <CODE>` only for codes in the standard CCD shipped
    with tools; for everything else (incl. novel A1*-style CAMEO codes) resolve a
    SMILES from the RCSB ligand definition endpoint
    (https://files.rcsb.org/ligands/download/<CODE>.cif), which carries SMILES even
    for freshly-deposited codes, and emit `smiles:`.

Writes quiz/boltz_inputs/<id>.yaml. Unresolvable ligands -> _unresolved.txt.
Does NOT run Boltz-2. Read-only w.r.t. CAMEO/RCSB (only downloads ligand defs).
"""
import json, sys, re, urllib.request
from pathlib import Path
import gemmi

HERE = Path(__file__).resolve().parent
CAMEO = Path("/Users/rafalwiewiora/cameo_data/extracted/modeling")
OUT = HERE / "boltz_inputs"
LIGCACHE = OUT / "_ligcache"
UA = {"User-Agent": "Mozilla/5.0 (boltz-prep)"}

# A small, well-known set of standard CCD codes is fine to pass as ccd:, but to be
# safe and uniform we resolve a SMILES for EVERY ligand from the RCSB ligand def.
# (Boltz-2 ships the standard CCD, so 3-letter classic codes could use ccd:, but
#  SMILES is universally accepted and avoids per-tool CCD-version mismatches.)


def af3_protein_seqs(week, tgt):
    """[(chain_id, one_letter_seq)] from the target's AF3 model-1.cif (the folded construct)."""
    src = CAMEO / week / tgt / "servers" / "server993" / "model-1" / "model-1.cif"
    if not src.exists():
        return None
    s = gemmi.read_structure(str(src))
    s.setup_entities()
    out = []
    for ch in s[0]:
        poly = ch.get_polymer()
        if len(poly) > 5:
            seq = poly.make_one_letter_sequence()
            if seq:
                # gemmi emits '(XXX)' for unrecognised residues and a LOWERCASE
                # parent-letter for modified standard residues (e.g. modified Cys
                # -> 'c'). Drop bracketed unknowns and uppercase so Boltz-2 sees
                # only standard 1-letter codes (the modified residue folds as its
                # standard parent — same construct AF3 effectively folded).
                seq = re.sub(r"\([^)]*\)", "", seq).upper()
                out.append((ch.name, seq))
    return out


def fetch_ligand_smiles(code):
    """SMILES for a CCD code from the RCSB ligand cif. Returns (smiles, source) or (None, reason)."""
    cif = LIGCACHE / f"{code}.cif"
    if cif.exists() and cif.stat().st_size > 0:
        data = cif.read_bytes()
    else:
        url = f"https://files.rcsb.org/ligands/download/{code}.cif"
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read()
        except Exception as e:
            return None, f"fetch_fail:{type(e).__name__}"
        cif.write_bytes(data)
    try:
        b = gemmi.cif.read_string(data.decode()).sole_block()
    except Exception as e:
        return None, f"parse_fail:{type(e).__name__}"
    t = b.find("_pdbx_chem_comp_descriptor.", ["comp_id", "type", "program", "descriptor"])
    cands = [(r.str(1), r.str(2), r.str(3)) for r in t if "SMILES" in r.str(1)]
    if not cands:
        return None, "no_smiles_in_def"
    # priority: OpenEye SMILES_CANONICAL > any SMILES_CANONICAL > any SMILES
    for typ, prog, desc in cands:
        if typ == "SMILES_CANONICAL" and "OpenEye" in prog:
            return desc, "rcsb_ligand_def(OE_canonical)"
    for typ, prog, desc in cands:
        if typ == "SMILES_CANONICAL":
            return desc, "rcsb_ligand_def(canonical)"
    return cands[0][2], "rcsb_ligand_def(smiles)"


def write_yaml(path, prot_seqs, lig_code, smiles):
    chain_ids = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    lines = ["version: 1", "sequences:"]
    used = []
    for i, (_, seq) in enumerate(prot_seqs):
        cid = chain_ids[i]
        used.append(cid)
        lines.append(f"  - protein:")
        lines.append(f"      id: {cid}")
        lines.append(f"      sequence: {seq}")
    lig_id = chain_ids[len(prot_seqs)]
    lines.append("  - ligand:")
    lines.append(f"      id: {lig_id}")
    if smiles is not None:
        lines.append(f"      smiles: '{smiles}'")
    else:
        lines.append(f"      ccd: {lig_code}")
    path.write_text("\n".join(lines) + "\n")


def main():
    OUT.mkdir(exist_ok=True)
    LIGCACHE.mkdir(exist_ok=True)
    items = json.load(open(HERE / "quiz_items.json"))["items"]
    print(f"{len(items)} quiz items")

    written, unresolved = [], []
    std_codes, novel_codes = [], []   # by code-length heuristic, for reporting
    smiles_ok, smiles_fail = 0, 0
    no_protein = []

    for it in items:
        tgt, week, code = it["id"], it["week"], it["ligand"]
        # report bucket: novel CAMEO codes are 5-char (A1xxx); classic CCD are <=3
        (novel_codes if len(code) == 5 else std_codes).append(code)

        prot = af3_protein_seqs(week, tgt)
        if not prot:
            no_protein.append((tgt, code))
            unresolved.append(f"{tgt}\t{code}\tno_protein_in_af3_model")
            continue

        smiles, src = fetch_ligand_smiles(code)
        if smiles is None:
            smiles_fail += 1
            unresolved.append(f"{tgt}\t{code}\tligand:{src}")
            continue
        smiles_ok += 1
        write_yaml(OUT / f"{tgt}.yaml", prot, code, smiles)
        written.append((tgt, code, len(prot), src))

    (OUT / "_unresolved.txt").write_text("\n".join(unresolved) + ("\n" if unresolved else ""))

    print(f"\nwritten YAMLs: {len(written)}")
    print(f"unresolved:    {len(unresolved)}")
    print(f"  no protein in AF3 model: {len(no_protein)}")
    print(f"  ligand SMILES fail:      {smiles_fail}")
    print(f"\nligand code buckets (by code form):")
    print(f"  classic CCD (<=3 char): {len(std_codes)}  (unique {len(set(std_codes))})")
    print(f"  novel A1*-style (5 char): {len(novel_codes)}  (unique {len(set(novel_codes))})")
    print(f"  SMILES resolved: {smiles_ok}/{len(items)}")
    # how many wrote >1 protein chain
    multi = sum(1 for _, _, n, _ in written if n > 1)
    print(f"  multi-chain YAMLs: {multi}")


if __name__ == "__main__":
    main()
