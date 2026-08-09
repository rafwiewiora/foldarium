"""Reproducible, heavy-atom ProLIF summaries for exact predicted poses.

The weekly models do not reliably contain explicit hydrogens.  ProLIF 2.2's
implicit-hydrogen interactions make it possible to calculate one comparable
fingerprint from every method without inventing hydrogen coordinates.  The
public quiz only needs ``count``; the remaining fields are provenance/audit
data and may stay in the private stage record.
"""

from __future__ import annotations

import math
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROLIF_VERSION = "2.2.0"
RDKIT_VERSION = "2026.3.4"
INTERACTION_POLICY = "prolif-vdwcontact-distance-unique-residue-pdb/v1"
VICINITY_CUTOFF_ANGSTROM = 6.0
VDW_RADII_PRESET = "rdkit"
PROTEIN_PARSER_POLICY = "pdb-fixed-columns-elements-and-coordinates/v1"

# AI-predicted receptors contain no explicit hydrogens and can contain metals or
# non-standard residues.  A VdW-only ProLIF fingerprint is the reproducible
# intersection available for every pose: it requires only elements and exact
# coordinates, and avoids inventing hydrogens or guessing protein bond orders.
INTERACTION_TYPES = ("VdWContact",)


class InteractionFingerprintError(RuntimeError):
    """Raised when an exact pose cannot be fingerprinted reproducibly."""


def _dependencies() -> tuple[Any, Any, Any]:
    try:
        import prolif
        from rdkit import Chem
        from rdkit.Geometry import Point3D
    except (ImportError, ModuleNotFoundError) as exc:
        raise InteractionFingerprintError(
            "interaction scoring requires ProLIF and RDKit; install the "
            "pipeline 'interactions' extra"
        ) from exc
    return prolif, Chem, Point3D


def _vdw_detector() -> Any:
    try:
        from prolif.interactions import VdWContact
    except (ImportError, ModuleNotFoundError) as exc:
        raise InteractionFingerprintError(
            "interaction scoring requires ProLIF; install the pipeline "
            "'interactions' extra"
        ) from exc
    return VdWContact(preset=VDW_RADII_PRESET)


def _installed_versions() -> dict[str, str]:
    try:
        versions = {
            "prolif": metadata.version("prolif"),
            "rdkit": metadata.version("rdkit"),
        }
    except metadata.PackageNotFoundError as exc:
        raise InteractionFingerprintError(
            "could not determine installed ProLIF/RDKit versions"
        ) from exc
    expected = {
        "prolif": PROLIF_VERSION,
        "rdkit": RDKIT_VERSION,
    }
    if versions != expected:
        raise InteractionFingerprintError(
            f"interaction scoring requires pinned versions {expected}; found {versions}"
        )
    return versions


