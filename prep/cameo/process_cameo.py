"""Turn the extracted 3-month CAMEO AlphaFold3 dump into pose-quiz items.

Reuses the CORRECTED viewer/quiz machinery (target selection, alignment, clustering,
eligibility) so the A2A caffeine/cholesterol bug stays fixed and the output schema is
byte-for-byte what quiz/build_quiz_items.py emits (so quiz/app.js works unchanged).

Per target:
  1. Read ligand_pose.json across the 5 AF3 models. Pick the target het = largest-atom_count
     drug_like ligand, EXCLUDING TEP (caffeine, A2A reference) when another candidate exists.
     Per-model rmsd = median over that het's scored copies. Require >=3 scored models.
  2. SCORE pre-filter (no download): keep only if min(per-model rmsd) < 2 AND max >= 2.
  3. Download the crystal; identify the crystal target-het copies.
  4. Per model: locate the AF3 ligand (chain.resname from model_ligand_rmsd) -> residue in
     model-<n>.cif; Ca-superpose its pocket chain onto the crystal, choosing the crystal-chain
     mapping that MINIMISES predicted-ligand-to-crystal-ligand-copy centroid distance; transform
     the AF3 ligand into the crystal frame; write pose-<sample>.pdb. Record pose ligand coords.
  5. Write the single crystal protein chain the poses aligned to as apo protein.pdb.
  6. Quiz eligibility (build_quiz_items.classify-equivalent): cluster pose coords; require
     >=2 clusters, single pocket, >=1 correct + >=1 wrong cluster.
  7. Emit item with the exact build_quiz_items schema.

Outputs: quiz/data/<TARGET>/{protein.pdb, pose-<s>.pdb, pocket-<s>.pdb, pocket-union.pdb}
and quiz/quiz_items.json (backed up first as quiz_items.json.bak2).
"""
import json, re, glob, shutil, sys, math, random
from pathlib import Path
from statistics import median
from collections import defaultdict
import numpy as np
import gemmi

HERE = Path(__file__).resolve().parent
VIEWER = HERE.parent / "viewer"
DATA = HERE / "data"
sys.path.insert(0, str(VIEWER))

# REUSE the corrected machinery -------------------------------------------------
import build_systems as bs            # drug_like, EXC, TARGET_EXCLUDE
import prep_poses as pp               # fetch_xtal, protein_polys, one_letter, pocket_chain,
                                      # af3_residue_by_id, write_ligand_pdb, ligand_centroid,
                                      # centroid_pos, transformed_centroid
import build_quiz_items as bq         # cluster, read_protein_atoms, write_pocket, thresholds
import compute_overlaps as co         # read_lig_pdb (for clustering coords, same as build_quiz_items)
import difflib

CAMEO = Path("/Users/rafalwiewiora/cameo_data/extracted/modeling")
XTAL_CACHE = HERE / "_xtal_cache"     # cache pristine crystal cifs to avoid re-download
XTAL_CACHE.mkdir(exist_ok=True)

drug_like = bs.drug_like
TARGET_EXCLUDE = getattr(bs, "TARGET_EXCLUDE", {"TEP"})


# --- 1. collect ligand_pose.json across 5 models per target -------------------
def server_dir(date, tgt):
    return CAMEO / date / tgt / "servers" / "server993"


def collect_target(date, tgt):
    """{het: {atoms, rmsd:{model:[copy rmsds]}}} for one target, from its 5 models."""
    ligs = defaultdict(lambda: {"atoms": None, "rmsd": defaultdict(list)})
    sd = server_dir(date, tgt)
    nmodels = 0
    for f in sorted(glob.glob(str(sd / "model-*" / "scores" / "ligand_pose.json"))):
        mdl = int(re.search(r"model-(\d+)", f).group(1))
        try:
            d = json.load(open(f))["results"]["details"]["ligand_pose"]["ligands"]
        except Exception:
            continue
        nmodels += 1
        for k, v in d.items():
            het = k.split(".")[-1].upper()
            L = ligs[het]
            # representative atom_count: the modal (max-frequency) non-null count, so multi-
            # copy hets whose copies have differing atom_count (truncated lipids) report the
            # canonical full-ligand size; for our drug-like target hets this is unambiguous.
            ac = v.get("atom_count")
            if ac is not None:
                L.setdefault("_acs", []).append(ac)
            r = v.get("rmsd")
            if r is not None:
                L["rmsd"][mdl].append(float(r))
    for het, L in ligs.items():
        acs = L.pop("_acs", [])
        if acs:
            # modal atom count
            L["atoms"] = max(set(acs), key=acs.count)
    return ligs, nmodels


