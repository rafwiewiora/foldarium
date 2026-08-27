"""Auditable protein-pocket training-similarity scoring.

The historical Foldarium CAMEO analysis used Foldseek to retrieve structural
neighbors, retained structures released before the AlphaFold 3 training cutoff,
aligned their conserved pocket cores, and measured the maximum volume overlap
between a carried training ligand and the query ligand.  This module keeps that
scientific policy independent from the old benchmark viewer and makes failures
explicit so an unavailable candidate can never become evidence of novelty.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from . import foldseek

TRAINING_CUTOFF = "2021-09-30"
FOLDSEEK_DATABASE = "pdb100"
FOLDSEEK_MODE = "3diaa"
FOLDSEEK_ROWS = 40
TRAINING_HIT_LIMIT = 25
POCKET_RADIUS_ANGSTROM = 8.0
MAX_LOCAL_RMSD_ANGSTROM = 3.0
NOVELTY_THRESHOLD = 0.25
SCORER_VERSION = "foldseek-pdb100-carried-ligand-overlap/v7"
CACHE_VERSION = 2
MAX_STRUCTURE_BYTES = 64 * 1024 * 1024
USER_AGENT = "Foldarium weekly training-similarity audit/1.0"

VDW_RADII = {
    "B": 1.92,
    "BR": 1.85,
    "C": 1.70,
    "CL": 1.75,
    "F": 1.47,
    "I": 1.98,
    "N": 1.55,
    "O": 1.52,
    "P": 1.80,
    "S": 1.80,
}

_EXCLUDED_LIGANDS = frozenset(
    """
    HOH DOD NA CL MG ZN CA K MN FE FE2 FE3 CU CU1 NI CO CD HG CS BA SR BR IOD I
    RB LI PB PT AU AG SO4 PO4 PI NO3 ACT EDO GOL PEG PG4 PGE 1PE 2PE P6G PE3
    PE4 PEU MPD DMS BME MES EPE TRS TAR CIT FLC FMT IPA BO3 NH4 AZI CAC OXL SCN
    BCT CO3 BCN BTB MRD IMD DTT DTV TLA SUC EOH ACE BU3 MLA MLI 15P 7PE 12P PG0
    144 SIN POL OCT D10 LDA UNX UNL UNK NAG MAN BMA FUC GAL GLC NDG BGC FUL XYS
    MAL TRE A2G NGA SIA ATP ADP AMP ANP ACP AGS APC GTP GDP GNP GSP GMP CTP UTP
    UDP UMP TTP TMP NAD NAP NDP FAD FMN COA ACO SAM SAH HEM HEC HEA HAS PLP PMP
    TPP BTI BTN B12 GTN CMP UD1 5GP CLR CHS CHD CLL Y01 OLA OLB OLC OLE PLM STE
    MYR PEE PCW PC1 PEK PSC PEF PX4 3PE 9PE PGV PGW POV PTY PEV PEH CDL SPH PLC
    PE PC PS PG PA SQD LHG LMT LMN LMG LMU DMU UMQ UDM JZ4 BOG BNG OCT NG6 DDR
    HP6 DD9 MC3 HC3 SDS DAO C8E C10 C12 C14 7E8 7E9 P15 7PH OLI ELA TWT D12 DDM
    DMP DHD MYS PLD PLO PX2 MSE SEP TPO PTR CSO CSD CME CSX KCX LLP MLY M3L CGU
    PCA SAC ALY DAL CAS OCS NEP HIC MHO
    """.split()
)

_PDB_ID = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHAIN_NAMES = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class TrainingSimilarityError(RuntimeError):
    """Raised when an audit input or authoritative upstream result is unusable."""


@dataclass(frozen=True)
class AtomCloud:
    """Heavy-atom positions and van der Waals radii."""

    positions: Any
    radii: Any


@dataclass(frozen=True)
class TrainingAnalog:
    """One carried ligand from a pre-cutoff Foldseek neighbor."""

    pdb_id: str
    ligand: str
    identity: float | None
    local_rmsd: float
    local_residue_count: int
    hit_rank: int
    cloud: AtomCloud


def _science() -> tuple[Any, Any]:
    try:
        import gemmi
        import numpy
    except ImportError as exc:
        raise TrainingSimilarityError(
            "training-similarity scoring requires the pipeline evaluation extras"
        ) from exc
    return gemmi, numpy


def file_sha256(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(_json_bytes(value))
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalized_pdb_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if not _PDB_ID.fullmatch(normalized):
        raise TrainingSimilarityError(f"invalid PDB ID: {value!r}")
    return normalized


def is_druglike_ligand(name: str) -> bool:
    normalized = str(name).strip().upper()
    return (
        3 <= len(normalized) <= 5
        and normalized not in _EXCLUDED_LIGANDS
        and normalized.isalnum()
    )


def vdw_radius(element: str) -> float:
    return VDW_RADII.get(str(element).upper(), 1.70)


def read_model(path: str | Path) -> Any:
    gemmi, _numpy = _science()
    try:
        structure = gemmi.read_structure(str(path))
        structure.setup_entities()
        if len(structure) < 1:
            raise ValueError("no model")
        return structure[0]
    except Exception as exc:
        raise TrainingSimilarityError(f"could not parse structure {path}") from exc


def atom_cloud(model: Any, *, residue_name: str | None = None) -> AtomCloud:
    _gemmi, numpy = _science()
    positions: list[list[float]] = []
    radii: list[float] = []
    wanted = residue_name.upper() if residue_name else None
    for chain in model:
        for residue in chain:
            if wanted and residue.name.upper() != wanted:
                continue
            for atom in residue:
                element = atom.element.name.upper()
                if element == "H":
                    continue
                positions.append([atom.pos.x, atom.pos.y, atom.pos.z])
                radii.append(vdw_radius(element))
    if not positions:
        raise TrainingSimilarityError("structure has no matching heavy atoms")
    return AtomCloud(
        positions=numpy.asarray(positions, dtype=float),
        radii=numpy.asarray(radii, dtype=float),
    )


def ligand_cloud(path: str | Path, component_id: str | None = None) -> AtomCloud:
    model = read_model(path)
    return atom_cloud(model, residue_name=component_id)


def protein_only_pdb(source: str | Path, destination: str | Path) -> None:
    gemmi, _numpy = _science()
    try:
        structure = gemmi.read_structure(str(source))
        structure.setup_entities()
        structure.remove_ligands_and_waters()
        structure.remove_empty_chains()
        if len(structure) < 1 or not list(structure[0]):
            raise ValueError("no polymer chains")
        for index, chain in enumerate(structure[0]):
            chain.name = _CHAIN_NAMES[index % len(_CHAIN_NAMES)]
        structure.write_pdb(str(destination))
    except Exception as exc:
        raise TrainingSimilarityError("could not prepare Foldseek protein query") from exc


def first_polymer_pdb(source: str | Path, destination: str | Path) -> None:
    """Write only Foldseek entry zero, matching the public API query policy."""

    with tempfile.NamedTemporaryFile(suffix=".pdb", delete=False) as handle:
        intermediate = Path(handle.name)
    try:
        protein_only_pdb(source, intermediate)
        lines = intermediate.read_text().splitlines()
        atom_lines = [line for line in lines if line.startswith("ATOM")]
        if not atom_lines:
            raise TrainingSimilarityError("Foldseek query has no protein atoms")
        first_chain = atom_lines[0][21:22]
        selected = [
            line
            for line in lines
            if line.startswith("ATOM") and line[21:22] == first_chain
        ]
        if not selected:
            raise TrainingSimilarityError("Foldseek query first chain is empty")
        Path(destination).write_text("\n".join(selected + ["END", ""]))
    finally:
        intermediate.unlink(missing_ok=True)


def _first_polymer(model: Any) -> Any | None:
    for chain in model:
        polymer = chain.get_polymer()
        if len(polymer) > 5:
            return polymer
    return None


def _polymer_ca(polymer: Any) -> list[Any | None]:
    _gemmi, numpy = _science()
    result: list[Any | None] = []
    for residue in polymer:
        atom = residue.find_atom("CA", "*")
        result.append(
            None
            if atom is None
            else numpy.asarray([atom.pos.x, atom.pos.y, atom.pos.z], dtype=float)
        )
    return result


def _matched_ca(hit: Mapping[str, Any], query_ca: Sequence[Any | None]) -> tuple[Any, Any] | None:
    _gemmi, numpy = _science()
    target_flat = hit.get("tCa")
    query_alignment = hit.get("qAln")
    target_alignment = hit.get("dbAln")
    if not isinstance(target_flat, str) or not isinstance(query_alignment, str) or not isinstance(
        target_alignment, str
    ):
        return None
    try:
        target_ca = numpy.asarray(
            [float(value) for value in target_flat.split(",")], dtype=float
        ).reshape(-1, 3)
    except (ValueError, TypeError):
        return None
    query_index = int(hit.get("qStartPos") or 1) - 1
    target_index = int(hit.get("dbStartPos") or 1) - 1
    query_matched: list[Any] = []
    target_matched: list[Any] = []
    for query_code, target_code in zip(query_alignment, target_alignment):
        if (
            query_code != "-"
            and target_code != "-"
            and 0 <= query_index < len(query_ca)
            and 0 <= target_index < len(target_ca)
            and query_ca[query_index] is not None
        ):
            query_matched.append(query_ca[query_index])
            target_matched.append(target_ca[target_index])
        if query_code != "-":
            query_index += 1
        if target_code != "-":
            target_index += 1
    if len(query_matched) < 4:
        return None
    return numpy.asarray(query_matched), numpy.asarray(target_matched)


def _kabsch(source: Any, target: Any) -> tuple[Any, Any]:
    _gemmi, numpy = _science()
    source_center = source.mean(0)
    target_center = target.mean(0)
    covariance = (source - source_center).T @ (target - target_center)
    left, _singular, right = numpy.linalg.svd(covariance)
    sign = numpy.sign(numpy.linalg.det(right.T @ left.T))
    rotation = right.T @ numpy.diag([1.0, 1.0, sign]) @ left.T
    return rotation, target_center - rotation @ source_center


def pocket_superposition(
    hit: Mapping[str, Any],
    query_ca: Sequence[Any | None],
    pocket_positions: Any,
) -> tuple[Any, Any, float, int] | None:
    _gemmi, numpy = _science()
    matched = _matched_ca(hit, query_ca)
    if matched is None:
        return None
    query_matched, target_matched = matched
    distances = numpy.asarray(
        [
            numpy.min(numpy.linalg.norm(pocket_positions - position, axis=1))
            for position in query_matched
        ]
    )
    local = distances < POCKET_RADIUS_ANGSTROM
    count = int(local.sum())
    if count < 4:
        return None
    rotation, translation = _kabsch(target_matched[local], query_matched[local])
    transformed = (rotation @ target_matched[local].T).T + translation
    rmsd = float(
        numpy.sqrt(((transformed - query_matched[local]) ** 2).sum(1).mean())
    )
    return rotation, translation, rmsd, count


def volume_tanimoto(left: AtomCloud, right: AtomCloud, spacing: float = 0.4) -> float:
    """Return the historical grid-based vdW volume Tanimoto."""

    _gemmi, numpy = _science()
    if spacing <= 0:
        raise TrainingSimilarityError("volume grid spacing must be positive")
    left_low = left.positions.min(0) - left.radii.max()
    left_high = left.positions.max(0) + left.radii.max()
    right_low = right.positions.min(0) - right.radii.max()
    right_high = right.positions.max(0) + right.radii.max()
    if numpy.any(numpy.minimum(left_high, right_high) <= numpy.maximum(left_low, right_low)):
        return 0.0
    low = numpy.minimum(left_low, right_low) - spacing
    high = numpy.maximum(left_high, right_high) + spacing
    dimensions = [max(1, math.ceil((high[i] - low[i]) / spacing)) for i in range(3)]
    if math.prod(dimensions) > 20_000_000:
        raise TrainingSimilarityError("ligand overlap grid exceeds the safety limit")
    axes = [
        numpy.arange(low[index], high[index], spacing) for index in range(3)
    ]
    grid = numpy.stack(numpy.meshgrid(*axes, indexing="ij"), -1).reshape(-1, 3)

    def occupied(cloud: AtomCloud) -> Any:
        mask = numpy.zeros(len(grid), dtype=bool)
        for position, radius in zip(cloud.positions, cloud.radii):
            mask |= ((grid - position) ** 2).sum(1) <= radius * radius
        return mask

    left_mask = occupied(left)
    right_mask = occupied(right)
    union = int((left_mask | right_mask).sum())
    return float((left_mask & right_mask).sum() / union) if union else 0.0


def parse_foldseek_hits(
    result: Mapping[str, Any],
    *,
    exclude_pdb: str,
    release_dates: Mapping[str, str | None],
    rows: int = FOLDSEEK_ROWS,
) -> list[dict[str, Any]]:
    """Validate and filter one Foldseek result to unique pre-cutoff PDB hits."""

    excluded = _normalized_pdb_id(exclude_pdb)
    try:
        alignments = result["results"][0]["alignments"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise TrainingSimilarityError("Foldseek result has no alignment table") from exc
    if not isinstance(alignments, list):
        raise TrainingSimilarityError("Foldseek alignments are invalid")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alignment in alignments[:rows]:
        if not isinstance(alignment, Mapping):
            continue
        pdb_id = foldseek.parse_pdbid(alignment.get("target", ""))
        if not pdb_id or pdb_id == excluded or pdb_id in seen:
            continue
        seen.add(pdb_id)
        released = release_dates.get(pdb_id)
        if not isinstance(released, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:T.*)?", released
        ):
            raise TrainingSimilarityError(
                f"release date is unavailable for Foldseek hit {pdb_id}"
            )
        if released[:10] >= TRAINING_CUTOFF:
            continue
        raw_identity = alignment.get("seqId")
        identity = (
            float(raw_identity) / 100.0
            if isinstance(raw_identity, (int, float)) and not isinstance(raw_identity, bool)
            else None
        )
        hits.append(
            {
                "pdb": pdb_id,
                "identity": identity,
                "qStartPos": alignment.get("qStartPos"),
                "dbStartPos": alignment.get("dbStartPos"),
                "qAln": alignment.get("qAln"),
                "dbAln": alignment.get("dbAln"),
                "tCa": alignment.get("tCa"),
            }
        )
    return hits


def search_pre_cutoff(
    query_structure: str | Path,
    *,
    exclude_pdb: str,
    cache_directory: str | Path,
    cache_label: str,
    retry_count: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Run and cache one authoritative Foldseek query."""

    pdb_id = _normalized_pdb_id(exclude_pdb)
    source = Path(query_structure)
    query_digest = file_sha256(source)
    cache_root = Path(cache_directory)
    cache_path = foldseek_cache_path(
        source,
        exclude_pdb=pdb_id,
        cache_directory=cache_root,
        cache_label=cache_label,
    )
    key = cache_path.stem
    raw_path = cache_root / "foldseek-raw" / f"{key}.json"
    ticket_path = cache_root / "foldseek-tickets" / f"{key}.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise TrainingSimilarityError("Foldseek cache is corrupt") from exc
        expected = {
            "cache_version": CACHE_VERSION,
            "query_sha256": query_digest,
            "cutoff": TRAINING_CUTOFF,
            "database": FOLDSEEK_DATABASE,
            "mode": FOLDSEEK_MODE,
        }
        if all(cached.get(field) == value for field, value in expected.items()):
            if isinstance(cached.get("error"), str):
                raise TrainingSimilarityError(cached["error"])
            if isinstance(cached.get("hits"), list):
                return list(cached["hits"])

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".pdb", delete=False) as handle:
        query_pdb = Path(handle.name)
    try:
        protein_only_pdb(source, query_pdb)
        result: Mapping[str, Any] | None = None
        if raw_path.is_file():
            try:
                candidate = json.loads(raw_path.read_text())
                if isinstance(candidate, Mapping):
                    result = candidate
            except (OSError, json.JSONDecodeError):
                result = None
        if result is None:
            ticket: str | None = None
            if ticket_path.is_file():
                try:
                    ticket_cache = json.loads(ticket_path.read_text())
                    if (
                        ticket_cache.get("query_sha256") == query_digest
                        and isinstance(ticket_cache.get("ticket"), str)
                    ):
                        ticket = ticket_cache["ticket"]
                except (OSError, json.JSONDecodeError, AttributeError):
                    ticket = None
            last_error: Exception | None = None
            if ticket is None:
                for attempt in range(retry_count):
                    try:
                        ticket, _status = foldseek.submit(query_pdb)
                        _write_json(
                            ticket_path,
                            {
                                "query_sha256": query_digest,
                                "ticket": ticket,
                                "database": FOLDSEEK_DATABASE,
                                "mode": FOLDSEEK_MODE,
                            },
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt + 1 < retry_count:
                            sleep(10.0 * (attempt + 1))
            if ticket is None:
                raise TrainingSimilarityError("Foldseek submission failed") from last_error
            status = foldseek.poll(ticket, every=3.0, cap=600.0)
            if status != "COMPLETE":
                if status == "ERROR":
                    ticket_path.unlink(missing_ok=True)
                raise TrainingSimilarityError(f"Foldseek search ended with status {status}")
            result = foldseek.fetch_result(ticket)
            _write_json(raw_path, result)
            ticket_path.unlink(missing_ok=True)
        candidate_ids: list[str] = []
        try:
            alignments = result["results"][0]["alignments"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise TrainingSimilarityError("Foldseek result has no alignment table") from exc
        for alignment in alignments[:FOLDSEEK_ROWS]:
            if isinstance(alignment, Mapping):
                candidate = foldseek.parse_pdbid(alignment.get("target", ""))
                if candidate and candidate != pdb_id and candidate not in candidate_ids:
                    candidate_ids.append(candidate)
        dates = foldseek.release_dates(candidate_ids)
        hits = parse_foldseek_hits(
            result, exclude_pdb=pdb_id, release_dates=dates
        )
        _write_json(
            cache_path,
            {
                "cache_version": CACHE_VERSION,
                "query_sha256": query_digest,
                "cutoff": TRAINING_CUTOFF,
                "database": FOLDSEEK_DATABASE,
                "mode": FOLDSEEK_MODE,
                "raw_result_sha256": file_sha256(raw_path),
                "hits": hits,
            },
        )
        return hits
    finally:
        query_pdb.unlink(missing_ok=True)


def foldseek_cache_path(
    query_structure: str | Path,
    *,
    exclude_pdb: str,
    cache_directory: str | Path,
    cache_label: str,
) -> Path:
    pdb_id = _normalized_pdb_id(exclude_pdb)
    digest = file_sha256(query_structure)
    key = f"{cache_label}-{pdb_id}-{digest[:16]}"
    return Path(cache_directory) / "foldseek-hits" / f"{key}.json"


def foldseek_cache_provenance(cache_path: str | Path) -> dict[str, Any]:
    try:
        cached = json.loads(Path(cache_path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TrainingSimilarityError("Foldseek hit cache provenance is invalid") from exc
    if not isinstance(cached, dict):
        raise TrainingSimilarityError("Foldseek hit cache provenance is invalid")
    local = cached.get("local_database_provenance")
    database = local.get("database") if isinstance(local, dict) else None
    return {
        "foldseek_backend": cached.get("backend", "public-api"),
        "foldseek_raw_result_sha256": cached.get("raw_result_sha256"),
        "foldseek_database_snapshot": (
            {
                key: database.get(key)
                for key in (
                    "requested_database",
                    "foldseek_release",
                    "foldseek_version",
                    "downloaded_at",
                    "database_file_count",
                    "database_total_bytes",
                )
            }
            if isinstance(database, dict)
            else None
        ),
    }


def import_local_foldseek_tsv(
    tsv_path: str | Path,
    manifest_path: str | Path,
    cache_directory: str | Path,
) -> dict[str, int]:
    """Seed API-compatible hit caches from one batched local Foldseek search."""

    manifest_file = Path(manifest_path)
    try:
        manifest = json.loads(manifest_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TrainingSimilarityError("local Foldseek query manifest is invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("queries"), list):
        raise TrainingSimilarityError("local Foldseek query manifest is invalid")
    queries: dict[str, dict[str, Any]] = {}
    for row in manifest["queries"]:
        if not isinstance(row, dict):
            raise TrainingSimilarityError("local Foldseek query manifest row is invalid")
        pdb_id = _normalized_pdb_id(row.get("item_id", ""))
        if (
            not isinstance(row.get("source_sha256"), str)
            or not _SHA256_RE.fullmatch(row["source_sha256"])
            or not isinstance(row.get("cache_label"), str)
        ):
            raise TrainingSimilarityError("local Foldseek query provenance is invalid")
        queries[pdb_id] = row
    grouped: dict[str, list[dict[str, Any]]] = {pdb_id: [] for pdb_id in queries}
    all_targets: set[str] = set()
    try:
        lines = Path(tsv_path).read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TrainingSimilarityError("local Foldseek result TSV is unavailable") from exc
    for line_number, line in enumerate(lines, 1):
        fields = line.split("\t")
        if len(fields) != 8:
            raise TrainingSimilarityError(
                f"local Foldseek TSV row {line_number} has {len(fields)} fields"
            )
        query_name, target, fident, qstart, tstart, qaln, taln, tca = fields
        query_match = re.match(r"^([0-9][A-Za-z0-9]{3})(?:[_|.\s-]|$)", query_name)
        if not query_match:
            raise TrainingSimilarityError(
                f"local Foldseek TSV row {line_number} has an invalid query"
            )
        query_id = query_match.group(1).upper()
        if query_id not in grouped:
            continue
        target_id = foldseek.parse_pdbid(target)
        if not target_id:
            continue
        try:
            identity = float(fident)
            query_start = int(qstart)
            target_start = int(tstart)
        except ValueError as exc:
            raise TrainingSimilarityError(
                f"local Foldseek TSV row {line_number} has invalid numbers"
            ) from exc
        if 0.0 <= identity <= 1.0:
            identity *= 100.0
        grouped[query_id].append(
            {
                "target": target,
                "seqId": identity,
                "qStartPos": query_start,
                "dbStartPos": target_start,
                "qAln": qaln,
                "dbAln": taln,
                "tCa": tca,
            }
        )
        all_targets.add(target_id)
    dates = foldseek.release_dates(all_targets)
    cache_root = Path(cache_directory)
    result_digest = file_sha256(tsv_path)
    counts = {
        "query_count": len(queries),
        "row_count": len(lines),
        "hit_count": 0,
        "unknown_query_count": 0,
    }
    for pdb_id, query in queries.items():
        result = {"results": [{"alignments": [grouped[pdb_id]]}]}
        key = (
            f"{query['cache_label']}-{pdb_id}-"
            f"{query['source_sha256'][:16]}"
        )
        cache_path = cache_root / "foldseek-hits" / f"{key}.json"
        error: str | None = None
        try:
            hits = parse_foldseek_hits(
                result,
                exclude_pdb=pdb_id,
                release_dates=dates,
            )
        except TrainingSimilarityError as exc:
            hits = []
            error = str(exc)
            counts["unknown_query_count"] += 1
        _write_json(
            cache_path,
            {
                "cache_version": CACHE_VERSION,
                "query_sha256": query["source_sha256"],
                "cutoff": TRAINING_CUTOFF,
                "database": FOLDSEEK_DATABASE,
                "mode": FOLDSEEK_MODE,
                "raw_result_sha256": result_digest,
                "backend": "local-foldseek-batch",
                "local_database_provenance": manifest.get("database_provenance"),
                "hits": hits,
                "error": error,
            },
        )
        counts["hit_count"] += len(hits)
    return counts


def download_rcsb_structure(
    pdb_id: str,
    cache_directory: str | Path,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Path:
    normalized = _normalized_pdb_id(pdb_id)
    destination = Path(cache_directory) / "rcsb-structures" / f"{normalized}.cif"
    if destination.is_file() and destination.stat().st_size:
        return destination
    request = Request(
        f"https://files.rcsb.org/download/{normalized}.cif",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with opener(request, timeout=60) as response:
            data = response.read(MAX_STRUCTURE_BYTES + 1)
    except Exception as exc:
        raise TrainingSimilarityError(
            f"could not download training structure {normalized}"
        ) from exc
    if not data or len(data) > MAX_STRUCTURE_BYTES:
        raise TrainingSimilarityError(
            f"training structure {normalized} is empty or too large"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=destination.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _training_ligands(model: Any) -> list[tuple[str, Any]]:
    by_name: dict[str, Any] = {}
    for chain in model:
        for residue in chain:
            name = residue.name.upper()
            if (
                residue.het_flag == "H"
                and not residue.is_water()
                and is_druglike_ligand(name)
                and (name not in by_name or len(residue) > len(by_name[name]))
            ):
                by_name[name] = residue
    return sorted(by_name.items())


def collect_training_analogs(
    query_structure: str | Path,
    pocket: AtomCloud,
    hits: Sequence[Mapping[str, Any]],
    *,
    reference_loader: Callable[[str], str | Path],
    hit_limit: int = TRAINING_HIT_LIMIT,
) -> tuple[list[TrainingAnalog], list[dict[str, Any]]]:
    """Carry all usable ligands from Foldseek neighbors into the query frame."""

    _gemmi, numpy = _science()
    query_model = read_model(query_structure)
    query_polymer = _first_polymer(query_model)
    if query_polymer is None:
        raise TrainingSimilarityError("query structure has no searchable polymer")
    query_ca = _polymer_ca(query_polymer)
    analogs: list[TrainingAnalog] = []
    failures: list[dict[str, Any]] = []
    for rank, hit in enumerate(hits[:hit_limit], 1):
        pdb_id = str(hit.get("pdb", "")).upper()
        if not _PDB_ID.fullmatch(pdb_id):
            failures.append({"rank": rank, "reason": "invalid-hit-pdb"})
            continue
        if _matched_ca(hit, query_ca) is None:
            failures.append(
                {
                    "pdb_id": pdb_id,
                    "rank": rank,
                    "reason": "incomplete-foldseek-alignment",
                }
            )
            continue
        try:
            superposition = pocket_superposition(
                hit, query_ca, pocket.positions
            )
            if superposition is None:
                continue
            rotation, translation, rmsd, local_count = superposition
            if rmsd > MAX_LOCAL_RMSD_ANGSTROM:
                continue
            reference_model = read_model(reference_loader(pdb_id))
            for ligand_name, residue in _training_ligands(reference_model):
                source = atom_cloud_for_residue(residue)
                transformed = (rotation @ source.positions.T).T + translation
                analogs.append(
                    TrainingAnalog(
                        pdb_id=pdb_id,
                        ligand=ligand_name,
                        identity=(
                            float(hit["identity"])
                            if isinstance(hit.get("identity"), (int, float))
                            else None
                        ),
                        local_rmsd=rmsd,
                        local_residue_count=local_count,
                        hit_rank=rank,
                        cloud=AtomCloud(transformed, numpy.asarray(source.radii)),
                    )
                )
        except Exception as exc:
            failures.append(
                {
                    "pdb_id": pdb_id,
                    "rank": rank,
                    "reason": type(exc).__name__,
                }
            )
    return analogs, failures


def atom_cloud_for_residue(residue: Any) -> AtomCloud:
    _gemmi, numpy = _science()
    positions: list[list[float]] = []
    radii: list[float] = []
    for atom in residue:
        element = atom.element.name.upper()
        if element == "H":
            continue
        positions.append([atom.pos.x, atom.pos.y, atom.pos.z])
        radii.append(vdw_radius(element))
    if not positions:
        raise TrainingSimilarityError("ligand residue has no heavy atoms")
    return AtomCloud(numpy.asarray(positions), numpy.asarray(radii))


def best_similarity(
    query_ligand: AtomCloud,
    analogs: Iterable[TrainingAnalog],
    *,
    maximum_hit_rank: int = TRAINING_HIT_LIMIT,
) -> tuple[float | None, TrainingAnalog | None]:
    best_score: float | None = None
    best_analog: TrainingAnalog | None = None
    for analog in analogs:
        if analog.hit_rank > maximum_hit_rank:
            continue
        score = volume_tanimoto(query_ligand, analog.cloud)
        if best_score is None or score > best_score:
            best_score = score
            best_analog = analog
    return best_score, best_analog


def classify_similarity(
    score: float | None,
    *,
    failures: Sequence[Mapping[str, Any]],
    hit_count: int,
) -> tuple[str, str]:
    if score is not None and score >= NOVELTY_THRESHOLD:
        return "familiar", "training-ligand-overlap-at-least-0.25"
    if failures:
        return "unknown", "incomplete-training-candidate-evaluation"
    if hit_count == 0:
        return "novel", "confirmed-empty-pre-cutoff-foldseek-result"
    if score is None:
        return "novel", "no-usable-pre-cutoff-ligand-pocket-analog"
    return "novel", "training-ligand-overlap-below-0.25"


def similarity_result(
    query_ligand: AtomCloud,
    analogs: Sequence[TrainingAnalog],
    failures: Sequence[Mapping[str, Any]],
    hits: Sequence[Mapping[str, Any]],
    *,
    maximum_hit_rank: int = TRAINING_HIT_LIMIT,
) -> dict[str, Any]:
    score, best = best_similarity(
        query_ligand, analogs, maximum_hit_rank=maximum_hit_rank
    )
    relevant_failures = [
        dict(failure)
        for failure in failures
        if int(failure.get("rank", maximum_hit_rank + 1)) <= maximum_hit_rank
    ]
    relevant_hits = list(hits[:maximum_hit_rank])
    classification, reason = classify_similarity(
        score, failures=relevant_failures, hit_count=len(relevant_hits)
    )
    identities = [
        float(hit["identity"])
        for hit in relevant_hits
        if isinstance(hit.get("identity"), (int, float))
    ]
    return {
        "classification": classification,
        "reason": reason,
        "novel": (
            True
            if classification == "novel"
            else False
            if classification == "familiar"
            else None
        ),
        "train_pdb": best.pdb_id if best else None,
        "train_het": best.ligand if best else None,
        "train_identity": round(best.identity, 4) if best and best.identity is not None else None,
        "train_max_protein_identity": round(max(identities), 4) if identities else None,
        "train_align_rmsd": round(best.local_rmsd, 3) if best else None,
        "train_local_residue_count": best.local_residue_count if best else None,
        "train_hit_rank": best.hit_rank if best else None,
        "train_shape_overlap": round(score, 4) if score is not None else None,
        "foldseek_hit_count": len(relevant_hits),
        "training_analog_count": sum(
            1 for analog in analogs if analog.hit_rank <= maximum_hit_rank
        ),
        "candidate_failures": relevant_failures,
        "cutoff": TRAINING_CUTOFF,
        "novel_threshold": NOVELTY_THRESHOLD,
        "pocket_radius_angstrom": POCKET_RADIUS_ANGSTROM,
        "maximum_local_rmsd_angstrom": MAX_LOCAL_RMSD_ANGSTROM,
        "foldseek_database": FOLDSEEK_DATABASE,
        "foldseek_mode": FOLDSEEK_MODE,
        "scorer_version": SCORER_VERSION,
    }


__all__ = [
    "CACHE_VERSION",
    "FOLDSEEK_DATABASE",
    "FOLDSEEK_MODE",
    "MAX_LOCAL_RMSD_ANGSTROM",
    "NOVELTY_THRESHOLD",
    "POCKET_RADIUS_ANGSTROM",
    "SCORER_VERSION",
    "TRAINING_CUTOFF",
    "TRAINING_HIT_LIMIT",
    "AtomCloud",
    "TrainingAnalog",
    "TrainingSimilarityError",
    "atom_cloud",
    "atom_cloud_for_residue",
    "best_similarity",
    "classify_similarity",
    "collect_training_analogs",
    "download_rcsb_structure",
    "file_sha256",
    "first_polymer_pdb",
    "foldseek_cache_path",
    "foldseek_cache_provenance",
    "import_local_foldseek_tsv",
    "is_druglike_ligand",
    "ligand_cloud",
    "parse_foldseek_hits",
    "pocket_superposition",
    "protein_only_pdb",
    "read_model",
    "search_pre_cutoff",
    "similarity_result",
    "volume_tanimoto",
]
