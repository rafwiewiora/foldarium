"""Align every AF3 pose to the experimental crystal structure, for an overlaid view.

For each system:
  1. download the real crystal from RCSB (these CAMEO targets are now released PDB entries),
  2. clean it to {protein + the one ground-truth ligand},
  3. superpose each of AF3's 5 pose models onto the crystal by protein Cα (gemmi, sequence-aware),
  4. write the aligned full model (so the viewer can show just its ligand in the crystal frame),
  5. record the crystal-ligand centroid so the viewer can focus the camera on the pocket.

Output: cleaned crystal + 5 aligned pose CIFs per system, and an updated systems.json.
"""
import json, urllib.request, difflib
from pathlib import Path
import gemmi

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"
UA = {"User-Agent": "cofold-viewer/0.1"}


def fetch_xtal(pdb_id: str, dest: Path):
    if dest.exists():
        return
    url = f"https://files.rcsb.org/download/{pdb_id}.cif"
    dest.write_bytes(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read())


def one_letter(poly):
    try:
        return poly.make_one_letter_sequence()
    except Exception:
        return "".join(gemmi.find_tabulated_residue(r.name).one_letter_code.upper()
                       if gemmi.find_tabulated_residue(r.name) else "X" for r in poly)


def main():
    manifest = json.loads((HERE / "systems.json").read_text())
    for s in manifest["systems"]:
        sid, het = s["id"], s["ligand"]
        sysdir = SYS / sid
        xtal_raw = sysdir / "xtal_raw.cif"
        fetch_xtal(sid, xtal_raw)

        xs = gemmi.read_structure(str(xtal_raw)); xs.setup_entities()
        xmodel = xs[0]
        # crystal protein chains + their sequences
        xpolys = [(ch.name, ch.get_polymer()) for ch in xmodel if len(ch.get_polymer()) > 5]

        # --- clean crystal: keep polymers + the target HET only ---
        clean = gemmi.Structure(); clean.cell = xs.cell; clean.spacegroup_hm = xs.spacegroup_hm
        cm = gemmi.Model("1")
        lig_atoms = []
        for ch in xmodel:
            nc = gemmi.Chain(ch.name); keep = False
            for res in ch.get_polymer():          # protein/nucleic residues
                nc.add_residue(res); keep = True
            for res in ch:                          # the ground-truth ligand
                if res.name == het:
                    nc.add_residue(res); keep = True
                    lig_atoms += [(a.pos.x, a.pos.y, a.pos.z) for a in res]
            if keep:
                cm.add_chain(nc)
        clean.add_model(cm); clean.setup_entities()
        clean.make_mmcif_document().write_file(str(sysdir / "xtal.cif"))
        if lig_atoms:
            n = len(lig_atoms)
            cen = [sum(c[i] for c in lig_atoms) / n for i in range(3)]
            s["xtal_center"] = [round(v, 2) for v in cen]
        s["xtal_file"] = f"systems/{sid}/xtal.cif"

        # --- superpose each pose model onto the crystal by protein Cα ---
        for p in s["poses"]:
            mpath = sysdir / Path(p["file"]).name
            ms = gemmi.read_structure(str(mpath)); ms.setup_entities()
            mmodel = ms[0]
            mpolys = [(ch.name, ch.get_polymer()) for ch in mmodel if len(ch.get_polymer()) > 5]
            if not mpolys or not xpolys:
                p["aligned_file"] = p["file"]; continue
            # model's longest protein chain
            mname, mpoly = max(mpolys, key=lambda x: len(x[1]))
            mseq = one_letter(mpoly)
            # best-matching crystal chain by sequence identity
            xname, xpoly = max(xpolys, key=lambda x: difflib.SequenceMatcher(None, one_letter(x[1]), mseq).ratio())
            sup = gemmi.calculate_superposition(xpoly, mpoly, gemmi.PolymerType.PeptideL, gemmi.SupSelect.CaP)
            mmodel.transform_pos_and_adp(sup.transform)
            ms.setup_entities()
            ms.make_mmcif_document().write_file(str(sysdir / f"aligned-{p['sample']}.cif"))
            p["aligned_file"] = f"systems/{sid}/aligned-{p['sample']}.cif"
            p["align_rmsd"] = round(sup.rmsd, 2); p["align_n"] = sup.count
        print(f"{sid} ({het}): xtal + {len(s['poses'])} aligned poses · "
              f"Cα-RMSD {[p.get('align_rmsd') for p in s['poses']]}")

    (HERE / "systems.json").write_text(json.dumps(manifest, indent=2))
    print("updated systems.json")


if __name__ == "__main__":
    main()
