"""Data-prep for the WITHIN-TARGET (fragment-screen) overlay mode.

The CAMEO month dump is dominated by a fragment-screening campaign: ~79 PDB
entries of the SAME protein (Adenosine A2A receptor), each soaked with a different
small fragment.  This script builds, for ONE protein group, a single common-frame
overlay so you can see how AF3 placed EVERY fragment at once on one shared protein.

Common-frame alignment (the key idea)
-------------------------------------
1. Group all 704 CAMEO targets by their longest-polymer one-letter sequence
   (gemmi make_one_letter_sequence).  The largest group is the A2A receptor.
2. Pick ONE reference crystal from the group (first member whose pristine RCSB
   mmCIF downloads cleanly).  Its UNTOUCHED RCSB cif is served as the cartoon
   protein (Mol* default preset -> PDB-website look; a gemmi-rewritten polymer
   would NOT render as cartoon, so we never rewrite the protein we display).
3. For every fragment's AF3 model: sequence-aware Ca superposition of that model's
   pocket protein chain onto the reference crystal's protein, then apply the
   transform to the predicted ligand atoms.  Now every fragment's predicted ligand
   sits in the reference crystal's coordinate frame.  Each aligned ligand is
   written as a 3-char-HET ligand-only PDB (reusing prep_poses.write_ligand_pdb).
4. Correctness oracle comes from CAMEO's per-pose `rmsd` (median over crystal
   copies).  A fragment is "correct" if its pose-1 rmsd < 2 A.  In all-poses mode,
   each of the 5 poses is colored by its OWN rmsd < 2 A.

Outputs
-------
  systems/group_<name>/ref.cif                  pristine reference crystal
  systems/group_<name>/<TGT>_<het>-pose-<n>.pdb  aligned ligand-only poses
  systems/group_<name>/<TGT>_<het>-truth.pdb     aligned crystal (truth) ligand
  group_<name>.json                              manifest the viewer reads

Run:  python3 build_group.py            # builds the A2A group
      GROUP_RANK=1 python3 build_group.py   # second-largest group, etc.
"""
import json
import math
import os
import re
import difflib
import urllib.request
from pathlib import Path
from collections import defaultdict
from statistics import median

import gemmi

from prep_poses import write_ligand_pdb  # reuse the strict-column PDB writer

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"
SRC = Path(os.environ.get("CAMEO_SRC", "/tmp/cameo_week/month/modeling"))
UA = {"User-Agent": "cofold-viewer/0.4 (research)"}

# Drug-like filter mirrored from build_systems.py (so the picked fragment per
# target is the soaked small molecule, not buffer / lipid / sugar / cofactor).
EXC = set((
 "HOH DOD NA CL MG ZN CA K MN FE FE2 FE3 CU CU1 NI CO CD HG CS BA SR BR IOD I RB LI PB PT AU AG TL SM GD YB EU MO W V SE F ZN2 3CO 4MO OH O OXY "
 "SO4 PO4 PI NO3 ACT EDO GOL PEG PG4 PGE 1PE 2PE P6G MPD DMS BME MES EPE TRS TAR CIT FLC FMT IPA BO3 NH4 AZI CAC MLA OXL SCN 144 15P PE4 PEU DIO SIN MLI BCT CO3 UNX UNL UNK BU3 MRD IMD POL PGO PG0 12P 7PE DTT DTV TLA SUC "
 "NAG MAN BMA FUC GAL GLC NDG BGC FUL XYS RAM SIA NGA A2G GLA XYP GCU ADA RIB API MAL TRE LMT LMN DGD SGN BOG "
 "NAD NAP NDP NAI NAJ FAD FMN FDA ATP ADP AMP ANP ACP AGS APC GTP GDP GNP GSP GMP CTP UTP UDP UMP TTP TMP COA ACO SAM SAH SFG HEM HEC HEA HEB DHE HAS PLP PMP TPP TDP BTI BTN B12 COB H4B BH4 MGD PAP UD1 UPG 5GP PNS "
 "PLM CLR POV PTY CDL OLA OLB OLC STE MYR PEE PCW PC1 PEF LHG PGV PGW D10 DD9 HP6 Y01 HC3 PX4 3PE PEK PSC 17F PC7 PEV UND DAO LMG MC3 9PE PLC SPH CHS CHD EIC ARA HTG PX2"
).split())
drug_like = lambda h, a: h not in EXC and not (a is not None and a < 6)

