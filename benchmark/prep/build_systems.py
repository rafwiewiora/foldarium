"""Select N viewer systems from the CAMEO 1-month AF3 dump.

Picks drug-like protein-ligand targets, verifies each crystal is RELEASED on RCSB
and actually contains the ligand HET, copies the 5 AF3 model CIFs into
verdict/data/poses/<ID>/, downloads the pristine crystal to systems/<ID>/xtal.cif,
and writes systems.json. Then run prep_poses.py to align + emit pose/protein PDBs.

Ordering: oldest CAMEO weeks first — their coordinates are already public; the most
recent week's structures may not have been released yet.

Env: CAMEO_SRC (default /tmp/cameo_week/month), N_SYSTEMS (default 50).
"""
import json, re, glob, os, shutil, urllib.request, random
from pathlib import Path
from collections import defaultdict
from statistics import median

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("CAMEO_SRC", "/tmp/cameo_week/month"))
POSE_DST = HERE.parent / "verdict" / "data" / "poses"
SYS = HERE / "systems"
N = int(os.environ.get("N_SYSTEMS", "50"))
UA = {"User-Agent": "cofold-viewer/0.3 (research)"}

EXC = set((
 "HOH DOD NA CL MG ZN CA K MN FE FE2 FE3 CU CU1 NI CO CD HG CS BA SR BR IOD I RB LI PB PT AU AG TL SM GD YB EU MO W V SE F ZN2 3CO 4MO OH O OXY "
 "SO4 PO4 PI NO3 ACT EDO GOL PEG PG4 PGE 1PE 2PE P6G MPD DMS BME MES EPE TRS TAR CIT FLC FMT IPA BO3 NH4 AZI CAC MLA OXL SCN 144 15P PE4 PEU DIO SIN MLI BCT CO3 UNX UNL UNK BU3 MRD IMD POL PGO PG0 12P 7PE DTT DTV TLA SUC "
 "NAG MAN BMA FUC GAL GLC NDG BGC FUL XYS RAM SIA NGA A2G GLA XYP GCU ADA RIB API MAL TRE LMT LMN DGD SGN BOG "
 "NAD NAP NDP NAI NAJ FAD FMN FDA ATP ADP AMP ANP ACP AGS APC GTP GDP GNP GSP GMP CTP UTP UDP UMP TTP TMP COA ACO SAM SAH SFG HEM HEC HEA HEB DHE HAS PLP PMP TPP TDP BTI BTN B12 COB H4B BH4 MGD PAP UD1 UPG 5GP PNS "
 "PLM CLR POV PTY CDL OLA OLB OLC STE MYR PEE PCW PC1 PEF LHG PGV PGW D10 DD9 HP6 Y01 HC3 PX4 3PE PEK PSC 17F PC7 PEV UND DAO LMG MC3 9PE PLC SPH CHS CHD EIC ARA HTG PX2"
).split())
drug_like = lambda h, a: h not in EXC and not (a is not None and a < 6)

# Hets that are valid ligands but are NEVER the screening target. Caffeine (TEP)
# is the co-crystallized A2A reference compound, present in every A2A fragment-soak
# entry; the screening target is the soaked fragment, not TEP. Excluded from the
# target-selection loop ONLY when another candidate exists (so a non-A2A entry that
# legitimately has TEP as its sole ligand still works). NOT added to EXC, which is
# the ligand-filter set used elsewhere.
TARGET_EXCLUDE = {"TEP"}


def collect():
    """target -> {week, ligs: {het: {atoms, rmsd:{model:[rmsd,...]}}}}"""
    data = defaultdict(lambda: {"week": None, "ligs": defaultdict(lambda: {"atoms": None, "rmsd": defaultdict(list)})})
    for f in glob.glob(str(SRC / "**/server993/model-*/scores/ligand_pose.json"), recursive=True):
        m = re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/servers/server993/model-(\d+)/", f)
        if not m:
            continue
        week, tgt, mdl = m.group(1), m.group(2), int(m.group(3))
        data[tgt]["week"] = week
        try:
            ligs = json.load(open(f))["results"]["details"]["ligand_pose"]["ligands"]
        except Exception:
            continue
        for k, v in ligs.items():
            r = v.get("rmsd")
            if r is None:
                continue
            het = k.split(".")[-1].upper()
            L = data[tgt]["ligs"][het]
            L["atoms"] = v.get("atom_count")
            L["rmsd"][mdl].append(float(r))
    return data


