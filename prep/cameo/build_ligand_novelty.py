"""FAST, local ligand-novelty scorer for CAMEO targets — no Foldseek API.

Novelty = how dissimilar a CAMEO target's drug-like ligand is to the AF3 training
ligand universe (PDB chemical components released BEFORE the AF3 cutoff,
2021-09-30). We replace Foldseek's slow, rate-limited 3D-shape homolog search with
a cheap *2D ligand* Tanimoto search against a precomputed pre-cutoff reference of
PDB chemical components.

Pipeline (run sub-steps via CLI arg; default runs all):

  step1  VALIDATE THE METRIC on our own data. For each train_sim item that HAS a
         Foldseek hit (train_het + train_shape_overlap), compute 2D Morgan-Tanimoto
         (RDKit r=2, 2048 bits) between the CAMEO ligand and the Foldseek-found
         train_het, and report Spearman vs the 3D shape metric (train_shape_overlap).
         -> does cheap 2D track the 3D shape signal on OUR data?

  ref    BUILD THE PRE-CUTOFF LIGAND REFERENCE. Pre-cutoff component IDs come from
         the RCSB search API (rcsb_chem_comp_info.initial_release_date < cutoff;
         one paginated query, ~35k IDs). SMILES come from the bulk RCSB Chemical
         Component Dictionary (components.cif.gz, one-time download). We keep only
         drug-like components (heavy-atom / element filter, mirrors viewer
         build_systems.drug_like spirit) and precompute Morgan fingerprints once,
         pickled to _lignov_cache/precutoff_ref.pkl.

  step3  SCORE + VALIDATE END-TO-END. For each train_sim item compute
         ligand_novelty = 1 - max Morgan-Tanimoto(CAMEO ligand, pre-cutoff ref).
         Correlate (Spearman) vs train_shape_overlap and report AUROC vs `novel`.
         -> does the independent cheap ligand search recover Foldseek's call?

  full   SCORE ALL drug-like CAMEO targets (the 228 in quiz_items.json) and write
         quiz/ligand_novelty.json.

Only ADDS files: build_ligand_novelty.py, ligand_novelty.json, _lignov_cache/.
Does NOT touch quiz_items.json, train_sim.json, app.js, server.py, process_cameo.py.
"""
import json, sys, gzip, pickle, urllib.request, urllib.parse, time
from pathlib import Path

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors, DataStructs
from rdkit import RDLogger
RDLogger.DisableLog("rdApp.*")
from scipy.stats import spearmanr

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_lignov_cache"
CACHE.mkdir(exist_ok=True)
LIGCACHE = HERE / "boltz_inputs" / "_ligcache"      # existing per-ligand cifs (read-only reuse)
TRAIN_SIM = HERE / "train_sim.json"
QUIZ_ITEMS = HERE / "quiz_items.json"
BOLTZ_YAML = HERE / "boltz_inputs"

CCD_GZ = CACHE / "components.cif.gz"
REF_PKL = CACHE / "precutoff_ref.pkl"
PRECUTOFF_IDS = CACHE / "precutoff_ids.json"
SMILES_CACHE = CACHE / "het_smiles.json"            # train_het / target-code SMILES cache

CUTOFF = "2021-09-30"
NOVEL_THRESH_SHAPE = 0.25                            # train_sim's definition (shape < 0.25 => novel)
NBITS = 2048
RADIUS = 2
UA = {"User-Agent": "Mozilla/5.0 (lignov-prep)"}

