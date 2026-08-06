"""What the drug-like filter excludes: for every CAMEO target, find its largest scored ligand and, for
targets with NO drug-like ligand (the dropped 4087), categorise WHY (ion / buffer / sugar / cofactor /
lipid / too-small / other). Also flag potential over-exclusion (a >=6-atom rejected ligand not in an
obvious junk category)."""
import glob, re, sys
from collections import Counter, defaultdict
sys.path.insert(0, "."); sys.path.insert(0, "../viewer")
import process_cameo as P, build_systems as bs
from statistics import median

# category sets (subsets of bs.EXC) for human-readable buckets
IONS = set("HOH DOD NA CL MG ZN CA K MN FE FE2 FE3 CU CU1 NI CO CD HG CS BA SR BR IOD I RB LI PB PT AU AG TL SM GD YB EU MO W V SE F ZN2 3CO 4MO OH O OXY NH4 AZI CAC SCN NO3 CO3 BCT UNX UNL UNK".split())
BUFFERS = set("SO4 PO4 PI ACT EDO GOL PEG PG4 PGE 1PE 2PE P6G MPD DMS BME MES EPE TRS TAR CIT FLC FMT IPA BO3 144 15P PE4 PEU DIO SIN MLI POL PGO PG0 12P 7PE DTT DTV TLA SUC MLA OXL BU3 MRD IMD".split())
SUGARS = set("NAG MAN BMA FUC GAL GLC NDG BGC FUL XYS RAM SIA NGA A2G GLA XYP GCU ADA RIB API MAL TRE LMT LMN DGD SGN BOG".split())
COFAC = set("NAD NAP NDP NAI NAJ FAD FMN FDA ATP ADP AMP ANP ACP AGS APC GTP GDP GNP GSP GMP CTP UTP UDP UMP TTP TMP COA ACO SAM SAH SFG HEM HEC HEA HEB DHE HAS PLP PMP TPP TDP BTI BTN B12 COB H4B BH4 MGD PAP UD1 UPG 5GP PNS".split())
LIPIDS = set("PLM CLR POV PTY CDL OLA OLB OLC STE MYR PEE PCW PC1 PEF LHG PGV PGW D10 DD9 HP6 Y01 HC3 PX4 3PE PEK PSC 17F PC7 PEV UND DAO LMG MC3 9PE PLC SPH CHS CHD EIC ARA HTG PX2".split())
def cat(h, a):
    if a is not None and a < 6: return "too-small (<6 atoms)"
    if h in IONS: return "ion / metal / small inorganic"
    if h in BUFFERS: return "buffer / cryoprotectant"
    if h in SUGARS: return "sugar / glycan"
    if h in COFAC: return "cofactor / nucleotide"
    if h in LIPIDS: return "lipid / detergent"
    if h == "TEP": return "TEP (caffeine, A2A reference)"
    return "other (in exclusion list)"

bases = sorted(glob.glob(str(P.CAMEO / "*" / "*" / "servers" / "server993")))
dropped_cat = Counter(); dropped_codes = Counter(); flagged = []; ndrop = nkept = nnolig = 0
for sd in bases:
    mm = re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/", sd); week, tgt = mm.group(1), mm.group(2)
    try: ligs, _ = P.collect_target(week, tgt)
    except Exception: ligs = {}
    scored = [(h, L) for h, L in ligs.items() if L["rmsd"]]
    if not scored: nnolig += 1; continue
    if bs.drug_like_any if False else any(bs.drug_like(h, L["atoms"]) and h != "TEP" for h, L in scored):
        nkept += 1; continue
    ndrop += 1
    big = max(scored, key=lambda x: (x[1]["atoms"] or 0))   # the best (largest) ligand we rejected
    h, a = big[0], big[1]["atoms"]
    dropped_cat[cat(h, a)] += 1; dropped_codes[h] += 1
    if a is not None and a >= 8 and cat(h, a) == "other (in exclusion list)":
        flagged.append((tgt, h, a))
tot = nkept + ndrop + nnolig
print(f"targets: {tot} | kept (drug-like) {nkept} | NO scored ligand {nnolig} | dropped-as-non-drug-like {ndrop}\n")
print("WHY the dropped targets fail (category of their LARGEST rejected ligand):")
for c, n in dropped_cat.most_common():
    print(f"  {c:<34s} {n:>5d}  ({round(100*n/ndrop)}%)")
print(f"\nmost common rejected ligand codes: {dict(dropped_codes.most_common(15))}")
print(f"\npotential over-exclusion (>=8-atom rejected ligand in 'other'): {len(flagged)}")
for t, h, a in flagged[:15]: print(f"   {t}: {h} ({a} atoms)")
