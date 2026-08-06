"""Per viewer system: find the closest PRE-CUTOFF training complex and score ligand shape overlap.

For each system's crystal (the experimental complex = what AF3 was asked to predict):
  1. find the most similar protein in the PDB released BEFORE 2021-09-30 that has a drug-like ligand
     (Foldseek STRUCTURE search via the public web API -- replaces the old RCSB sequence search,
      which was polluted by BRIL-fusion partners on A2A targets and falsely flagged them "novel"),
  2. superpose that training protein onto our crystal protein (seq-aware Cα),
  3. carry the training ligand into our frame, write it as a ligand-only PDB (for the viewer overlay),
  4. score how much the training ligand and OUR crystal ligand occupy the same space
     (vdW-volume Tanimoto, in place — the protein-frame / sucos_protein-spirit metric).

Adds to each system in systems.json:
  train_pdb, train_identity, train_het, train_shape_overlap, train_ligand_file
(or train_pdb=null if no pre-cutoff ligand-bearing similar protein → genuinely novel).
"""
import json, sys, urllib.request, urllib.parse, urllib.error, tempfile, difflib, time, re
from pathlib import Path
import numpy as np, gemmi

HERE = Path(__file__).resolve().parent
SYS = HERE / "systems"
CUTOFF = "2021-09-30"

# Reuse the prior agent's validated Foldseek web-API client (submit/poll/fetch + RCSB date filter).
FOLDSEEK_DIR = Path("/Users/rafalwiewiora/repos/paperia/cofolding_benchmark/foldseek_test")
sys.path.insert(0, str(FOLDSEEK_DIR))
import foldseek_search as fs   # submit, poll, fetch_result, release_date, parse_pdbid
UA = {"User-Agent": "cofold-trainsim/1.0"}
VDW = {"C": 1.7, "N": 1.55, "O": 1.52, "S": 1.8, "P": 1.8, "F": 1.47, "CL": 1.75, "BR": 1.85, "I": 1.98, "B": 1.92}
vdw = lambda e: VDW.get(e.upper(), 1.7)
EXC = set(("HOH DOD NA CL MG ZN CA K MN FE FE2 FE3 CU CU1 NI CO CD HG CS BA SR BR IOD I RB LI PB PT AU AG SO4 PO4 PI "
 "NO3 ACT EDO GOL PEG PG4 PGE 1PE 2PE P6G PE3 PE4 PEU MPD DMS BME MES EPE TRS TAR CIT FLC FMT IPA BO3 NH4 AZI CAC OXL "
 "SCN BCT CO3 BCN BTB MRD IMD DTT DTV TLA SUC EOH ACE BU3 MLA MLI 15P 7PE 12P P6G PG0 PG4 144 SIN POL OCT D10 LDA "
 "UNX UNL UNK NAG MAN BMA FUC GAL GLC NDG BGC FUL XYS MAL TRE A2G NGA SIA "  # buffers/cryo/sugars
 "ATP ADP AMP ANP ACP AGS APC GTP GDP GNP GSP GMP CTP UTP UDP UMP TTP TMP NAD NAP NDP FAD FMN COA ACO SAM SAH "  # nucleotides/cofactors
 "HEM HEC HEA HAS PLP PMP TPP BTI BTN B12 GTN CMP UD1 5GP "
 # membrane lipids / detergents / fatty acids — pass the 3-5char druglike test but are crystallization
 # additives (GPCR structures are full of these), NOT real ligands; would be picked spuriously.
 "CLR CHS CHD CLL Y01 OLA OLB OLC OLE PLM STE MYR PEE PCW PC1 PEK PSC PEF PX4 3PE 9PE PGV PGW POV PTY PEV PEH "
 "CDL SPH PLC PE PC PS PG PI PA SQD LHG LMT LMN LMG LMU DMU UMQ UDM JZ4 BOG BNG OCT NG6 DDR HP6 D10 DD9 MC3 HC3 "
 "LDA SDS DAO C8E C10 C12 C14 7E8 7E9 P15 PEU 1WV F09 KEX CXE BAM B7N B3P MB6 OGA 211 12P 16P 6PE 7PH OLI ELA "
 "TWT D12 DDM DMP DHD MYS PLD PLO PX2 "
 # modified standard residues (HETATM but part of the chain, not ligands): selenomethionine etc.
 "MSE SEP TPO PTR CSO CSD CME CSX KCX LLP MLY M3L CGU PCA SAC ALY DAL CAS OCS NEP HIC MHO").split())