def _coordinates(value: Iterable[Sequence[float]]) -> list[tuple[float, float, float]]:
    coordinates: list[tuple[float, float, float]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 3:
            raise InteractionFingerprintError(
                f"ligand coordinate {index} must contain exactly three numbers"
            )
        if any(isinstance(axis, bool) or not isinstance(axis, (int, float)) for axis in raw):
            raise InteractionFingerprintError(
                f"ligand coordinate {index} must contain exactly three numbers"
            )
        xyz = tuple(float(axis) for axis in raw)
        if not all(math.isfinite(axis) for axis in xyz):
            raise InteractionFingerprintError(
                f"ligand coordinate {index} contains a non-finite number"
            )
        coordinates.append(xyz)
    if not coordinates:
        raise InteractionFingerprintError("ligand coordinates cannot be empty")
    return coordinates


def _residue_label(value: Any) -> str:
    label = str(value).strip()
    if not label:
        raise InteractionFingerprintError("ProLIF returned an empty residue identifier")
    return label


def _protein_vicinity_records(
    path: Path,
    ligand_coordinates: Sequence[tuple[float, float, float]],
) -> list[tuple[tuple[str, str, str, str], str, tuple[float, float, float]]]:
    """Keep complete protein residues having an atom within the 6 A vicinity.

    ProLIF's generic fingerprint runner otherwise has to visit every residue in
    a full predicted complex for every pose. This filters fixed-width PDB atom
    records before RDKit parsing, retaining every atom of every nearby residue.
    It is an exact prefilter for ProLIF's own ``vicinity_cutoff``: no residue
    outside the cutoff can yield a configured interaction.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise InteractionFingerprintError(
            "could not read the pose-specific predicted protein"
        ) from exc

    cutoff_squared = VICINITY_CUTOFF_ANGSTROM * VICINITY_CUTOFF_ANGSTROM
    atom_records: list[
        tuple[tuple[str, str, str, str], str, tuple[float, float, float]]
    ] = []
    nearby_residues: set[tuple[str, str, str, str]] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        if len(line) < 54:
            raise InteractionFingerprintError(
                f"predicted protein has a truncated atom record at line {line_number}"
            )
        residue_key = (line[21:22], line[22:26], line[26:27], line[17:20])
        try:
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as exc:
            raise InteractionFingerprintError(
                f"predicted protein has invalid coordinates at line {line_number}"
            ) from exc
        if not all(math.isfinite(axis) for axis in xyz):
            raise InteractionFingerprintError(
                f"predicted protein has non-finite coordinates at line {line_number}"
            )
        element = line[76:78].strip() if len(line) >= 78 else ""
        if not element or not element.isalpha() or len(element) > 2:
            raise InteractionFingerprintError(
                f"predicted protein has no valid PDB element at line {line_number}"
            )
        element = element[0].upper() + element[1:].lower()
        atom_records.append((residue_key, element, xyz))
        if any(
            (xyz[0] - ligand[0]) ** 2
            + (xyz[1] - ligand[1]) ** 2
            + (xyz[2] - ligand[2]) ** 2
            <= cutoff_squared
            for ligand in ligand_coordinates
        ):
            nearby_residues.add(residue_key)

    if not atom_records:
        raise InteractionFingerprintError(
            "pose-specific predicted protein has no PDB atom records"
        )
    if not nearby_residues:
        return []
    return [record for record in atom_records if record[0] in nearby_residues]


def _pdb_residue_label(key: tuple[str, str, str, str]) -> str:
    chain, residue_number, insertion_code, residue_name = key
    name = residue_name.strip()
    number = residue_number.strip()
    insertion = insertion_code.strip()
    chain_id = chain.strip()
    if not name or not number:
        raise InteractionFingerprintError(
            "predicted protein has invalid PDB residue metadata"
        )
    return f"{name}{number}{insertion}{'.' + chain_id if chain_id else ''}"


def _vdw_summary(
    protein_records: Sequence[
        tuple[tuple[str, str, str, str], str, tuple[float, float, float]]
    ],
    ligand: Any,
    ligand_coordinates: Sequence[tuple[float, float, float]],
) -> dict[str, Any]:
    """Apply ProLIF's pinned VdWContact distance definition exactly."""

    detector = _vdw_detector()
    ligand_elements = [atom.GetSymbol() for atom in ligand.GetAtoms()]
    if len(ligand_elements) != len(ligand_coordinates):
        raise InteractionFingerprintError(
            "ligand element count does not match predicted coordinates"
        )
    contacting: set[str] = set()
    for residue_key, protein_element, protein_xyz in protein_records:
        residue_label = _pdb_residue_label(residue_key)
        for ligand_element, ligand_xyz in zip(ligand_elements, ligand_coordinates):
            try:
                threshold = (
                    detector._get_radii_sum(ligand_element, protein_element)
                    + detector.tolerance
                )
            except ValueError as exc:
                raise InteractionFingerprintError(
                    "ProLIF VdW radii are missing a pose element"
                ) from exc
            distance_squared = (
                (protein_xyz[0] - ligand_xyz[0]) ** 2
                + (protein_xyz[1] - ligand_xyz[1]) ** 2
                + (protein_xyz[2] - ligand_xyz[2]) ** 2
            )
            if distance_squared <= threshold * threshold:
                contacting.add(residue_label)
                break
    residues = [
        {"id": residue, "types": ["VdWContact"]}
        for residue in sorted(contacting)
    ]
    return {
        "count": len(residues),
        "interacting_residue_count": len(residues),
        "by_type": {"VdWContact": len(residues)} if residues else {},
        "residues": residues,
    }


def summarize_ifp(
    ifp: Mapping[tuple[Any, Any], Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize an IFP as unique ``(protein residue, interaction type)`` bits.

    Atom-level occurrences are intentionally not counted.  This prevents a
    single residue with several equivalent atoms from inflating the public
    value, and matches a boolean ProLIF fingerprint (``count=False``).
    """

    residue_types: dict[str, set[str]] = {}
    for pair, interactions in ifp.items():
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise InteractionFingerprintError("ProLIF returned an invalid residue pair")
        if not isinstance(interactions, Mapping):
            raise InteractionFingerprintError("ProLIF returned invalid interaction metadata")
        protein_residue = _residue_label(pair[1])
        types = residue_types.setdefault(protein_residue, set())
        for interaction_type, occurrences in interactions.items():
            if interaction_type not in INTERACTION_TYPES:
                raise InteractionFingerprintError(
                    f"ProLIF returned an unconfigured interaction: {interaction_type}"
                )
            # With metadata=True an empty tuple means the interaction is absent.
            # Accept any non-empty sized collection so this helper remains easy
            # to test independently of ProLIF's concrete metadata type.
            try:
                present = len(occurrences) > 0
            except TypeError as exc:
                raise InteractionFingerprintError(
                    "ProLIF returned invalid interaction occurrences"
                ) from exc
            if present:
                types.add(interaction_type)

    residue_types = {residue: types for residue, types in residue_types.items() if types}
    by_type = {
        interaction_type: sum(
            interaction_type in types for types in residue_types.values()
        )
        for interaction_type in INTERACTION_TYPES
    }
    by_type = {key: value for key, value in by_type.items() if value}
    residues = [
        {"id": residue, "types": sorted(types)}
        for residue, types in sorted(residue_types.items())
    ]
    return {
        "count": sum(by_type.values()),
        "interacting_residue_count": len(residues),
        "by_type": by_type,
        "residues": residues,
    }


def calculate_interaction_summary(
    protein_path: str | Path,
    ligand_smiles: str,
    ligand_coordinates: Iterable[Sequence[float]],
) -> dict[str, Any]:
    """Calculate the fixed ProLIF summary for one exact cofolded pose.

    ``protein_path`` must be the pose-specific predicted receptor in the same
    frame as ``ligand_coordinates``.  ``ligand_smiles`` supplies authoritative
    bond orders and formal charges; coordinates alone are not a safe chemistry
    topology.
    """

    path = Path(protein_path).resolve()
    if not path.is_file():
        raise InteractionFingerprintError("pose-specific predicted protein is missing")
    if not isinstance(ligand_smiles, str) or not ligand_smiles.strip():
        raise InteractionFingerprintError("ligand SMILES is required")
    coordinates = _coordinates(ligand_coordinates)
    versions = _installed_versions()
    _prolif, Chem, Point3D = _dependencies()

    ligand = Chem.MolFromSmiles(ligand_smiles.strip())
    if ligand is None:
        raise InteractionFingerprintError("RDKit could not parse ligand SMILES")
    ligand = Chem.RemoveHs(ligand)
    if ligand.GetNumAtoms() != len(coordinates):
        raise InteractionFingerprintError(
            "ligand SMILES heavy-atom count does not match predicted coordinates"
        )
    conformer = Chem.Conformer(ligand.GetNumAtoms())
    for atom_index, xyz in enumerate(coordinates):
        conformer.SetAtomPosition(atom_index, Point3D(*xyz))
    ligand.RemoveAllConformers()
    ligand.AddConformer(conformer, assignId=True)

    protein_records = _protein_vicinity_records(path, coordinates)
    summary = _vdw_summary(protein_records, ligand, coordinates)

    return {
        "schema_version": 1,
        "engine": "prolif",
        "engine_version": versions["prolif"],
        "rdkit_version": versions["rdkit"],
        "policy": INTERACTION_POLICY,
        "protein_parser_policy": PROTEIN_PARSER_POLICY,
        "vicinity_cutoff_angstrom": VICINITY_CUTOFF_ANGSTROM,
        "vdw_radii_preset": VDW_RADII_PRESET,
        "interaction_types": list(INTERACTION_TYPES),
        **summary,
    }


def calculate_interaction_summary_from_pose(
    protein_path: str | Path,
    ligand_path: str | Path,
    ligand_smiles: str,
) -> dict[str, Any]:
    """Fingerprint one staged ligand PDB against its exact predicted protein."""

    pose_path = Path(ligand_path).resolve()
    if not pose_path.is_file():
        raise InteractionFingerprintError("pose-specific predicted ligand is missing")
    _prolif, Chem, _Point3D = _dependencies()
    try:
        pose = Chem.MolFromPDBFile(
            str(pose_path),
            sanitize=False,
            removeHs=True,
            proximityBonding=False,
        )
    except Exception as exc:
        raise InteractionFingerprintError(
            "RDKit could not parse the pose-specific predicted ligand"
        ) from exc
    if pose is None or pose.GetNumConformers() != 1:
        raise InteractionFingerprintError(
            "RDKit could not parse the pose-specific predicted ligand"
        )
    conformer = pose.GetConformer()
    coordinates = []
    for index in range(pose.GetNumAtoms()):
        position = conformer.GetAtomPosition(index)
        coordinates.append((float(position.x), float(position.y), float(position.z)))
    return calculate_interaction_summary(protein_path, ligand_smiles, coordinates)


__all__ = [
    "INTERACTION_POLICY",
    "INTERACTION_TYPES",
    "PROLIF_VERSION",
    "PROTEIN_PARSER_POLICY",
    "RDKIT_VERSION",
    "VICINITY_CUTOFF_ANGSTROM",
    "VDW_RADII_PRESET",
    "InteractionFingerprintError",
    "_protein_vicinity_records",
    "_vdw_summary",
    "calculate_interaction_summary",
    "calculate_interaction_summary_from_pose",
    "summarize_ifp",
]
