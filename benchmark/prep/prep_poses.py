"""Data-prep for the co-folding pose viewer.

Key design decision (root-cause fix):
  - The REFERENCE crystal is served as the PRISTINE RCSB mmCIF file (untouched),
    so Mol*'s default representation preset renders it exactly like the PDB website
    (smooth cartoon + ligand ball-and-stick, clean coloring, native frame).
  - The AF3 POSES are reduced to LIGAND-ONLY structures, superposed into the
    crystal's coordinate frame by protein-Cα alignment, and written as simple
    3-column PDB files (HET renamed to "LIG"). A ligand-only structure renders
    reliably in Mol* regardless of how it was written, so this sidesteps the
    gemmi-rewritten-polymer cartoon failure entirely.

Outputs per system under viewer/systems/<ID>/:
  - xtal.cif        : pristine RCSB download (served with Mol* default preset)
  - pose-<n>.pdb    : ligand-only pose, aligned into crystal frame
And a regenerated systems.json with xtal_center (crystal-ligand centroid).

Source AF3 poses: ../verdict/data/poses/<ID>/model-<1..5>.cif
"""
import json
import math
import re
import glob
import urllib.request
import difflib
from pathlib import Path
import gemmi

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"
POSE_SRC = HERE.parent / "verdict" / "data" / "poses"
M3 = Path("/tmp/cameo_week/m3")
UA = {"User-Agent": "cofold-viewer/0.2"}


def af3_ligand_map(sid, het):
    """{model_no: (af3_chain, af3_resname)} for the AF3 ligand CAMEO matched to the
    system's target het, read from m3 ligand_pose.json. model_ligand_rmsd is e.g.
    "C.LIG_C1" => chain "C", residue "LIG_C" with seqid 1 in model-<n>.cif. Returns
    {} if no CAMEO match (non-A2A single-ligand systems -> caller falls back)."""
    out = {}
    for f in sorted(glob.glob(str(
            M3 / "modeling" / "*" / sid / "servers" / "server993" /
            "model-*" / "scores" / "ligand_pose.json"))):
        mdl = int(re.search(r"model-(\d+)", f).group(1))
        try:
            ligs = json.load(open(f))["results"]["details"]["ligand_pose"]["ligands"]
        except Exception:
            continue
        for k, v in ligs.items():
            if k.split(".")[-1].upper() != het:
                continue
            if v.get("rmsd") is None or not v.get("model_ligand_rmsd"):
                continue
            chain, resid = v["model_ligand_rmsd"].split(".", 1)
            m = re.match(r"(.*?)(\d+)$", resid)        # LIG_C1 -> resname LIG_C
            resname = m.group(1) if m else resid
            out[mdl] = (chain, resname)
            break
    return out


def af3_residue_by_id(model, chain_name, resname):
    """The AF3 residue matching chain name + residue name (exact). None if absent."""
    for ch in model:
        if ch.name != chain_name:
            continue
        for res in ch:
            if res.name == resname:
                return res
    return None