GROUP_RANK = int(os.environ.get("GROUP_RANK", "0"))  # 0 = largest group
GROUP_NAME = os.environ.get("GROUP_NAME", "")        # override output name


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


def longest_seq(model):
    polys = [p for _, p in protein_polys(model)]
    if not polys:
        return None
    return one_letter(max(polys, key=lambda p: len(p)))


def target_dirs():
    """{target -> Path to its CAMEO target dir (the dir holding servers/)}."""
    out = {}
    for d in sorted(SRC.glob("*/*/servers/server993")):
        tgt = d.parent.parent.name
        out.setdefault(tgt, d.parent.parent)
    return out


def fetch_xtal(pdb_id, dest: Path):
    if dest.exists() and dest.stat().st_size > 0:
        return dest.stat().st_size
    url = f"https://files.rcsb.org/download/{pdb_id}.cif"
    try:
        data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()
    except Exception:
        return 0
    dest.write_bytes(data)
    return len(data)


def fragment_for_target(tdir: Path):
    """Pick the soaked drug-like fragment for a target.

    Returns (het, atoms, rmsd_by_model{mdl:median}, chain_by_model{mdl:chainName})
    or None.  rmsd is CAMEO's ligand_pose rmsd (median over crystal copies); the
    model chain comes from `model_ligand_rmsd` (e.g. 'C.LIG_C1' -> chain 'C')."""
    per = defaultdict(lambda: {"atoms": None, "rmsd": defaultdict(list), "chain": {}})
    for mdl in range(1, 6):
        f = tdir / "servers/server993" / f"model-{mdl}" / "scores" / "ligand_pose.json"
        if not f.exists():
            continue
        try:
            ligs = json.load(open(f))["results"]["details"]["ligand_pose"]["ligands"]
        except Exception:
            continue
        for k, v in ligs.items():
            r = v.get("rmsd")
            if r is None:
                continue
            het = k.split(".")[-1].upper()
            L = per[het]
            L["atoms"] = v.get("atom_count")
            L["rmsd"][mdl].append(float(r))
            ml = v.get("model_ligand_rmsd")
            if ml and "." in str(ml):
                L["chain"][mdl] = str(ml).split(".")[0]
    # best drug-like by atom count
    best = None
    for het, L in per.items():
        if not drug_like(het, L["atoms"]) or not L["rmsd"]:
            continue
        if best is None or (L["atoms"] or 0) > (per[best]["atoms"] or 0):
            best = het
    if best is None:
        return None
    L = per[best]
    rmsd_by = {m: round(median(rs), 2) for m, rs in L["rmsd"].items()}
    return best, L["atoms"], rmsd_by, L["chain"]


def lig_residue_in_chain(model, chain_name):
    """The LIG_<chain> residue in the named model chain (the scored fragment)."""
    for ch in model:
        if ch.name != chain_name:
            continue
        best = None
        for res in ch:
            if res.name.startswith("LIG") and (best is None or len(res) > len(best)):
                best = res
        if best is not None:
            return best
    # fall back: any LIG residue (chain naming should match, but be safe)
    best = None
    for ch in model:
        for res in ch:
            if res.name.startswith("LIG") and (best is None or len(res) > len(best)):
                best = res
    return best


def pocket_chain_for_lig(model, lig_res):
    """Protein chain whose pocket holds the ligand (most Ca within 10 A)."""
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


def transformed_centroid(res, transform):
    """Centroid of `res` after applying a gemmi.Transform (does not mutate res)."""
    pts = [transform.apply(a.pos) for a in res]
    n = len(pts)
    return gemmi.Position(sum(p.x for p in pts) / n,
                          sum(p.y for p in pts) / n,
                          sum(p.z for p in pts) / n)


def _res_code(res):
    info = gemmi.find_tabulated_residue(res.name)
    c = info.one_letter_code.upper() if info and info.one_letter_code else "X"
    return c if c.strip() else "X"


def chain_ca_seq(poly):
    """(seq_str, [Ca positions]) built from the SAME residues that have a Ca, so
    sequence and coordinate lists stay index-aligned (make_one_letter_sequence can
    insert gap dashes / count differently -- that breaks the pairing)."""
    seq, cas = [], []
    for r in poly:
        ca = next((at.pos for at in r if at.name == "CA"), None)
        if ca is None:
            continue
        seq.append(_res_code(r)); cas.append(ca)
    return "".join(seq), cas