druglike = lambda h: h.upper() not in EXC and 3 <= len(h) <= 5
MAX_ALIGN_RMSD = 4.0      # legacy whole-chain gate (kept as a loose ceiling)
MAX_LOCAL_RMSD = 3.0      # pocket-local (active-site) gate on the Foldseek-aligned core -- the meaningful
                          # one for remote homologs, where whole-chain rigid RMSD is large but the
                          # conserved active site superposes tightly.
POCKET_RADIUS = 8.0       # query residues within this of the crystal ligand define the "pocket-local" set


REFCACHE = HERE / "_refcache"   # downloaded RCSB reference structures, reused across systems

def load(src):
    if len(src) == 4 and "." not in src and "/" not in src:
        REFCACHE.mkdir(exist_ok=True)
        cached = REFCACHE / f"{src.upper()}.cif"
        if not cached.exists():
            data = urllib.request.urlopen(urllib.request.Request(
                f"https://files.rcsb.org/download/{src.upper()}.cif", headers=UA), timeout=60).read()
            cached.write_bytes(data)
        src = str(cached)
    st = gemmi.read_structure(src); st.setup_entities()
    return st[0]


STD_AA = set("ACDEFGHIKLMNPQRSTVWY")
def longest_seq(model):
    polys = [ch.get_polymer() for ch in model if len(ch.get_polymer()) > 5]
    if not polys:
        return None
    raw = max(polys, key=len).make_one_letter_sequence()
    # strip gap '-' / non-standard chars (e.g. BRIL-fusion breaks) — RCSB rejects them
    return "".join(c for c in raw if c in STD_AA)


def lig_atoms(model, het):
    best = None
    for ch in model:
        for res in ch:
            if res.name.upper() == het.upper() and (best is None or len(res) > len(best)):
                best = res
    if best is None:
        return None
    return best


def lig_arrays(res):
    pos = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in res if a.element.name != "H"])
    rad = np.array([vdw(a.element.name) for a in res if a.element.name != "H"])
    return pos, rad


_PDB_CHAIN_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

def protein_only_pdb(cif_path, dest):
    """cif -> protein-only PDB for Foldseek (gemmi; same recipe as foldseek_test/prep_structures.py).

    Legacy PDB allows only single-character chain IDs; some crystals (e.g. 9V9D has chain 'AAA')
    use multi-char names that gemmi refuses to write. Rename every chain to a single char first.
    Foldseek searches entry 0 (first chain) and downstream picks chains by sequence, so names are
    immaterial to the result.
    """
    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()
    st.remove_ligands_and_waters()
    st.remove_empty_chains()
    for i, ch in enumerate(st[0]):
        ch.name = _PDB_CHAIN_CHARS[i % len(_PDB_CHAIN_CHARS)]
    st.write_pdb(str(dest))


FSHITS = HERE / "_fshits"   # parsed Foldseek hit lists per system, so reruns never re-hit the API
FSHITS_VERSION = 2          # v2 stores Foldseek's per-hit ALIGNMENT (qAln/dbAln/start/tCa) so the
                            # downstream can superpose on the conserved core, not the whole chain.
                            # Bumping this invalidates v1 caches (which lacked alignment fields).


def _parse_fs_result(res, exclude_pdb, rows):
    """Foldseek result dict -> [hit,...] pre-cutoff, query excluded, best-first.

    Each hit keeps {pdb, identity} PLUS Foldseek's structural alignment so we can superpose the
    training protein onto the query via the conserved-core correspondence (robust at low identity):
      qStartPos/dbStartPos : 1-based start of the alignment in the query / target sequence
      qAln/dbAln           : the gapped aligned sequences
      tCa                  : the target chain's Ca coords (flat 'x,y,z,x,y,z,...' string)
    We pick the BEST (first / highest-score) alignment per PDB id.
    """
    try:
        alns = res["results"][0]["alignments"][0]
    except (KeyError, IndexError, TypeError):
        return None
    out, seen = [], set()
    for h in alns[:rows]:
        pdb = fs.parse_pdbid(h.get("target", "") or "")
        if not pdb or pdb in seen or pdb.upper() == exclude_pdb.upper():
            continue
        dt = fs.release_date(pdb)            # RCSB initial_release_date (cached in fs)
        if not dt or dt[:10] >= CUTOFF:      # keep strictly pre-cutoff (excludes co-released entries)
            continue
        seen.add(pdb)
        seqid = h.get("seqId")
        ident = (seqid / 100.0) if seqid is not None else None   # 0-100 -> 0-1
        out.append({"pdb": pdb, "identity": ident,
                    "qStartPos": h.get("qStartPos"), "dbStartPos": h.get("dbStartPos"),
                    "qAln": h.get("qAln"), "dbAln": h.get("dbAln"), "tCa": h.get("tCa")})
    return out