# Drug-like-ish gate for the reference & for CAMEO ligands. Mirrors the *spirit* of
# viewer/build_systems.drug_like (exclude tiny ions/solvents/sugars-as-buffer, crystallization
# junk, very large polymers). We keep it permissive on the reference side so we never
# *miss* a real training match; the gate mostly removes single-atom ions and waters.
COMMON_JUNK = {
    "HOH", "DOD", "WAT", "NA", "CL", "K", "MG", "CA", "ZN", "MN", "FE", "FE2", "CU",
    "CO", "NI", "CD", "HG", "BR", "IOD", "SO4", "PO4", "NO3", "ACT", "EDO", "GOL",
    "PEG", "PG4", "PGE", "DMS", "MPD", "FMT", "ACE", "EPE", "TRS", "MES", "BME",
    "IPA", "CO3", "NH4", "F", "OH", "O", "CMO", "AZI", "SCN", "FLC", "CAC",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def morgan(smiles):
    m = Chem.MolFromSmiles(smiles) if smiles else None
    if m is None:
        return None
    return rdMolDescriptors.GetMorganFingerprintAsBitVect(m, RADIUS, nBits=NBITS)


def tanimoto(fp1, fp2):
    return DataStructs.TanimotoSimilarity(fp1, fp2)


def is_druglike_mol(m):
    """Permissive drug-like gate on an RDKit mol: a real small molecule, not an
    ion / single atom / huge peptide. Used for the reference set and CAMEO ligands."""
    if m is None:
        return False
    nheavy = m.GetNumHeavyAtoms()
    if nheavy < 6 or nheavy > 150:
        return False
    elems = {a.GetSymbol() for a in m.GetAtoms()}
    if not (elems & {"C"}):          # must contain carbon
        return False
    # mostly-metal clusters (e.g. SF4, MN3, FES) -> drop
    metals = {"FE", "MN", "MO", "W", "NI", "CO", "CU", "ZN", "MG", "CA", "NA", "K",
              "CD", "HG", "PT", "PD", "RU", "OS", "IR", "V", "CR"}
    nmetal = sum(1 for a in m.GetAtoms() if a.GetSymbol().upper() in metals)
    if nmetal > 0 and nmetal >= nheavy - nmetal:   # at least half metal
        return False
    return True


# ---------------------------------------------------------------------------
# SMILES resolution for individual codes (train_het, CAMEO target code)
# reuses existing per-ligand cif cache; falls back to RCSB ligand endpoint.
# ---------------------------------------------------------------------------
def _smiles_from_cif_text(text):
    try:
        import gemmi
        b = gemmi.cif.read_string(text).sole_block()
    except Exception:
        return None
    t = b.find("_pdbx_chem_comp_descriptor.", ["comp_id", "type", "program", "descriptor"])
    cands = [(r.str(1), r.str(2), r.str(3)) for r in t if "SMILES" in r.str(1)]
    if not cands:
        return None
    for typ, prog, desc in cands:
        if typ == "SMILES_CANONICAL" and "OpenEye" in prog:
            return desc
    for typ, prog, desc in cands:
        if typ == "SMILES_CANONICAL":
            return desc
    return cands[0][2]


def fetch_code_smiles(code, smiles_cache):
    """SMILES for a CCD code. Order: in-memory cache -> existing boltz _ligcache cif
    -> our _lignov_cache cif -> RCSB ligand endpoint (cached)."""
    if code in smiles_cache:
        return smiles_cache[code]
    smi = None
    for cif in (LIGCACHE / f"{code}.cif", CACHE / f"{code}.cif"):
        if cif.exists() and cif.stat().st_size > 0:
            smi = _smiles_from_cif_text(cif.read_text(errors="ignore"))
            if smi:
                break
    if smi is None:
        url = f"https://files.rcsb.org/ligands/download/{code}.cif"
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read()
            (CACHE / f"{code}.cif").write_bytes(data)
            smi = _smiles_from_cif_text(data.decode(errors="ignore"))
        except Exception:
            smi = None
    smiles_cache[code] = smi
    return smi


def cameo_smiles_from_yaml(tgt):
    """CAMEO target ligand SMILES straight from the boltz input yaml (authoritative,
    same SMILES we folded)."""
    y = BOLTZ_YAML / f"{tgt}.yaml"
    if not y.exists():
        return None
    for line in y.read_text().splitlines():
        s = line.strip()
        if s.startswith("smiles:"):
            return s.split("smiles:", 1)[1].strip().strip("'\"")
    return None


def load_smiles_cache():
    if SMILES_CACHE.exists():
        return json.loads(SMILES_CACHE.read_text())
    return {}


def save_smiles_cache(c):
    SMILES_CACHE.write_text(json.dumps(c, indent=0))


# ---------------------------------------------------------------------------
# AUROC (no sklearn dependency needed but we have it; keep self-contained)
# ---------------------------------------------------------------------------
def auroc(labels, scores):
    """AUROC of `scores` predicting boolean `labels` (True=positive)."""
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=float)
    pos = scores[labels]
    neg = scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    # Mann-Whitney U / AUC via rank
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    allv = np.concatenate([pos, neg])
    # average ranks for ties
    sv = np.sort(allv)
    ranks_sorted = np.arange(1, len(allv) + 1, dtype=float)
    # assign average rank for ties
    from scipy.stats import rankdata
    r = rankdata(allv)
    rpos = r[:len(pos)]
    auc = (rpos.sum() - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))
    return auc