def candidates(data):
    """One drug-like ligand per target; per-model rmsd = median over copies."""
    out = []
    for tgt, d in data.items():
        # candidate hets = drug-like with >=1 scored (non-null rmsd) copy
        cand_hets = [het for het, L in d["ligs"].items()
                     if drug_like(het, L["atoms"]) and L["rmsd"]]
        if not cand_hets:
            continue
        # drop TARGET_EXCLUDE hets (e.g. caffeine TEP) only if another candidate
        # exists; otherwise fall back to the full candidate set.
        non_excl = [het for het in cand_hets if het not in TARGET_EXCLUDE]
        pool = set(non_excl) if non_excl else set(cand_hets)
        best = None
        for het, L in d["ligs"].items():
            if het not in pool:
                continue
            if best is None or (L["atoms"] or 0) > (d["ligs"][best]["atoms"] or 0):
                best = het
        if best is None:
            continue
        L = d["ligs"][best]
        poses = [{"sample": mdl, "rmsd": round(median(rs), 2), "correct": median(rs) < 2.0,
                  "confidence": None} for mdl, rs in sorted(L["rmsd"].items())]
        if len(poses) < 3:
            continue
        nc = sum(p["correct"] for p in poses)
        out.append({"id": tgt, "week": d["week"], "ligand": best, "n_atoms": L["atoms"],
                    "model": "AlphaFold3", "poses": poses,
                    "blurb": f"{nc}/{len(poses)} correct, best {min(p['rmsd'] for p in poses)}Å"})
    # Shuffle deterministically for DIVERSITY (sorting by id clusters consecutive
    # deposition series, e.g. one protein's fragment screen). All 4 weeks in this
    # window are already released; unreleased/missing crystals are skipped anyway.
    random.seed(7)
    random.shuffle(out)
    return out


def get_crystal(pdb_id):
    try:
        return urllib.request.urlopen(
            urllib.request.Request(f"https://files.rcsb.org/download/{pdb_id}.cif", headers=UA),
            timeout=60).read()
    except Exception:
        return None


def copy_models(tgt, week):
    wd = week  # dir uses dotted week (2026.05.16)
    src_dir = SRC / "modeling" / wd / tgt / "servers" / "server993"
    dst = POSE_DST / tgt
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for k in range(1, 6):
        src = src_dir / f"model-{k}" / f"model-{k}.cif"
        if src.exists():
            shutil.copy(src, dst / f"model-{k}.cif"); n += 1
    return n


def main():
    if not list(SRC.glob("modeling")):
        raise SystemExit(f"no CAMEO data at {SRC} (set CAMEO_SRC)")
    cands = candidates(collect())
    print(f"{len(cands)} drug-like candidate targets; validating crystals on RCSB until {N} ...")
    chosen, checked = [], 0
    for c in cands:
        if len(chosen) >= N:
            break
        checked += 1
        cif = get_crystal(c["id"])
        if cif is None:
            continue  # not released yet / no entry
        if c["ligand"].encode() not in cif:
            continue  # crystal lacks this HET
        if copy_models(c["id"], c["week"]) < 3:
            continue
        (SYS / c["id"]).mkdir(parents=True, exist_ok=True)
        (SYS / c["id"] / "xtal.cif").write_bytes(cif)
        chosen.append(c)
        if len(chosen) % 10 == 0:
            print(f"  {len(chosen)} valid (checked {checked}) ...")
    (HERE / "systems.json").write_text(json.dumps({"systems": chosen}, indent=2))
    print(f"selected {len(chosen)} systems (checked {checked} crystals). wrote systems.json")
    print("now run: python3 prep_poses.py")


if __name__ == "__main__":
    main()