def search_pre_cutoff(seq, exclude_pdb, rows=40, _cif=None):
    """FOLDSEEK structure search for pre-cutoff training candidates.

    `seq` is kept in the signature for downstream compatibility but unused -- Foldseek needs the
    *structure*, so we read the crystal cif (passed via `_cif`, default systems/<exclude_pdb>/xtal.cif),
    write a protein-only PDB, and query the public Foldseek server.

    DISK CACHE: the parsed hit list is cached at _fshits/<ID>.json. If present we read it and never
    touch the (rate-limited) Foldseek API. Cache is seeded from foldseek_test/raw/ where available.

    Returns the SAME shape the downstream expects: a list of {"pdb": <id>, "identity": <0-1 float>}
    for hits released BEFORE CUTOFF and != the query PDB, ordered best-first by Foldseek score.
    Foldseek's seqId is on a 0-100 scale; we divide by 100 so `identity` stays 0-1 like the old
    RCSB sequence_identity (downstream rounds it; train_max_protein_identity stays meaningful).
    On a hard API failure -> return None (system left unchanged, logged by caller).
    """
    FSHITS.mkdir(exist_ok=True)
    cache = FSHITS / f"{exclude_pdb}.json"
    if cache.exists():
        try:
            c = json.loads(cache.read_text())
            if isinstance(c, dict) and c.get("_v") == FSHITS_VERSION:
                return c["hits"]
            # else: v1 (bare list) or older version -> stale, re-fetch to capture alignment fields
        except Exception:
            pass   # corrupt cache -> fall through and re-query

    cif = Path(_cif) if _cif else (SYS / exclude_pdb / "xtal.cif")
    if not cif.exists():
        return []
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pdb", delete=False) as f:
            qpdb = Path(f.name)
        protein_only_pdb(cif, qpdb)
    except Exception as e:
        print(f"    [foldseek] {exclude_pdb}: pdb-prep failed: {str(e)[:80]}", flush=True)
        return []
    # Submit with backoff: the public server intermittently rate-limits (returns a non-ticket body,
    # surfacing as KeyError 'id' in fs.submit). Retry rather than mislabel the system "novel".
    res = None
    try:
        ticket = None
        for attempt in range(5):
            try:
                ticket, _ = fs.submit(qpdb)
                break
            except Exception as e:
                wait = 10 * (attempt + 1)   # 10,20,30,40s backoff
                print(f"    [foldseek] {exclude_pdb}: submit retry {attempt+1}/5 "
                      f"({str(e)[:50]}) -> wait {wait}s", flush=True)
                time.sleep(wait)
        if ticket is None:
            print(f"    [foldseek] {exclude_pdb}: submit failed after retries (rate-limited)", flush=True)
            return None   # signal HARD failure (distinct from genuine empty result -> [])
        status = fs.poll(ticket, every=3.0, cap=180.0)
        if status != "COMPLETE":
            print(f"    [foldseek] {exclude_pdb}: search status={status}", flush=True)
            return None
        res = fs.fetch_result(ticket, 0)
    except Exception as e:
        print(f"    [foldseek] {exclude_pdb}: API error {str(e)[:80]}", flush=True)
        return None
    finally:
        try: qpdb.unlink()
        except Exception: pass
    # Parse alignments (same structure as foldseek_search.top_hits / the verified result schema).
    out = _parse_fs_result(res, exclude_pdb, rows)
    if out is None:
        print(f"    [foldseek] {exclude_pdb}: empty/odd result", flush=True)
        return []
    cache.write_text(json.dumps({"_v": FSHITS_VERSION, "hits": out}))   # versioned; never re-hit API
    return out