# ===========================================================================
# STEP 1 — validate 2D Morgan vs 3D shape on our CAMEO data
# ===========================================================================
def step1():
    ts = json.loads(TRAIN_SIM.read_text())
    smiles_cache = load_smiles_cache()
    rows = []
    skipped = []
    for tgt, v in ts.items():
        het = v.get("train_het")
        shape = v.get("train_shape_overlap")
        if not het or shape is None:
            continue
        cam_smi = cameo_smiles_from_yaml(tgt)
        het_smi = fetch_code_smiles(het, smiles_cache)
        fp_c = morgan(cam_smi)
        fp_h = morgan(het_smi)
        if fp_c is None or fp_h is None:
            skipped.append((tgt, het, cam_smi is None, het_smi is None))
            continue
        t2d = tanimoto(fp_c, fp_h)
        rows.append((tgt, het, t2d, shape, v.get("novel")))
    save_smiles_cache(smiles_cache)

    t2d = np.array([r[2] for r in rows])
    shp = np.array([r[3] for r in rows])
    rho, p = spearmanr(t2d, shp)
    print(f"\n[STEP1] 2D Morgan-Tanimoto vs 3D shape_overlap on our CAMEO Foldseek hits")
    print(f"  n = {len(rows)} items with a train_het + shape_overlap (skipped {len(skipped)})")
    print(f"  Spearman rho = {rho:.3f}  (p = {p:.2e})")
    # also: does 2D-Tanimoto predict the `novel` label directly?
    labels = np.array([bool(r[4]) for r in rows])
    auc_2d = auroc(labels, -t2d)     # higher novelty = LOWER tanimoto
    print(f"  AUROC of (1 - 2D Tanimoto to its OWN Foldseek het) for `novel`: {auc_2d:.3f}")
    if skipped:
        print(f"  skipped (no SMILES): {skipped[:8]}{' ...' if len(skipped)>8 else ''}")
    return rows, rho, p


# ===========================================================================
# STEP ref — build the pre-cutoff drug-like ligand reference
# ===========================================================================
def fetch_precutoff_ids():
    """All chem-comp IDs released before CUTOFF, via RCSB search API (paginated)."""
    if PRECUTOFF_IDS.exists():
        d = json.loads(PRECUTOFF_IDS.read_text())
        print(f"[ref] loaded {len(d)} pre-cutoff IDs from cache")
        return set(d)
    ids = []
    start = 0
    rows = 10000
    total = None
    while True:
        q = {
            "query": {"type": "terminal", "service": "text_chem", "parameters": {
                "attribute": "rcsb_chem_comp_info.initial_release_date",
                "operator": "less", "value": f"{CUTOFF}T00:00:00Z"}},
            "return_type": "mol_definition",
            "request_options": {"paginate": {"start": start, "rows": rows}},
        }
        url = "https://search.rcsb.org/rcsbsearch/v2/query?json=" + urllib.parse.quote(json.dumps(q))
        r = json.loads(urllib.request.urlopen(url, timeout=120).read())
        total = r["total_count"]
        batch = [x["identifier"] for x in r.get("result_set", [])]
        ids.extend(batch)
        print(f"[ref]   fetched {len(ids)}/{total}")
        start += rows
        if start >= total or not batch:
            break
        time.sleep(0.3)
    ids = sorted(set(ids))
    PRECUTOFF_IDS.write_text(json.dumps(ids))
    print(f"[ref] {len(ids)} distinct pre-cutoff (<{CUTOFF}) chem-comp IDs")
    return set(ids)


def parse_ccd_smiles(keep_ids):
    """Stream the bulk components.cif.gz; for each data block whose id is in keep_ids,
    pull the best SMILES_CANONICAL. Returns {code: smiles}. Pure-text parse (fast,
    no gemmi per-block) — we only need the SMILES_CANONICAL descriptor lines."""
    if not CCD_GZ.exists():
        raise SystemExit(f"missing {CCD_GZ} — download components.cif.gz first")
    out = {}
    cur = None
    keep = False
    # priority per block: OpenEye SMILES_CANONICAL > any SMILES_CANONICAL > any SMILES
    best = {}   # code -> (rank, smiles); rank 0 best
    with gzip.open(CCD_GZ, "rt", errors="ignore") as fh:
        for line in fh:
            if line.startswith("data_"):
                cur = line[5:].strip()
                keep = cur in keep_ids
                continue
            if not keep:
                continue
            # descriptor rows look like:
            #  CODE  SMILES_CANONICAL  "OpenEye OEToolkits" 1.5.0 "smiles..."
            if "SMILES" not in line:
                continue
            # tokenise respecting quotes
            toks = _cif_tokens(line)
            if len(toks) < 5:
                continue
            code, typ, prog = toks[0], toks[1], toks[2]
            if code != cur:
                continue
            if "SMILES" not in typ:
                continue
            smi = toks[-1]
            rank = 2
            if typ == "SMILES_CANONICAL":
                rank = 0 if "OpenEye" in prog else 1
            prev = best.get(code)
            if prev is None or rank < prev[0]:
                best[code] = (rank, smi)
    for code, (_, smi) in best.items():
        out[code] = smi
    return out