def select_target_het(ligs):
    """Largest-atom_count drug-like het with >=1 scored copy, excluding TEP when possible."""
    cand = [het for het, L in ligs.items() if drug_like(het, L["atoms"]) and L["rmsd"]]
    if not cand:
        return None
    non_excl = [h for h in cand if h not in TARGET_EXCLUDE]
    pool = non_excl if non_excl else cand
    return max(pool, key=lambda h: (ligs[h]["atoms"] or 0))


def per_model_rmsd(L):
    """{model: median rmsd over that het's scored copies}."""
    return {mdl: round(median(rs), 4) for mdl, rs in sorted(L["rmsd"].items())}


# --- AF3 ligand map from EXTRACTED dir (replaces prep_poses.af3_ligand_map) ----
def af3_ligand_map(date, tgt, het):
    """{model_no: (af3_chain, af3_resname)} for the AF3 ligand CAMEO matched to `het`,
    read from each model's ligand_pose.json model_ligand_rmsd (e.g. 'C.LIG_C1' ->
    chain 'C', resname 'LIG_C')."""
    out = {}
    sd = server_dir(date, tgt)
    for f in sorted(glob.glob(str(sd / "model-*" / "scores" / "ligand_pose.json"))):
        mdl = int(re.search(r"model-(\d+)", f).group(1))
        try:
            ligs = json.load(open(f))["results"]["details"]["ligand_pose"]["ligands"]
        except Exception:
            continue
        for k, v in ligs.items():
            if k.split(".")[-1].upper() != het.upper():
                continue
            mlr = v.get("model_ligand_rmsd")
            if not mlr:
                continue
            chain, resid = mlr.split(".", 1)
            m = re.match(r"(.*?)(\d+)$", resid)
            resname = m.group(1) if m else resid
            out[mdl] = (chain, resname)
            break
    return out


# --- crystal helpers -----------------------------------------------------------
def crystal_het_centroids(xmodel, het):
    """[(gemmi.Position centroid, residue)] for every crystal copy of het."""
    out = []
    for ch in xmodel:
        for res in ch:
            if res.name.upper() == het.upper():
                out.append((pp.centroid_pos(res), ch.name))
    return out


def write_apo_protein(model, chain_name, dest):
    """Write a single protein chain (polymer only, no ligand) as PDB -> apo pocket protein."""
    out = gemmi.Structure()
    om = gemmi.Model("1")
    for ch in model:
        if ch.name != chain_name:
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


