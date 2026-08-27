"""Reproducible ProLIF hydrogen-bond summaries for exact predicted poses.

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
INTERACTION_POLICY = "prolif-implicit-hbond-unique-protein-residue/v1"
VICINITY_CUTOFF_ANGSTROM = 6.0
PROTEIN_STANDARDIZATION_POLICY = (
    "prolif-molecule-standardizer-standard-amino-acids/v1"
)
PROTEIN_PARSE_POLICY = "rdkit-pdb-unsanitized-proximity-connectivity/v1"

# ProLIF names these according to the ligand's role.  The public metric collapses
# the direction into one unique-protein-residue H-bond count; private provenance
# retains the directional breakdown.
INTERACTION_TYPES = ("ImplicitHBAcceptor", "ImplicitHBDonor")


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


def _hbond_summary(
    prolif: Any,
    Chem: Any,
    protein_path: Path,
    ligand: Any,
) -> dict[str, Any]:
    """Run ProLIF's implicit-H H-bond detectors with their default geometry checks."""

    try:
        # RDKit's default PDB sanitization can reject cofolded receptors when
        # proximity-guessed bonds span a steric clash. ProLIF immediately strips
        # and replaces each residue's bonds from standard templates, so defer
        # sanitization until that authoritative residue-standardization step.
        raw_protein = Chem.MolFromPDBFile(
            str(protein_path),
            sanitize=False,
            removeHs=False,
            proximityBonding=True,
        )
        if raw_protein is None:
            raise ValueError("RDKit could not parse predicted receptor coordinates")
        protein = prolif.io.MoleculeStandardizer()(raw_protein)
        ligand_molecule = prolif.Molecule.from_rdkit(
            ligand,
            resname="LIG",
            resnumber=1,
            chain="X",
        )
        fingerprint = prolif.Fingerprint(
            interactions=["HBAcceptor", "HBDonor"],
            count=False,
            vicinity_cutoff=VICINITY_CUTOFF_ANGSTROM,
            implicit_hydrogens=True,
        )
        ifp = fingerprint.generate(ligand_molecule, protein, metadata=True)
    except Exception as exc:
        detail = str(exc).replace("\n", " ").strip()[:500]
        raise InteractionFingerprintError(
            "ProLIF could not standardize or fingerprint the exact predicted "
            f"complex: {type(exc).__name__}: {detail or 'no detail'}"
        ) from exc
    return summarize_ifp(ifp)


def summarize_ifp(
    ifp: Mapping[tuple[Any, Any], Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize an IFP as unique protein residues with ligand H-bonds.

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
        "count": len(residues),
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
    prolif, Chem, Point3D = _dependencies()

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

    summary = _hbond_summary(prolif, Chem, path, ligand)

    return {
        "schema_version": 1,
        "engine": "prolif",
        "engine_version": versions["prolif"],
        "rdkit_version": versions["rdkit"],
        "policy": INTERACTION_POLICY,
        "protein_parse_policy": PROTEIN_PARSE_POLICY,
        "protein_standardization_policy": PROTEIN_STANDARDIZATION_POLICY,
        "vicinity_cutoff_angstrom": VICINITY_CUTOFF_ANGSTROM,
        "implicit_hydrogens": True,
        "geometry_checks": "prolif-defaults",
        "include_water": False,
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
    "PROTEIN_STANDARDIZATION_POLICY",
    "PROTEIN_PARSE_POLICY",
    "RDKIT_VERSION",
    "VICINITY_CUTOFF_ANGSTROM",
    "InteractionFingerprintError",
    "_hbond_summary",
    "calculate_interaction_summary",
    "calculate_interaction_summary_from_pose",
    "summarize_ifp",
]