def superpose(new_model, ref_model):
    npoly = max([ch.get_polymer() for ch in new_model if len(ch.get_polymer()) > 5], key=len)
    nseq = npoly.make_one_letter_sequence()
    rpolys = [ch.get_polymer() for ch in ref_model if len(ch.get_polymer()) > 5]
    rpoly = max(rpolys, key=lambda p: difflib.SequenceMatcher(None, p.make_one_letter_sequence(), nseq).ratio())
    sup = gemmi.calculate_superposition(npoly, rpoly, gemmi.PolymerType.PeptideL, gemmi.SupSelect.CaP)
    return sup.transform, sup.rmsd, rpoly


# ---- Foldseek-alignment-based superposition (conserved core, robust at low identity) ----------------
# Whole-chain Ca superposition fails for remote homologs (15-45% id): the whole fold differs, but the
# LOCAL active site is conserved. We instead build matched Ca pairs from Foldseek's own structural
# alignment (qAln/dbAln walked against the query's and target's Ca), Kabsch-fit them, and gate on the
# POCKET-LOCAL rmsd (matched residues near the crystal ligand) -- the part that actually matters for
# placing the training ligand.

def _first_poly(model):
    """The FIRST polymer chain in model order (>5 residues). This is the chain Foldseek searched
    (entry 0 == first chain of our protein-only PDB / of the RCSB assembly), so its Ca ordering
    matches qStartPos/dbStartPos. NOTE: must be the first chain, NOT the longest -- multi-copy
    crystals (e.g. 7I9D has chains A-D) would otherwise mis-index the alignment correspondence."""
    for ch in model:
        poly = ch.get_polymer()
        if len(poly) > 5:
            return poly
    return None


def _poly_ca(poly):
    """Ca positions (np array or None per residue) in residue order = Foldseek input-sequence order."""
    cas = []
    for r in poly:
        a = r.find_atom("CA", "*")
        cas.append(None if a is None else np.array([a.pos.x, a.pos.y, a.pos.z]))
    return cas


def _kabsch(P, Q):
    """Rigid transform (R, t) mapping P onto Q (same N, paired). x' = R @ x + t."""
    Pc, Qc = P.mean(0), Q.mean(0)
    H = (P - Pc).T @ (Q - Qc)
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    return R, Qc - R @ Pc


def _matched_ca(hit, qca):
    """Walk qAln/dbAln, pair non-gap columns -> (query_ca[N,3], target_ca[N,3]) numpy arrays.
    qca = query first-chain Ca list; target Ca come from the cached flat 'tCa' string."""
    tcaflat = hit.get("tCa")
    if not tcaflat or not hit.get("qAln") or not hit.get("dbAln"):
        return None, None
    tca = np.array([float(x) for x in tcaflat.split(",")]).reshape(-1, 3)
    qi = (hit.get("qStartPos") or 1) - 1
    ti = (hit.get("dbStartPos") or 1) - 1
    qm, tm = [], []
    for qc, dc in zip(hit["qAln"], hit["dbAln"]):
        if qc != "-" and dc != "-":
            if 0 <= qi < len(qca) and 0 <= ti < len(tca) and qca[qi] is not None:
                qm.append(qca[qi]); tm.append(tca[ti])
        if qc != "-": qi += 1
        if dc != "-": ti += 1
    if len(qm) < 4:
        return None, None
    return np.array(qm), np.array(tm)


class _Tf:
    """Minimal transform wrapper exposing .apply(gemmi.Position) like gemmi.Transform, so the
    existing ligand-placement / write_protein_pdb code works unchanged."""
    def __init__(self, R, t):
        self.R, self.t = R, t
    def apply(self, pos):
        v = self.R @ np.array([pos.x, pos.y, pos.z]) + self.t
        return gemmi.Position(float(v[0]), float(v[1]), float(v[2]))


def align_superpose(hit, qca, qlig_pos):
    """Superpose target->query using Foldseek's alignment, gated on the POCKET-LOCAL core.
    Returns (transform, local_rmsd, n_local) or None if no usable correspondence.
    qlig_pos = query crystal-ligand heavy-atom coords (np array), defines the pocket."""
    qm, tm = _matched_ca(hit, qca)
    if qm is None:
        return None
    # pocket-local subset: matched query residues with a Ca near the crystal ligand
    dmin = np.array([np.min(np.linalg.norm(qlig_pos - q, axis=1)) for q in qm])
    loc = dmin < POCKET_RADIUS
    if loc.sum() < 4:
        # Foldseek's conserved core does NOT cover the ligand site (<4 matched residues near the
        # pocket) -> we have no reliable local frame to place the training ligand. Skip this candidate
        # rather than fit on distant residues (which can fling the ligand 100s of A away).
        return None
    R, t = _kabsch(tm[loc], qm[loc])
    rmsd = float(np.sqrt((((R @ tm[loc].T).T + t - qm[loc]) ** 2).sum(1)).mean())
    return _Tf(R, t), rmsd, int(loc.sum())