# --- per-pose alignment (replicates prep_poses lines ~268-300) -----------------
def align_pose(date, tgt, het, sample, af3map, xmodel, xpolys, xlig):
    """Return (pose_ligand_residue_in_crystal_frame, used_crystal_chain) or (None, None).
    Transforms a COPY of the model so the crystal stays untouched. xlig = list of
    (centroid Position, crystal chain name) of the target-het copies."""
    src = server_dir(date, tgt) / f"model-{sample}" / f"model-{sample}.cif"
    if not src.exists():
        return None, None
    ms = gemmi.read_structure(str(src))
    ms.setup_entities()
    mmodel = ms[0]
    cr = af3map.get(sample)
    lig = None
    if cr is not None:
        lig = pp.af3_residue_by_id(mmodel, cr[0], cr[1])
    if lig is None:
        return None, None
    pchain = pp.pocket_chain(mmodel, lig)
    if pchain is None or not xpolys:
        return None, None
    mpoly = pchain.get_polymer()
    mseq = pp.one_letter(mpoly)
    cands = [(xn, xp) for xn, xp in xpolys
             if difflib.SequenceMatcher(None, pp.one_letter(xp), mseq).ratio() > 0.5]
    best = None  # (lig_dist, sup, xname)
    xlig_cent = [c for c, _ in xlig]
    for xn, xp in (cands or xpolys):
        sup = gemmi.calculate_superposition(
            xp, mpoly, gemmi.PolymerType.PeptideL, gemmi.SupSelect.CaP)
        if not math.isfinite(sup.rmsd):
            continue
        lc = pp.transformed_centroid(lig, sup.transform)
        d = min((lc.dist(xc) for xc in xlig_cent), default=0.0)
        if best is None or d < best[0]:
            best = (d, sup, xn)
    if best is None:
        return None, None
    _, sup, used = best
    mmodel.transform_pos_and_adp(sup.transform)
    lig2 = pp.af3_residue_by_id(mmodel, cr[0], cr[1])  # re-fetch transformed residue
    return lig2, used


def nearest_crystal_chain_to_poses(xmodel, xpolys, pose_coords):
    """The crystal protein chain (polymer) whose Ca atoms are closest to the pose ligand
    cloud (the copy the poses aligned to) -> apo protein chain to write."""
    allpos = np.vstack(pose_coords)
    best, bestd = None, 1e18
    for xn, xp in xpolys:
        cas = []
        for res in xp:
            for at in res:
                if at.name == "CA":
                    cas.append([at.pos.x, at.pos.y, at.pos.z]); break
        if not cas:
            continue
        cas = np.array(cas)
        # min distance from any pose atom to any Ca
        d = np.min(np.linalg.norm(cas[:, None] - allpos[None], axis=2))
        if d < bestd:
            bestd, best = d, xn
    return best


