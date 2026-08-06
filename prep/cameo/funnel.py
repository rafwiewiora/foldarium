"""Count the filter funnel over all extracted CAMEO weeks: total targets -> ... -> eligible quiz items."""
import glob, re, sys
from collections import Counter
import numpy as np, gemmi, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "."); sys.path.insert(0, "../viewer")
import process_cameo as P, align_to_crystal as A, build_quiz_items as bq
from pathlib import Path
XTAL = Path("_xtal_cache")
c = Counter()
bases = sorted(glob.glob(str(P.CAMEO / "*" / "*" / "servers" / "server993")))
for i, sd in enumerate(bases):
    mm = re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/", sd); week, tgt = mm.group(1), mm.group(2)
    c["1 total CAMEO targets"] += 1
    try:
        ligs, _ = P.collect_target(week, tgt); het = P.select_target_het(ligs)
    except Exception:
        het = None
    if not het: continue
    c["2 has a drug-like target ligand"] += 1
    na = ligs[het].get("atoms") or 0                 # heavy-atom count (AF3 poses carry no H)
    if na >= 15: c["heavy15_druglike"] += 1          # >=15-heavy floor = the quiz's HEAVY_MIN
    pm = P.per_model_rmsd(ligs[het])
    if len(pm) < 3: continue
    c["3 >=3 scored AF3 models"] += 1
    if na >= 15: c["heavy15_scored"] += 1
    mn, mx = min(pm.values()), max(pm.values())
    if mn >= 2: continue
    c["4 oracle-correct (a <2A pose exists)"] += 1
    if mx < 2: continue
    c["5 + a clearly-wrong pose (rmsd-ambiguous)"] += 1
    cf = XTAL / f"{tgt}.cif"
    if not cf.exists(): c["rej_crystal_not_cached"] += 1; continue
    try:
        cst = gemmi.read_structure(str(cf)); cst.setup_entities(); cm = cst[0]
        cligs = [r for ch in cm for r in ch if r.name.upper() == het.upper()]
        clp = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in cligs[0] if a.element.name != "H"]) if cligs else None
        cpoly = A.pocket_chain(cm, clp) if clp is not None else None
    except Exception:
        cpoly = None
    if cpoly is None: continue
    c["6 crystal + pocket usable"] += 1
    amap = P.af3_ligand_map(week, tgt, het); poses = {}
    for mdl in pm:
        cr = amap.get(mdl)
        if not cr: continue
        try:
            st = gemmi.read_structure(str(P.server_dir(week, tgt) / f"model-{mdl}" / f"model-{mdl}.cif")); st.setup_entities(); m = st[0]
        except Exception: continue
        lr = next((r for ch in m if ch.name == cr[0] for r in ch if r.name == cr[1]), None)
        if lr is None: continue
        apoly = A.pocket_chain(m, np.array([[a.pos.x, a.pos.y, a.pos.z] for a in lr if a.element.name != "H"]))
        sup = A.seq_super(apoly, cpoly) if apoly else None
        if sup is None: continue
        R, t = sup
        poses[mdl] = (np.array([R @ np.array([a.pos.x, a.pos.y, a.pos.z]) + t for a in lr if a.element.name != "H"]), pm[mdl])
    if len(poses) < 3: continue
    c["7 >=3 poses align to crystal"] += 1
    samples = sorted(poses); cs = [poses[s][0] for s in samples]; n = len(cs[0])
    if any(len(x) != n for x in cs): continue
    labels, medoid = bq.cluster(cs); nclu = len(medoid)
    if nclu < 2: continue
    c["8 >=2 distinct pose clusters"] += 1
    cents = [cs[medoid[k]].mean(0) for k in range(nclu)]
    spread = max((float(np.linalg.norm(cents[a] - cents[b])) for a in range(nclu) for b in range(a + 1, nclu)), default=0)
    if spread >= bq.SINGLE_POCKET: continue
    c["9 single pocket (<8A spread)"] += 1
    rc = [poses[samples[medoid[k]]][1] < 2 for k in range(nclu)]
    if not (any(rc) and not all(rc)): continue
    c["10 ELIGIBLE: >=1 right + >=1 wrong cluster"] += 1
order = sorted([k for k in c if k[0].isdigit()], key=lambda k: int(k.split()[0]))
print(f"(crystals not cached for {c.get('rej_crystal_not_cached',0)} rmsd-ambiguous targets — skipped in this count)\n")
prev = None
for k in order:
    pct = f"{round(100*c[k]/c['1 total CAMEO targets'])}%"
    drop = f"  (−{prev - c[k]})" if prev is not None else ""
    print(f"  {k:<46s} {c[k]:>6d}  {pct:>4s}{drop}")
    prev = c[k]
tot = c['1 total CAMEO targets']
print("\n  ── >=15 heavy-atom floor (the quiz's HEAVY_MIN; not applied above) ──")
print(f"  drug-like & >=15 heavy                         {c['heavy15_druglike']:>6d}  {round(100*c['heavy15_druglike']/tot):>3d}%")
print(f"  >=3 scored & >=15 heavy                        {c['heavy15_scored']:>6d}  {round(100*c['heavy15_scored']/tot):>3d}%")