def _xyz(x, y, z):
    """Format PDB coordinates (cols 31-54) compatibly with BOTH consumers of these files.

    compute_overlaps.py reads train_ligand/protein PDBs two ways: read_lig_pdb() via str.split()
    (needs whitespace BETWEEN coords), and the multi-copy disp-remap via fixed columns ln[30:38]
    etc. (needs each coord in its 8-wide slot). Standard '%8.3f' breaks split() when |coord|>=100
    because '-100.254' fills all 8 chars and touches the previous field (e.g. '-8.686-144.482').
    Using '%8.2f' keeps every |coord|<1000 to <=7 chars, so each 8-wide field always has a leading
    space -> str.split() separates them AND the fixed columns stay aligned. The 0.01A precision drop
    is irrelevant for the 0.4A-grid shape overlap. Mol* reads the fixed columns either way."""
    return f"{x:8.2f}{y:8.2f}{z:8.2f}"


def write_protein_pdb(poly, transform, dest):
    """Aligned training protein chain (standard residues) as PDB → Mol* renders cartoon."""
    lines = []; serial = 0
    for res in poly:
        for a in res:
            if a.element.name == "H":
                continue
            serial += 1
            p = transform.apply(a.pos)
            lines.append(f"ATOM  {serial:>5d} {a.name[:4]:<4s} {res.name[:3]:>3s} A{res.seqid.num:>4d}    "
                         f"{_xyz(p.x, p.y, p.z)}  1.00  0.00          {a.element.name:>2s}")
    lines.append("END")
    dest.write_text("\n".join(lines) + "\n")


def vol_tanimoto(pa, ra, pb, rb, sp=0.4):
    allp = np.vstack([pa, pb]); allr = np.concatenate([ra, rb])
    lo = allp.min(0) - allr.max() - sp; hi = allp.max(0) + allr.max() + sp
    G = np.stack(np.meshgrid(*[np.arange(lo[i], hi[i], sp) for i in range(3)], indexing="ij"), -1).reshape(-1, 3)
    def occ(p, r):
        ins = np.zeros(len(G), bool)
        for q, rr in zip(p, r): ins |= ((G - q) ** 2).sum(1) <= rr * rr
        return ins
    A, B = occ(pa, ra), occ(pb, rb)
    u = (A | B).sum()
    return float((A & B).sum() / u) if u else 0.0