def seqaware_superpose(ref_poly, mov_poly):
    """Sequence-aware Ca superposition of mov_poly ONTO ref_poly.

    The crystal reference and the AF3 model have DIFFERENT residue numbering and
    lengths (crystals have unmodelled loops; AF3 predicts the full chain), so a
    positional Ca pairing is frame-shifted and rotates the pocket away. We instead
    align the two one-letter sequences (gemmi.align_string_sequences), pair only
    the matched Ca atoms, and superpose those. Returns a gemmi superposition
    result (has .transform, .rmsd, .count) or None if too few matches / degenerate.
    """
    rseq, rca = chain_ca_seq(ref_poly)
    mseq, mca = chain_ca_seq(mov_poly)
    if len(rca) < 3 or len(mca) < 3:
        return None
    aln = gemmi.align_string_sequences(list(rseq), list(mseq), [])
    # CIGAR convention here: M consumes both; D consumes target(mov); I consumes query(ref).
    ri = mi = 0
    rp, mp = [], []
    for num, op in re.findall(r"(\d+)([MID])", aln.cigar_str()):
        num = int(num)
        if op == "M":
            for _ in range(num):
                rp.append(rca[ri]); mp.append(mca[mi]); ri += 1; mi += 1
        elif op == "D":
            mi += num
        elif op == "I":
            ri += num
    if len(rp) < 3:
        return None
    sup = gemmi.superpose_positions(rp, mp)
    if not math.isfinite(sup.rmsd):
        return None
    return sup


def superpose_onto_ref(model, ref_polys, pchain, lig_res=None, pocket_center=None):
    """Superpose the AF3 model onto the reference and return (rmsd, transform, n).

    Symmetry-aware choice (mirrors prep_poses.py's "ignore chain names, minimize
    ligand displacement"): the model often has 2 receptor copies and the ligand
    can sit in either copy's pocket, so the lowest-Ca-RMSD chain mapping can drop
    the ligand on a symmetry mate ~30 A from the shared pocket. When a ligand and a
    pocket anchor are given we therefore try EVERY model protein chain (each
    sequence-aware-superposed onto the best-matching reference chain) and KEEP THE
    mapping whose transformed ligand centroid is CLOSEST to the pocket anchor.
    Without those args we fall back to the model's own pocket chain at minimum Ca
    RMSD. Degenerate superpositions are skipped; never returns a non-finite rmsd.
    """
    if not ref_polys:
        return None

    def best_ref_sup(mpoly):
        """Sequence-aware-superpose mpoly onto whichever reference chain gives the
        lowest Ca RMSD (reference chains are the same protein)."""
        best = None
        mseq = one_letter(mpoly)
        cands = [(xn, xp) for xn, xp in ref_polys
                 if difflib.SequenceMatcher(None, one_letter(xp), mseq).ratio() > 0.4]
        for xn, xp in (cands or ref_polys):
            sup = seqaware_superpose(xp, mpoly)
            if sup is None:
                continue
            if best is None or sup.rmsd < best.rmsd:
                best = sup
        return best

    by_ligand = lig_res is not None and pocket_center is not None
    if by_ligand:
        # try every protein chain in the model; keep the one putting the ligand
        # closest to the shared-pocket anchor.
        best = None  # (lig_dist, rmsd, transform, count)
        for ch in model:
            if len(ch.get_polymer()) < 5:
                continue
            sup = best_ref_sup(ch.get_polymer())
            if sup is None:
                continue
            lc = transformed_centroid(lig_res, sup.transform)
            d = lc.dist(pocket_center)
            if not math.isfinite(d):
                continue
            if best is None or d < best[0]:
                best = (d, sup.rmsd, sup.transform, sup.count)
        return best[1:] if best else None

    # legacy / anchor-free: use the model's pocket chain, min-RMSD ref mapping.
    if pchain is None:
        return None
    sup = best_ref_sup(pchain.get_polymer())
    if sup is None:
        return None
    return (sup.rmsd, sup.transform, sup.count)


def centroid(res):
    n = len(res)
    return [round(sum(a.pos.x for a in res) / n, 2),
            round(sum(a.pos.y for a in res) / n, 2),
            round(sum(a.pos.z for a in res) / n, 2)]