def fetch_xtal(pdb_id: str, dest: Path):
    """Pristine RCSB mmCIF (the exact file the PDB website renders). Reuse if
    build_systems.py already downloaded it."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest.stat().st_size
    url = f"https://files.rcsb.org/download/{pdb_id}.cif"
    data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()
    dest.write_bytes(data)
    return len(data)


def one_letter(poly):
    try:
        seq = poly.make_one_letter_sequence()
        if seq:
            return seq
    except Exception:
        pass
    out = []
    for r in poly:
        info = gemmi.find_tabulated_residue(r.name)
        out.append(info.one_letter_code.upper() if info else "X")
    return "".join(out)


def protein_polys(model):
    return [(ch.name, ch.get_polymer()) for ch in model if len(ch.get_polymer()) > 5]


def model_ligand_residue(model):
    """Return the main AF3 ligand residue (label_comp_id starts 'LIG', most atoms)."""
    best = None
    for ch in model:
        for res in ch:
            if res.name.startswith("LIG") and (best is None or len(res) > len(best)):
                best = res
    return best


def write_ligand_pdb(residue, dest: Path, resname="LIG", chain="X"):
    """Write a ligand-only PDB with a 3-char HET code, strict PDB column layout.

    Columns (1-indexed, PDB v3.3):
      1-6 record, 7-11 serial, 13-16 atom name, 17 altLoc, 18-20 resName,
      22 chainID, 23-26 resSeq, 31-38 x, 39-46 y, 47-54 z, 55-60 occ,
      61-66 tempFactor, 77-78 element.
    """
    lines = []
    serial = 0
    for atom in residue:
        serial += 1
        name = atom.name
        el = atom.element.name
        # Atom name field (cols 13-16). For single-letter elements with a name
        # shorter than 4 chars, the convention is a leading space.
        if len(name) >= 4:
            aname = name[:4]
        elif len(el) == 1:
            aname = (" " + name).ljust(4)
        else:
            aname = name.ljust(4)
        x, y, z = atom.pos.x, atom.pos.y, atom.pos.z
        line = (
            f"HETATM"           # 1-6
            f"{serial:>5d}"     # 7-11
            f" "                # 12
            f"{aname:<4s}"      # 13-16 atom name
            f" "                # 17 altLoc
            f"{resname:>3s}"    # 18-20 resName
            f" "                # 21
            f"{chain:1s}"       # 22 chainID
            f"{1:>4d}"          # 23-26 resSeq
            f"    "             # 27-30 (iCode + pad)
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}"  # 31-54
            f"{1.0:>6.2f}{0.0:>6.2f}"        # 55-66
            f"          "       # 67-76
            f"{el:>2s}"         # 77-78 element
        )
        lines.append(line)
    lines.append("END")
    dest.write_text("\n".join(lines) + "\n")
    return serial


def write_protein_pdb(model, dest: Path, only_chain=None):
    """Write the aligned AF3 protein (polymer only) as PDB. Standard residues +
    PDB format => Mol* computes secondary structure and renders smooth CARTOON
    (unlike gemmi-written mmCIF, which Mol* fails to classify as polymer).
    If only_chain is given, write just that chain — for multi-chain assemblies we
    only show the pocket chain, since AF3 can pack the other chains differently
    (e.g. 13JK: each chain fits <1Å but the dimer arrangement differs by ~6Å)."""
    out = gemmi.Structure()
    om = gemmi.Model("1")
    for ch in model:
        if only_chain is not None and ch.name != only_chain:
            continue
        poly = ch.get_polymer()
        if len(poly) < 5:
            continue
        nc = gemmi.Chain(ch.name)
        for res in poly:
            nc.add_residue(res)
        om.add_chain(nc)
    out.add_model(om)
    out.setup_entities()
    out.write_pdb(str(dest))


def pocket_chain(model, lig_res):
    """The protein chain whose pocket actually holds the ligand (most Cα within 10Å)."""
    ligpos = [a.pos for a in lig_res]
    best, bestcnt = None, -1
    for ch in model:
        if len(ch.get_polymer()) < 5:
            continue
        cnt = 0
        for res in ch.get_polymer():
            for at in res:
                if at.name == "CA":
                    if any(at.pos.dist(lp) < 10.0 for lp in ligpos):
                        cnt += 1
                    break
        if cnt > bestcnt:
            bestcnt, best = cnt, ch
    return best


def centroid_pos(res):
    n = len(res)
    return gemmi.Position(sum(a.pos.x for a in res) / n,
                          sum(a.pos.y for a in res) / n,
                          sum(a.pos.z for a in res) / n)


def transformed_centroid(res, tr):
    pts = [tr.apply(a.pos) for a in res]
    n = len(pts)
    return gemmi.Position(sum(p.x for p in pts) / n,
                          sum(p.y for p in pts) / n,
                          sum(p.z for p in pts) / n)


def ligand_centroid(residue):
    n = len(residue)
    cx = sum(a.pos.x for a in residue) / n
    cy = sum(a.pos.y for a in residue) / n
    cz = sum(a.pos.z for a in residue) / n
    return [round(cx, 2), round(cy, 2), round(cz, 2)]


def main():
    manifest = json.loads((HERE / "systems.json").read_text())
    for s in manifest["systems"]:
        sid, het = s["id"], s["ligand"]
        sysdir = SYS / sid
        sysdir.mkdir(parents=True, exist_ok=True)

        # 1) pristine crystal --------------------------------------------------
        xtal = sysdir / "xtal.cif"
        nbytes = fetch_xtal(sid, xtal)
        xs = gemmi.read_structure(str(xtal))
        xs.setup_entities()
        xmodel = xs[0]
        xpolys = protein_polys(xmodel)
        # all crystal copies of the ground-truth ligand (centroids) — for picking
        # the chain mapping that minimizes ligand displacement.
        xlig_centroids = [centroid_pos(res) for ch in xmodel for res in ch if res.name == het]

        s["xtal_file"] = f"systems/{sid}/xtal.cif"
        # camera-focus center: set below from the pose-1 ligand centroid, which
        # robustly points at the binding site AF3 modeled (crystals can have
        # multiple copies / two binding sites, e.g. 21KW).

        # CAMEO-matched AF3 ligand to extract for display: {model: (chain, resname)}.
        # CAMEO records which AF3 ligand it scored against the crystal target het;
        # we extract THAT ligand (the correct fragment) instead of the largest LIG
        # residue (which wrongly grabbed cholesterol for the A2A membrane co-folds).
        # Empty for non-A2A single-ligand systems -> fall back to largest-LIG pick.
        af3map = af3_ligand_map(sid, het)

        # 2) aligned ligand-only poses ----------------------------------------
        for p in s["poses"]:
            src = POSE_SRC / sid / f"model-{p['sample']}.cif"
            ms = gemmi.read_structure(str(src))
            ms.setup_entities()
            mmodel = ms[0]

            # Pick the AF3 ligand CAMEO matched to the target het for this model.
            # Map is verified stable across models above; for the few systems where
            # it varies we use the per-model entry. Fall back to the largest-LIG
            # residue when there's no CAMEO match (non-A2A single-ligand systems).
            lig = None
            cr = af3map.get(p["sample"])
            if cr is not None:
                lig = af3_residue_by_id(mmodel, cr[0], cr[1])
            if lig is None:
                lig = model_ligand_residue(mmodel)
            if lig is None:
                raise RuntimeError(f"{sid} model-{p['sample']}: no LIG residue found")

            # Align IGNORING CHAIN NAMES: superpose the model's pocket chain onto
            # EVERY sequence-matching crystal chain, and keep the mapping that puts
            # the predicted ligand closest to a crystal ligand copy (i.e. minimizes
            # ligand displacement). This is the symmetry-aware choice and reproduces
            # how CAMEO scores ligand RMSD — for a correct pose some mapping overlaps
            # the truth; for a wrong pose none does.
            pchain = pocket_chain(mmodel, lig)
            p["_aligned"] = False
            if pchain is not None and xpolys:
                mpoly = pchain.get_polymer()
                mseq = one_letter(mpoly)
                cands = [(xn, xp) for xn, xp in xpolys
                         if difflib.SequenceMatcher(None, one_letter(xp), mseq).ratio() > 0.5]
                best = None  # (lig_dist, sup, xname)
                for xn, xp in (cands or xpolys):
                    sup = gemmi.calculate_superposition(
                        xp, mpoly, gemmi.PolymerType.PeptideL, gemmi.SupSelect.CaP
                    )
                    if not math.isfinite(sup.rmsd):        # degenerate superposition — skip
                        continue
                    lc = transformed_centroid(lig, sup.transform)
                    d = min((lc.dist(xc) for xc in xlig_centroids), default=0.0)
                    if best is None or d < best[0]:
                        best = (d, sup, xn)
                if best is not None:
                    _, sup, used = best
                    mmodel.transform_pos_and_adp(sup.transform)   # moves protein + ligand together
                    p["align_rmsd"] = round(sup.rmsd, 2)
                    p["align_n"] = sup.count
                    p["lig_match_dist"] = round(best[0], 2)
                    p["_aligned"] = True

            pdb = sysdir / f"pose-{p['sample']}.pdb"
            natoms = write_ligand_pdb(lig, pdb)
            p["pose_file"] = f"systems/{sid}/pose-{p['sample']}.pdb"
            p["pose_atoms"] = natoms
            # aligned AF3 protein — pocket chain only — for the optional toggle.
            prot = sysdir / f"protein-{p['sample']}.pdb"
            write_protein_pdb(mmodel, prot, only_chain=(pchain.name if pchain else None))
            p["protein_file"] = f"systems/{sid}/protein-{p['sample']}.pdb"
            if p["sample"] == 1:
                s["xtal_center"] = ligand_centroid(lig)
            # drop stale keys from the old gemmi-rewritten approach
            p.pop("aligned_file", None)
            p.pop("file", None)  # old pointer to a model CIF no longer copied here

        print(
            f"{sid} ({het}): xtal {nbytes//1024}KB · "
            f"Ca-RMSD {[p.get('align_rmsd') for p in s['poses']]} · "
            f"pose atoms {[p.get('pose_atoms') for p in s['poses']]}"
        )

    # Keep only systems that aligned (≥1 usable pose); strip helper key; never emit NaN.
    def denan(o):
        if isinstance(o, float):
            return None if not math.isfinite(o) else o
        if isinstance(o, dict):
            return {k: denan(v) for k, v in o.items()}
        if isinstance(o, list):
            return [denan(v) for v in o]
        return o
    good = []
    for s in manifest["systems"]:
        if any(p.get("_aligned") for p in s["poses"]):
            for p in s["poses"]:
                p.pop("_aligned", None)
            good.append(s)
        else:
            print(f"DROP {s['id']} ({s['ligand']}): alignment failed (degenerate superposition)")
    manifest["systems"] = denan(good)
    (HERE / "systems.json").write_text(json.dumps(manifest, indent=2, allow_nan=False))
    print(f"wrote systems.json ({len(good)} systems)")


if __name__ == "__main__":
    main()