def process(s):
    sid, het = s["id"], s["ligand"]
    xtal = SYS / sid / "xtal.cif"
    if not xtal.exists():
        return None
    new_m = load(str(xtal))
    qlig = lig_atoms(new_m, het)
    if qlig is None:
        return None
    qpos, qrad = lig_arrays(qlig)
    seq = longest_seq(new_m)   # no longer required for retrieval (Foldseek uses the structure)
    hits = search_pre_cutoff(seq, sid)
    if hits is None:           # hard Foldseek/API failure (rate-limit etc.) -> don't mislabel as novel
        raise RuntimeError("foldseek API hard-failure (rate-limited); leaving system unchanged")
    max_ident = max([h["identity"] for h in hits if h["identity"]], default=None)
    # Query first-chain Ca, in Foldseek input-sequence order (used to build the alignment correspondence).
    qpoly = _first_poly(new_m)
    qca = _poly_ca(qpoly) if qpoly is not None else None
    # Over the top candidates, find the reference ligand with the BEST shape overlap to our crystal
    # ligand. The training protein is superposed onto ours via FOLDSEEK'S OWN structural alignment
    # (conserved-core correspondence) -- whole-chain rigid Ca fitting fails at low identity (15-45%),
    # where the fold differs but the active site is conserved; we gate on the POCKET-LOCAL rmsd.
    best = None   # (overlap, pdb, het, rmsd, ident, aligned_ref_atoms, ref_poly, transform)
    for h in hits[:25]:                  # Foldseek's top ~25 structural neighbors are what matter
        if qca is None or not h.get("qAln"):
            continue                     # no alignment correspondence available -> can't place ligands
        # Enumerate candidate ligands from the DOWNLOADED structure (gemmi), NOT the RCSB
        # nonpolymer_bound_components field -- that field is incomplete and misses the real ligand
        # (e.g. for A2A hits 4EIY/5OM4 it returns just ['NA'], dropping ZMA/T4E). Download once
        # (cached in _refcache), then read het residues straight off the structure.
        try:
            ref_m = load(h["pdb"])
            sup = align_superpose(h, qca, qpos)
        except Exception:
            continue
        if sup is None:
            continue
        tr, rmsd, _nloc = sup
        if rmsd > MAX_LOCAL_RMSD:         # pocket-local alignment unreliable -> overlap meaningless
            continue
        ref_poly = _first_poly(ref_m)     # the chain Foldseek aligned to (entry 0 / first polymer)
        if ref_poly is None:
            continue
        ligs = sorted({r.name for ch in ref_m for r in ch
                       if not r.is_water() and r.het_flag == "H" and druglike(r.name)})
        if not ligs:
            continue
        for hh in ligs:
            ref_res = lig_atoms(ref_m, hh)
            if ref_res is None:
                continue
            aligned = [(a, tr.apply(a.pos)) for a in ref_res if a.element.name != "H"]
            rpos = np.array([[p.x, p.y, p.z] for _, p in aligned])
            rrad = np.array([vdw(a.element.name) for a, _ in aligned])
            tani = vol_tanimoto(qpos, qrad, rpos, rrad)
            if best is None or tani > best[0]:
                best = (tani, h["pdb"], hh, rmsd, h["identity"], aligned, ref_poly, tr)
    if best is None:
        return {"train_pdb": None, "train_max_protein_identity": round(max_ident, 3) if max_ident else None}
    tani, pdb, het_ref, rmsd, ident, aligned, ref_poly, tr = best
    out_lig = SYS / sid / "train_ligand.pdb"
    lines = []
    for i, (a, p) in enumerate(aligned, 1):
        lines.append(f"HETATM{i:>5d} {a.name[:4]:<4s} LIG X   1    {_xyz(p.x, p.y, p.z)}  1.00  0.00          {a.element.name:>2s}")
    lines.append("END")
    out_lig.write_text("\n".join(lines) + "\n")
    write_protein_pdb(ref_poly, tr, SYS / sid / "train_protein.pdb")   # aligned closest-training protein
    return {"train_pdb": pdb, "train_identity": round(ident, 3) if ident else None,
            "train_max_protein_identity": round(max_ident, 3) if max_ident else None,
            "train_het": het_ref, "train_align_rmsd": round(rmsd, 2),
            "train_shape_overlap": round(tani, 3),
            "train_ligand_file": f"systems/{sid}/train_ligand.pdb",
            "train_protein_file": f"systems/{sid}/train_protein.pdb"}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None,
                    help="comma-separated system IDs to (re)process; others untouched")
    a = ap.parse_args()
    only = set(x.strip().upper() for x in a.only.split(",")) if a.only else None

    man = json.loads((HERE / "systems.json").read_text())
    n_done = n_novel = n_err = 0
    for i, s in enumerate(man["systems"]):
        if only is not None and s["id"].upper() not in only:
            continue
        try:
            res = process(s)
        except Exception as e:
            print(f"  {s['id']}: error {str(e)[:80]}", flush=True); res = None; n_err += 1
        if res:
            s.update(res)
            if res.get("train_pdb"):
                n_done += 1
                print(f"  {s['id']} ({s['ligand']}): train {res['train_pdb']} ({res['train_het']}) "
                      f"id={res['train_identity']} overlap={res['train_shape_overlap']}", flush=True)
            else:
                n_novel += 1
                print(f"  {s['id']} ({s['ligand']}): NOVEL (no pre-cutoff ligand-bearing match)", flush=True)
        # incremental write so a mid-run rate-limit doesn't lose completed systems
        (HERE / "systems.json").write_text(json.dumps(man, indent=2, allow_nan=False))
        time.sleep(0.5)   # be polite to the public Foldseek server between systems
    print(f"\ndone: {n_done} with training ligand, {n_novel} novel, {n_err} errors", flush=True)


if __name__ == "__main__":
    main()