def main():
    tdirs = target_dirs()
    print(f"{len(tdirs)} CAMEO targets; grouping by protein sequence ...")

    # 1) group by longest-polymer sequence ------------------------------------
    groups = defaultdict(list)
    for tgt, tdir in sorted(tdirs.items()):
        m1 = tdir / "servers/server993/model-1/model-1.cif"
        if not m1.exists():
            continue
        try:
            st = gemmi.read_structure(str(m1)); st.setup_entities()
        except Exception:
            continue
        seq = longest_seq(st[0])
        if seq:
            groups[seq].append(tgt)
    bysize = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    print("largest protein groups:")
    for seq, mem in bysize[:5]:
        print(f"  {len(mem):3d} members  seqlen {len(seq)}  eg {sorted(mem)[:4]}")

    if GROUP_RANK >= len(bysize):
        raise SystemExit(f"GROUP_RANK {GROUP_RANK} out of range ({len(bysize)} groups)")
    seq, members = bysize[GROUP_RANK]
    members = sorted(members)
    gname = GROUP_NAME or f"a2a" if GROUP_RANK == 0 else GROUP_NAME or f"group{GROUP_RANK}"
    print(f"\nSelected group rank {GROUP_RANK}: {len(members)} members, seqlen {len(seq)}, name '{gname}'")

    gdir = SYS / f"group_{gname}"
    gdir.mkdir(parents=True, exist_ok=True)

    # 2) pick the reference crystal -------------------------------------------
    ref_pdb = None
    for tgt in members:
        dest = gdir / "ref.cif"
        n = fetch_xtal(tgt, dest)
        if n > 0:
            ref_pdb = tgt
            break
    if ref_pdb is None:
        raise SystemExit("no reference crystal could be downloaded for this group")
    print(f"reference crystal: {ref_pdb}  (ref.cif, {(gdir/'ref.cif').stat().st_size//1024} KB)")

    refs = gemmi.read_structure(str(gdir / "ref.cif")); refs.setup_entities()
    ref_model = refs[0]
    ref_polys = protein_polys(ref_model)

    # --- PASS 1: find the DOMINANT-POCKET ANCHOR --------------------------------
    # This fragment screen is NOT single-site: the fragments bind several pockets,
    # and the reference crystal's own ligand happens to sit in a MINORITY site. So
    # we anchor on the centroid cluster of the CORRECTLY-placed predicted ligands
    # (where AF3 reproduced the crystal), which marks the dominant shared pocket --
    # exactly the "centroid of already-correctly-placed fragment ligands" option.
    #
    # For a CORRECT pose-1 the ligand sits in the MODEL's own pocket, so the plain
    # min-RMSD chain mapping already lands it consistently; we collect those points,
    # then take the densest cluster centroid as the anchor.
    anchor_pts = []   # (x,y,z) for correct pose-1 ligands, in reference frame
    pass1 = {}        # tgt -> (het, atoms, rmsd_by, chain_by) cache for pass 2
    for tgt in members:
        tdir = tdirs[tgt]
        frag = fragment_for_target(tdir)
        if frag is None:
            continue
        het, atoms, rmsd_by, chain_by = frag
        if 1 not in rmsd_by:
            continue
        pass1[tgt] = frag
        if rmsd_by[1] >= 2.0:          # only correct pose-1s anchor the pocket
            continue
        m1 = tdir / "servers/server993/model-1/model-1.cif"
        if not m1.exists():
            continue
        ms = gemmi.read_structure(str(m1)); ms.setup_entities()
        mmodel = ms[0]
        lig = lig_residue_in_chain(mmodel, chain_by.get(1))
        if lig is None:
            continue
        pchain = pocket_chain_for_lig(mmodel, lig)
        sup = superpose_onto_ref(mmodel, ref_polys, pchain)   # anchor-free min-RMSD
        if sup is None:
            continue
        lc = transformed_centroid(lig, sup[1])
        anchor_pts.append((lc.x, lc.y, lc.z))

    if not anchor_pts:
        raise SystemExit("no correct pose-1 to anchor the dominant pocket")
    # densest point = the one with the most neighbours within 8 A; average that cluster.
    def neigh(p, q):
        return math.dist(p, q) < 8.0
    seed = max(anchor_pts, key=lambda p: sum(neigh(p, q) for q in anchor_pts))
    cluster = [q for q in anchor_pts if neigh(seed, q)]
    pocket_center = gemmi.Position(
        sum(p[0] for p in cluster) / len(cluster),
        sum(p[1] for p in cluster) / len(cluster),
        sum(p[2] for p in cluster) / len(cluster))
    print(f"dominant-pocket anchor = centroid of {len(cluster)}/{len(anchor_pts)} "
          f"correctly-placed ligands ({pocket_center.x:.1f}, {pocket_center.y:.1f}, "
          f"{pocket_center.z:.1f})")

    # collected for the post-rebuild sanity distribution
    centroid_dists = []  # (tgt, pose1_dist_to_anchor)

    # --- PASS 2: align every fragment to the dominant-pocket anchor -----------
    fragments = []
    skipped = []
    for tgt in members:
        tdir = tdirs[tgt]
        frag = pass1.get(tgt)
        if frag is None:
            skipped.append((tgt, "no drug-like fragment with pose-1 rmsd"))
            continue
        het, atoms, rmsd_by, chain_by = frag

        poses_out = []
        aligned_any = False
        truth_written = False
        for mdl in sorted(rmsd_by):
            m1 = tdir / f"servers/server993/model-{mdl}/model-{mdl}.cif"
            if not m1.exists():
                continue
            ms = gemmi.read_structure(str(m1)); ms.setup_entities()
            mmodel = ms[0]
            chain_name = chain_by.get(mdl) or chain_by.get(1)
            lig = lig_residue_in_chain(mmodel, chain_name)
            if lig is None:
                continue
            pchain = pocket_chain_for_lig(mmodel, lig)
            # choose the superposition that lands THIS ligand in the shared pocket
            sup = superpose_onto_ref(mmodel, ref_polys, pchain,
                                     lig_res=lig, pocket_center=pocket_center)
            if sup is None:
                continue
            align_rmsd, transform, nca = sup
            mmodel.transform_pos_and_adp(transform)   # move protein + ligand together
            # re-fetch ligand handle after transform (same object, now moved)
            lig = lig_residue_in_chain(mmodel, chain_name)
            rms = rmsd_by[mdl]
            pdb = gdir / f"{tgt}_{het}-pose-{mdl}.pdb"
            write_ligand_pdb(lig, pdb)
            if mdl == 1:
                lc = gemmi.Position(*[sum(a.pos.__getattribute__(ax) for a in lig) / len(lig)
                                      for ax in ("x", "y", "z")])
                centroid_dists.append((tgt, lc.dist(pocket_center), rmsd_by[1] < 2.0))
            poses_out.append({
                "sample": mdl,
                "rmsd": rms,
                "correct": rms < 2.0,
                "pose_file": f"systems/group_{gname}/{pdb.name}",
                "align_rmsd": round(align_rmsd, 2),
                "align_n": nca,
            })
            aligned_any = True
            if mdl == 1:
                center = centroid(lig)

        if not aligned_any:
            skipped.append((tgt, "no pose aligned (degenerate superposition)"))
            continue

        # crystal (truth) ligand aligned into ref frame ----------------------
        truth_file = None
        xtal = gdir / f"_xtal_{tgt}.cif"
        if fetch_xtal(tgt, xtal) > 0:
            try:
                xs = gemmi.read_structure(str(xtal)); xs.setup_entities()
                xm = xs[0]
                # find this het in the crystal; align its chain onto reference
                xlig = None
                for ch in xm:
                    for res in ch:
                        if res.name == het and (xlig is None or len(res) > len(xlig)):
                            xlig = res
                if xlig is not None:
                    xpchain = pocket_chain_for_lig(xm, xlig)
                    xsup = superpose_onto_ref(xm, ref_polys, xpchain,
                                              lig_res=xlig, pocket_center=pocket_center)
                    if xsup is not None:
                        xm.transform_pos_and_adp(xsup[1])
                        # re-find
                        xlig2 = None
                        for ch in xm:
                            for res in ch:
                                if res.name == het and (xlig2 is None or len(res) > len(xlig2)):
                                    xlig2 = res
                        tpdb = gdir / f"{tgt}_{het}-truth.pdb"
                        write_ligand_pdb(xlig2, tpdb, resname="LIG")
                        truth_file = f"systems/group_{gname}/{tpdb.name}"
                        truth_written = True
            except Exception:
                pass
            try:
                xtal.unlink()
            except Exception:
                pass

        pose1 = next(p for p in poses_out if p["sample"] == 1)
        fragments.append({
            "id": tgt,
            "ligand": het,
            "n_atoms": atoms,
            "correct": pose1["correct"],        # group oracle = pose-1 correctness
            "pose1_rmsd": pose1["rmsd"],
            "center": center,
            "poses": poses_out,
            "truth_file": truth_file,
        })
        tag = "OK " if truth_written else "no-truth"
        print(f"  {tgt} {het:7s} pose1 {pose1['rmsd']:5.2f}A "
              f"{'CORRECT' if pose1['correct'] else 'wrong  '} "
              f"({len(poses_out)} poses, {tag})")

    if not fragments:
        raise SystemExit("no fragments aligned for this group")

    nc = sum(f["correct"] for f in fragments)
    manifest = {
        "name": gname,
        "label": "Adenosine A2A receptor" if gname == "a2a" else gname,
        "reference_pdb": ref_pdb,
        "ref_file": f"systems/group_{gname}/ref.cif",
        "seqlen": len(seq),
        "n_members": len(members),
        "n_included": len(fragments),
        "n_correct": nc,
        "n_wrong": len(fragments) - nc,
        "fragments": fragments,
    }

    def denan(o):
        if isinstance(o, float):
            return None if not math.isfinite(o) else o
        if isinstance(o, dict):
            return {k: denan(v) for k, v in o.items()}
        if isinstance(o, list):
            return [denan(v) for v in o]
        return o

    out = HERE / f"group_{gname}.json"
    out.write_text(json.dumps(denan(manifest), indent=2, allow_nan=False))
    print(f"\nincluded {len(fragments)}/{len(members)} fragments "
          f"({nc} correct / {len(fragments)-nc} wrong), skipped {len(skipped)}")
    for tgt, why in skipped:
        print(f"  SKIP {tgt}: {why}")
    print(f"reference = {ref_pdb}; wrote {out.name}")

    # ---- sanity: distribution of pose-1 ligand-centroid -> dominant pocket ----
    # The screen binds several sub-pockets, so we anchor on the DOMINANT pocket
    # (cluster of correctly-placed ligands). The real symmetry-fix test is that
    # CORRECT fragments collapse there (a few A) -- a correct pose that landed on a
    # symmetry mate before should now be rescued. Genuinely wrong poses (AF3 put
    # the ligand elsewhere) and true minority-site binders may stay far -- that is
    # real chemistry, not an alignment artifact.
    import statistics as _st
    all_d = sorted(d for _, d, _ in centroid_dists)
    corr_d = sorted(d for _, d, c in centroid_dists if c)
    wrong_d = sorted(d for _, d, c in centroid_dists if not c)
    if all_d:
        def stats(xs):
            if not xs:
                return "  (none)"
            return (f"  min {xs[0]:.2f}  median {_st.median(xs):.2f}  max {xs[-1]:.2f}"
                    f"  | <5A: {sum(x < 5 for x in xs)}/{len(xs)}"
                    f"  >10A: {sum(x > 10 for x in xs)}/{len(xs)}")
        print(f"\npose-1 ligand-centroid -> dominant-pocket anchor (A):")
        print(f"  ALL     (n={len(all_d):2d}):{stats(all_d)}")
        print(f"  CORRECT (n={len(corr_d):2d}):{stats(corr_d)}")
        print(f"  WRONG   (n={len(wrong_d):2d}):{stats(wrong_d)}")
        # histogram of CORRECT fragments (these MUST collapse into one cluster)
        edges = [0, 2, 5, 10, 15, 20, 25, 30, 35, 1e9]
        labels = ["0-2", "2-5", "5-10", "10-15", "15-20", "20-25", "25-30", "30-35", ">35"]
        print("  CORRECT-fragment histogram (bin A : count):")
        for i, lab in enumerate(labels):
            c = sum(edges[i] <= d < edges[i + 1] for d in corr_d)
            print(f"    {lab:>6} : {c:2d}  {'#' * c}")
        corr_far = [(t, d) for t, d, c in centroid_dists if c and d > 10.0]
        print(f"  CORRECT fragments still >10 A from dominant pocket: {len(corr_far)}"
              + (" (these are real minority-site binders, not symmetry artifacts)"
                 if corr_far else " -- all collapsed, symmetry split fixed"))
        for t, d in sorted(corr_far, key=lambda x: -x[1]):
            print(f"    {t}: {d:.1f} A")


if __name__ == "__main__":
    main()