def main():
    random.seed(7)
    dates = sorted(p.name for p in CAMEO.iterdir() if p.is_dir())
    print(f"CAMEO weeks: {dates}")

    # Stage 1+2: collect every target, select het, SCORE pre-filter (no downloads).
    prefiltered = []   # (date, tgt, het, ligs_for_het, per_model_rmsd dict)
    n_targets = 0
    for date in dates:
        for tdir in sorted((CAMEO / date).iterdir()):
            if not tdir.is_dir():
                continue
            tgt = tdir.name
            n_targets += 1
            ligs, nmodels = collect_target(date, tgt)
            het = select_target_het(ligs)
            if het is None:
                continue
            L = ligs[het]
            pmr = per_model_rmsd(L)
            if len(pmr) < 3:
                continue
            vals = list(pmr.values())
            if not (min(vals) < 2.0 and max(vals) >= 2.0):
                continue
            prefiltered.append((date, tgt, het, L, pmr))
    print(f"scanned {n_targets} targets across {len(dates)} weeks; "
          f"{len(prefiltered)} pass SCORE pre-filter (min<2 & max>=2, >=3 models)")

    # Stage 3-7: per candidate, download crystal, align, classify, emit.
    items = []
    skipped = defaultdict(list)
    gate2_records = []   # for random sampling later
    for date, tgt, het, L, pmr in prefiltered:
        # download/cache crystal
        cif = XTAL_CACHE / f"{tgt}.cif"
        try:
            pp.fetch_xtal(tgt, cif)
        except Exception as e:
            skipped["download_failed"].append(tgt); continue
        if cif.stat().st_size == 0:
            skipped["download_failed"].append(tgt); continue
        try:
            xs = gemmi.read_structure(str(cif))
            xs.setup_entities()
            xmodel = xs[0]
        except Exception:
            skipped["xtal_parse_error"].append(tgt); continue
        # crystal must actually contain the target het
        xlig = crystal_het_centroids(xmodel, het)
        if not xlig:
            skipped["het_not_in_crystal"].append(tgt); continue
        xpolys = pp.protein_polys(xmodel)
        if not xpolys:
            skipped["no_protein"].append(tgt); continue

        af3map = af3_ligand_map(date, tgt, het)

        # align each model's AF3 ligand into the crystal frame
        try:
            samples = sorted(pmr.keys())
            pose_residues = {}   # sample -> transformed gemmi residue
            for s in samples:
                lig2, used = align_pose(date, tgt, het, s, af3map, xmodel, xpolys, xlig)
                if lig2 is None:
                    continue
                pose_residues[s] = lig2
        except Exception as e:
            skipped["align_error"].append(f"{tgt}:{e}"); continue
        if len(pose_residues) < 3:
            skipped["align_too_few"].append(tgt); continue

        # restrict to samples that aligned AND have a score; build pose coord arrays
        poses = []
        for s in sorted(pose_residues):
            res = pose_residues[s]
            heavy = [[a.pos.x, a.pos.y, a.pos.z] for a in res if a.element.name != "H"]
            poses.append({"sample": s, "rmsd": round(pmr[s], 2),
                          "coords": np.array(heavy), "res": res})
        # consistent atom count across poses (required for clustering)
        n = len(poses[0]["coords"])
        if n == 0 or any(len(p["coords"]) != n for p in poses):
            skipped["atomcount_mismatch"].append(tgt); continue

        cs = [p["coords"] for p in poses]
        labels, medoid = bq.cluster(cs)
        nclu = len(medoid)
        if nclu < 2:
            skipped["trivial_singlecluster"].append(tgt); continue
        rep_cent = [cs[medoid[c]].mean(0) for c in range(nclu)]
        spread = max(float(np.linalg.norm(rep_cent[i] - rep_cent[j]))
                     for i in range(nclu) for j in range(i + 1, nclu))
        reps_correct = [poses[medoid[c]]["rmsd"] < 2 for c in range(nclu)]
        if not (any(reps_correct) and not all(reps_correct)):
            skipped["trivial_allsame"].append(tgt); continue
        if spread >= bq.SINGLE_POCKET:
            skipped["multipocket"].append(tgt); continue

        # ELIGIBLE -> write data
        d = DATA / tgt
        d.mkdir(parents=True, exist_ok=True)
        # apo crystal protein: the chain the poses aligned to
        apo_chain = nearest_crystal_chain_to_poses(xmodel, xpolys, cs)
        write_apo_protein(xmodel, apo_chain, d / "protein.pdb")
        prot = bq.read_protein_atoms(d / "protein.pdb")
        bq.write_pocket(prot, np.vstack(cs), d / "pocket-union.pdb")

        choices = []
        for i, p in enumerate(poses):
            pp.write_ligand_pdb(p["res"], d / f"pose-{p['sample']}.pdb")
            bq.write_pocket(prot, p["coords"], d / f"pocket-{p['sample']}.pdb")
            choices.append({
                "af3_sample": p["sample"],
                "pose_file": f"data/{tgt}/pose-{p['sample']}.pdb",
                "pocket_file": f"data/{tgt}/pocket-{p['sample']}.pdb",
                "rmsd": p["rmsd"], "correct": bool(p["rmsd"] < 2),
                "cluster": labels[i], "is_rep": medoid[labels[i]] == i,
            })
        item = {
            "id": tgt, "ligand": het, "protein_file": f"data/{tgt}/protein.pdb",
            "pocket_union_file": f"data/{tgt}/pocket-union.pdb",
            "choices": choices, "n_clusters": nclu, "af3_top_sample": 1,
            "n_correct": sum(c["correct"] for c in choices),
            "source": "cameo-af3", "week": date,
        }
        items.append(item)
        # record for gate 2: pose centroid -> nearest crystal het copy distance
        gate2_records.append((tgt, het, xlig, poses))

    # ---- VALIDATION GATES ----
    print("\n" + "=" * 70)
    print("GATE 1 — A2A ligand bug check (targets 7IN*/7IO*/7IP*)")
    print("=" * 70)
    gate1_fail = False
    a2a_items = [it for it in items if it["id"][:3] in ("7IN", "7IO", "7IP")]
    for it in a2a_items:
        # pose atom count from the written pose file
        pf = HERE / it["choices"][0]["pose_file"]
        pose_atoms = sum(1 for ln in open(pf) if ln[:6] in ("HETATM", "ATOM  "))
        cameo_atoms = None
        # cameo target het atom_count
        ligs, _ = collect_target(it["week"], it["id"])
        cameo_atoms = ligs[it["ligand"]]["atoms"]
        print(f"  {it['id']}: ligand={it['ligand']} pose_atoms={pose_atoms} "
              f"cameo_het_atoms={cameo_atoms}")
        if it["ligand"] == "TEP":
            print(f"    !! A2A item has ligand=TEP"); gate1_fail = True
        if pose_atoms == 28:
            print(f"    !! A2A pose has 28 atoms (cholesterol)"); gate1_fail = True
        if cameo_atoms is not None and pose_atoms != cameo_atoms:
            print(f"    !! pose_atoms != cameo het atom_count"); gate1_fail = True
    if not a2a_items:
        print("  (no A2A items produced)")
    if gate1_fail:
        print("\nGATE 1 FAILED — stopping, not writing quiz_items.json")
        return

    print("\n" + "=" * 70)
    print("GATE 2 — rmsd sanity (5 random eligible items)")
    print("=" * 70)
    gate2_fail = False
    sample_recs = random.sample(gate2_records, min(5, len(gate2_records)))
    for tgt, het, xlig, poses in sample_recs:
        xcent = [c for c, _ in xlig]
        print(f"  {tgt} ({het}):")
        for p in poses:
            cent = p["coords"].mean(0)
            cg = gemmi.Position(*cent)
            dmin = min(cg.dist(xc) for xc in xcent)
            tag = ""
            if p["rmsd"] < 2 and dmin > 3.0:
                tag = "  <-- correct pose FAR from crystal lig"; gate2_fail = True
            print(f"    sample {p['sample']}: cameo_rmsd={p['rmsd']:.2f} "
                  f"centroid_dist_to_xtal={dmin:.2f}{tag}")
    if gate2_fail:
        print("\nGATE 2 FAILED — correct poses land far from crystal ligand. "
              "Alignment is wrong. Stopping, not writing quiz_items.json")
        return

    # ---- write outputs ----
    bak = HERE / "quiz_items.json.bak2"
    cur = HERE / "quiz_items.json"
    if cur.exists() and not bak.exists():
        shutil.copy(cur, bak)
        print(f"\nbacked up existing quiz_items.json -> {bak.name}")
    elif cur.exists():
        # bak2 already exists from a prior run; refresh from a .bak of the original if needed
        print(f"\n{bak.name} already exists (preserving original backup)")
    (cur).write_text(json.dumps({"items": items}, indent=2))

    print("\n" + "=" * 70)
    print("GATE 3 — final counts / AF3-on-the-quiz-set baseline")
    print("=" * 70)
    print(f"  eligible quiz items: {len(items)}")
    nclu_dist = defaultdict(int)
    for it in items:
        nclu_dist[it["n_clusters"]] += 1
    print(f"  n_clusters distribution: {dict(sorted(nclu_dist.items()))}")
    af3_right = 0
    for it in items:
        c = next(ch for ch in it["choices"] if ch["af3_sample"] == it["af3_top_sample"])
        if c["correct"]:
            af3_right += 1
    print(f"  AF3 top-pick (model 1) correct: {af3_right}/{len(items)} "
          f"= {100*af3_right/max(1,len(items)):.0f}%  (wrong: {len(items)-af3_right})")

    print("\nskipped/errored counts:")
    for reason, ids in sorted(skipped.items()):
        print(f"  {reason}: {len(ids)}")
    print(f"\nwrote {len(items)} items -> {cur}")


if __name__ == "__main__":
    main()