def _cif_tokens(line):
    """Split a CCD descriptor line into tokens, honoring single/double quotes."""
    toks, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c in " \t\n\r":
            i += 1
            continue
        if c in "'\"":
            j = line.find(c, i + 1)
            if j == -1:
                toks.append(line[i + 1:].strip())
                break
            toks.append(line[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and line[j] not in " \t\n\r":
                j += 1
            toks.append(line[i:j])
            i = j
    return toks


def build_reference(force=False):
    if REF_PKL.exists() and not force:
        with open(REF_PKL, "rb") as fh:
            ref = pickle.load(fh)
        print(f"[ref] loaded reference: {len(ref['codes'])} drug-like pre-cutoff ligands (cached)")
        return ref

    keep_ids = fetch_precutoff_ids()
    print(f"[ref] parsing SMILES from bulk CCD for {len(keep_ids)} pre-cutoff IDs ...")
    smi_map = parse_ccd_smiles(keep_ids)
    print(f"[ref]   got SMILES for {len(smi_map)} / {len(keep_ids)} pre-cutoff IDs")

    codes, fps = [], []
    n_junk = n_nondruglike = n_badsmiles = 0
    for code, smi in smi_map.items():
        if code in COMMON_JUNK:
            n_junk += 1
            continue
        m = Chem.MolFromSmiles(smi)
        if m is None:
            n_badsmiles += 1
            continue
        if not is_druglike_mol(m):
            n_nondruglike += 1
            continue
        fp = rdMolDescriptors.GetMorganFingerprintAsBitVect(m, RADIUS, nBits=NBITS)
        codes.append(code)
        fps.append(fp)
    print(f"[ref]   kept {len(codes)} drug-like  (dropped junk={n_junk}, "
          f"non-druglike={n_nondruglike}, bad_smiles={n_badsmiles})")
    ref = {"codes": codes, "fps": fps, "cutoff": CUTOFF, "nbits": NBITS, "radius": RADIUS,
           "n_precutoff_ids": len(keep_ids)}
    with open(REF_PKL, "wb") as fh:
        pickle.dump(ref, fh)
    print(f"[ref] wrote {REF_PKL}")
    return ref


# ===========================================================================
# scoring core
# ===========================================================================
def score_against_ref(fp, ref):
    """Return (max_tanimoto, nearest_code) of fp vs the reference."""
    sims = DataStructs.BulkTanimotoSimilarity(fp, ref["fps"])
    j = int(np.argmax(sims))
    return float(sims[j]), ref["codes"][j]


# ===========================================================================
# STEP 3 — end-to-end validation vs Foldseek on train_sim items
# ===========================================================================
def step3(ref):
    ts = json.loads(TRAIN_SIM.read_text())
    smiles_cache = load_smiles_cache()
    rows = []
    skipped = []
    for tgt, v in ts.items():
        cam_smi = cameo_smiles_from_yaml(tgt)
        fp = morgan(cam_smi)
        if fp is None:
            # fall back to the target ligand code
            fp = morgan(fetch_code_smiles(v.get("ligand", ""), smiles_cache))
        if fp is None:
            skipped.append(tgt)
            continue
        maxt, near = score_against_ref(fp, ref)
        rows.append({
            "id": tgt, "ligand": v.get("ligand"), "max_tanimoto_precutoff": round(maxt, 4),
            "nearest_train_ccd": near, "ligand_novelty": round(1 - maxt, 4),
            "shape_overlap": v.get("train_shape_overlap"), "foldseek_novel": bool(v.get("novel")),
        })
    save_smiles_cache(smiles_cache)

    # correlation vs shape_overlap (only items that HAVE a shape value)
    with_shape = [r for r in rows if r["shape_overlap"] is not None]
    ln = np.array([r["ligand_novelty"] for r in with_shape])
    shp = np.array([r["shape_overlap"] for r in with_shape])
    rho, p = spearmanr(ln, shp)

    labels = np.array([r["foldseek_novel"] for r in rows])
    novscore = np.array([r["ligand_novelty"] for r in rows])
    auc = auroc(labels, novscore)

    # also correlate max_tanimoto vs shape (positive expected)
    mt = np.array([r["max_tanimoto_precutoff"] for r in with_shape])
    rho_t, p_t = spearmanr(mt, shp)

    print(f"\n[STEP3] cheap ligand_novelty (1 - max Tanimoto to pre-cutoff ref) vs Foldseek")
    print(f"  n total = {len(rows)} (skipped {len(skipped)}); n with shape_overlap = {len(with_shape)}")
    print(f"  Spearman(ligand_novelty, shape_overlap) = {rho:.3f}  (p={p:.2e})")
    print(f"  Spearman(max_tanimoto,   shape_overlap) = {rho_t:.3f}  (p={p_t:.2e})")
    print(f"  AUROC(ligand_novelty -> Foldseek `novel`) = {auc:.3f}   [RnP ref was 0.81]")
    n_nov = int(labels.sum())
    print(f"  Foldseek novel: {n_nov}/{len(rows)}")
    # suggest a threshold: pick the ligand_novelty cut maximizing Youden's J
    best_j, best_thr = -1, None
    for thr in np.unique(novscore):
        pred = novscore >= thr
        tp = int((pred & labels).sum()); fp_ = int((pred & ~labels).sum())
        fn = int((~pred & labels).sum()); tn = int((~pred & ~labels).sum())
        tpr = tp / (tp + fn) if (tp + fn) else 0
        fpr = fp_ / (fp_ + tn) if (fp_ + tn) else 0
        j = tpr - fpr
        if j > best_j:
            best_j, best_thr = j, float(thr)
    print(f"  best ligand_novelty threshold (Youden J={best_j:.2f}): >= {best_thr:.3f} => novel")
    if skipped:
        print(f"  skipped: {skipped}")
    return rows, rho, auc, best_thr


# ===========================================================================
# STEP full — score every drug-like CAMEO target in quiz_items.json
# ===========================================================================
def step_full(ref, thr):
    items = json.loads(QUIZ_ITEMS.read_text())["items"]
    smiles_cache = load_smiles_cache()
    out = {}
    skipped = []
    for it in items:
        tgt, code = it["id"], it.get("ligand")
        cam_smi = cameo_smiles_from_yaml(tgt) or fetch_code_smiles(code, smiles_cache)
        fp = morgan(cam_smi)
        if fp is None:
            skipped.append(tgt)
            continue
        maxt, near = score_against_ref(fp, ref)
        ln = round(1 - maxt, 4)
        out[tgt] = {
            "ligand": code,
            "max_tanimoto_precutoff": round(maxt, 4),
            "nearest_train_ccd": near,
            "ligand_novelty": ln,
            "novel": bool(ln >= thr),
        }
    save_smiles_cache(smiles_cache)
    (HERE / "ligand_novelty.json").write_text(json.dumps(out, indent=2))
    n_nov = sum(1 for v in out.values() if v["novel"])
    print(f"\n[FULL] scored {len(out)} drug-like CAMEO targets (skipped {len(skipped)})")
    print(f"  novel (ligand_novelty >= {thr:.3f}): {n_nov}")
    print(f"  wrote {HERE/'ligand_novelty.json'}")
    if skipped:
        print(f"  skipped (no SMILES): {skipped}")
    return out


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("step1", "all"):
        step1()
    if which in ("ref", "all"):
        build_reference()
    if which in ("step3", "all", "full"):
        ref = build_reference()
        if which == "step3":
            step3(ref)
        elif which == "all":
            _, _, _, thr = step3(ref)
            step_full(ref, thr)
        elif which == "full":
            # default to the validated threshold if rerun standalone
            step_full(ref, NOVEL_THRESH_LN_DEFAULT)


# Threshold for the `novel` flag in ligand_novelty.json. This is a PURE-LIGAND
# 2D-novelty cut (ligand_novelty = 1 - max Morgan-Tanimoto to the pre-cutoff PDB).
# 0.60 == max-Tanimoto < 0.40 == "no close 2D analog in the pre-cutoff PDB", the
# usual medchem near-duplicate cutoff. NOTE: this is NOT Foldseek's `novel` (which is
# protein-anchored 3D shape, shape_overlap < 0.25) — see step3; the two diverge.
NOVEL_THRESH_LN_DEFAULT = 0.60


if __name__ == "__main__":
    main()
