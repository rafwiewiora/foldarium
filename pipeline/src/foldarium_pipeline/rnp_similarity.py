"""RnP-style ligand/pocket similarity scoring over the top 25 Foldseek hits."""

from __future__ import annotations

import math
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .training_similarity import (
    MAX_STRUCTURE_BYTES,
    TRAINING_HIT_LIMIT,
    USER_AGENT,
    TrainingAnalog,
    file_sha256,
)

RNP_STYLE_METHOD = "rnp-style/top-25"
RNP_STYLE_VERSION = "rnp-style-sucos-pocket-qcov/v1"
# Runs N' Poses publishes novelty as a score below 25/100.  This is a
# separately defined calibration; its equality to the canonical overlap
# threshold is coincidental.
RNP_NOVELTY_THRESHOLD = 25.0 / 100.0
RNP_POCKET_RADIUS_ANGSTROM = 6.0
RNP_ALIGNMENT_PREITERATIONS = 100
RNP_ALIGNMENT_POSTITERATIONS = 100
RNP_FEATURE_FAMILIES = frozenset(
    {
        "Donor",
        "Acceptor",
        "NegIonizable",
        "PosIonizable",
        "ZnBinder",
        "Aromatic",
        "Hydrophobe",
        "LumpedHydrophobe",
    }
)

_CCD_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_BOND_ORDERS = {
    "SING": "SINGLE",
    "SINGLE": "SINGLE",
    "DOUB": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "TRIP": "TRIPLE",
    "TRIPLE": "TRIPLE",
    "AROM": "AROMATIC",
    "AROMATIC": "AROMATIC",
    "DELO": "ONEANDAHALF",
}


class RnPSimilarityError(RuntimeError):
    """Raised when an RnP-style score cannot be computed faithfully."""


def _science() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    try:
        import gemmi
        import numpy
        from rdkit import Chem, RDConfig
        from rdkit.Chem import (
            ChemicalFeatures,
            rdDetermineBonds,
            rdMolAlign,
            rdShapeAlign,
            rdShapeHelpers,
        )
        from rdkit.Chem.FeatMaps import FeatMaps
    except ImportError as exc:
        raise RnPSimilarityError(
            "RnP-style scoring requires the pipeline evaluation extras"
        ) from exc
    return (
        gemmi,
        numpy,
        Chem,
        ChemicalFeatures,
        RDConfig,
        (FeatMaps, rdMolAlign, rdDetermineBonds),
        (rdShapeAlign, rdShapeHelpers),
    )


def _component_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if (
        not 1 <= len(normalized) <= 5
        or any(character not in _CCD_ID_CHARS for character in normalized)
    ):
        raise RnPSimilarityError(f"invalid CCD component ID: {value!r}")
    return normalized


def download_rcsb_ccd(
    component_id: str,
    cache_directory: str | Path,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Path:
    """Return one immutable, locally cached RCSB CCD component definition."""

    component = _component_id(component_id)
    destination = Path(cache_directory) / "rcsb-ccd" / f"{component}.cif"
    if destination.is_file() and destination.stat().st_size:
        return destination
    request = Request(
        f"https://files.rcsb.org/ligands/download/{component}.cif",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with opener(request, timeout=60) as response:
            content = response.read(MAX_STRUCTURE_BYTES + 1)
    except Exception as exc:
        raise RnPSimilarityError(
            f"could not download CCD chemistry for {component}"
        ) from exc
    if not content or len(content) > MAX_STRUCTURE_BYTES:
        raise RnPSimilarityError(
            f"CCD chemistry for {component} is empty or too large"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=destination.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _read_structure(path: str | Path) -> Any:
    gemmi, _numpy, *_rest = _science()
    try:
        structure = gemmi.read_structure(str(path))
        structure.setup_entities()
        if len(structure) < 1:
            raise ValueError("no model")
        return structure
    except Exception as exc:
        raise RnPSimilarityError(f"could not parse coordinates {path}") from exc


def _first_protein_polymer(model: Any) -> Any:
    for chain in model:
        polymer = chain.get_polymer()
        if len(polymer) > 0:
            return polymer
    raise RnPSimilarityError("query structure has no protein polymer")


def _coordinate_ligand_residue(
    path: str | Path, declared_component_id: str
) -> Any:
    component = _component_id(declared_component_id)
    structure = _read_structure(path)
    exact = [
        residue
        for chain in structure[0]
        for residue in chain
        if residue.name.upper() == component and not residue.is_water()
    ]
    candidates = exact
    if not candidates and component != "LIG":
        # Predicted pose files often use generic LIG while their task metadata
        # carries the authoritative CCD component identifier.
        candidates = [
            residue
            for chain in structure[0]
            for residue in chain
            if residue.name.upper() == "LIG" and not residue.is_water()
        ]
    if len(candidates) != 1:
        raise RnPSimilarityError(
            f"coordinates do not contain exactly one {component} ligand"
        )
    return candidates[0]


def _heavy_coordinate_atoms(residue: Any) -> list[Any]:
    atoms = [atom for atom in residue if atom.element.name.upper() != "H"]
    names = [atom.name.strip().upper() for atom in atoms]
    if not atoms or any(not name for name in names) or len(names) != len(set(names)):
        raise RnPSimilarityError("ligand coordinates have invalid atom names")
    return atoms


def _cif_table(block: Any, prefix: str, columns: Sequence[str]) -> Any:
    table = block.find(prefix, list(columns))
    if table is None or len(table) == 0:
        raise RnPSimilarityError(f"CCD chemistry has no {prefix} records")
    return table


def _authoritative_molecule(
    coordinate_atoms: Sequence[Any],
    ccd_atoms: Mapping[str, tuple[str, int]],
    ccd_bonds: Sequence[tuple[str, str, str]],
    template_to_coordinate: Mapping[str, int],
    Chem: Any,
) -> Any:
    if set(template_to_coordinate) != set(ccd_atoms) or len(
        set(template_to_coordinate.values())
    ) != len(coordinate_atoms):
        raise RnPSimilarityError("CCD atom mapping is not bijective")
    coordinate_to_template = {
        coordinate_index: template_name
        for template_name, coordinate_index in template_to_coordinate.items()
    }
    editable = Chem.RWMol()
    conformer = Chem.Conformer(len(coordinate_atoms))
    for coordinate_index, coordinate_atom in enumerate(coordinate_atoms):
        template_name = coordinate_to_template.get(coordinate_index)
        if template_name is None:
            raise RnPSimilarityError("CCD atom mapping is incomplete")
        template_element, charge = ccd_atoms[template_name]
        observed_element = coordinate_atom.element.name.capitalize()
        if observed_element != template_element:
            raise RnPSimilarityError(
                "coordinate element disagrees with the mapped CCD atom"
            )
        atom = Chem.Atom(observed_element)
        atom.SetFormalCharge(charge)
        editable.AddAtom(atom)
        position = (
            float(coordinate_atom.pos.x),
            float(coordinate_atom.pos.y),
            float(coordinate_atom.pos.z),
        )
        if not all(math.isfinite(value) for value in position):
            raise RnPSimilarityError("ligand coordinates are not finite")
        conformer.SetAtomPosition(coordinate_index, position)

    aromatic_atoms: set[int] = set()
    for left_name, right_name, order_name in ccd_bonds:
        left = template_to_coordinate[left_name]
        right = template_to_coordinate[right_name]
        bond_type = getattr(Chem.BondType, order_name)
        editable.AddBond(left, right, bond_type)
        if bond_type == Chem.BondType.AROMATIC:
            aromatic_atoms.update((left, right))
    molecule = editable.GetMol()
    molecule.AddConformer(conformer)
    for atom_index in aromatic_atoms:
        molecule.GetAtomWithIdx(atom_index).SetIsAromatic(True)
    try:
        Chem.SanitizeMol(molecule)
    except Exception as exc:
        raise RnPSimilarityError(
            "CCD chemistry does not form a valid molecule"
        ) from exc
    if len(Chem.GetMolFrags(molecule)) != 1:
        raise RnPSimilarityError("CCD chemistry is not one connected molecule")
    return molecule


def _chemistry_assignment_signature(molecule: Any) -> tuple[Any, ...]:
    atom_signature = tuple(
        (atom.GetFormalCharge(), atom.GetIsAromatic())
        for atom in molecule.GetAtoms()
    )
    bond_signature = tuple(
        sorted(
            (
                min(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                max(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                str(bond.GetBondType()),
                bond.GetIsAromatic(),
            )
            for bond in molecule.GetBonds()
        )
    )
    return atom_signature, bond_signature


def _renamed_atom_molecule(
    coordinate_atoms: Sequence[Any],
    ccd_atoms: Mapping[str, tuple[str, int]],
    ccd_bonds: Sequence[tuple[str, str, str]],
    Chem: Any,
    rdDetermineBonds: Any,
) -> Any:
    observed_elements = [
        atom.element.name.capitalize() for atom in coordinate_atoms
    ]
    template_elements = [element for element, _charge in ccd_atoms.values()]
    if Counter(observed_elements) != Counter(template_elements):
        raise RnPSimilarityError(
            "renamed coordinates do not match the CCD element multiset"
        )

    observed_editable = Chem.RWMol()
    observed_conformer = Chem.Conformer(len(coordinate_atoms))
    for index, (coordinate_atom, element) in enumerate(
        zip(coordinate_atoms, observed_elements)
    ):
        observed_editable.AddAtom(Chem.Atom(element))
        position = (
            float(coordinate_atom.pos.x),
            float(coordinate_atom.pos.y),
            float(coordinate_atom.pos.z),
        )
        if not all(math.isfinite(value) for value in position):
            raise RnPSimilarityError("ligand coordinates are not finite")
        observed_conformer.SetAtomPosition(index, position)
    observed = observed_editable.GetMol()
    observed.AddConformer(observed_conformer)
    try:
        # Connectivity is the only chemical property inferred from coordinates.
        # Bond orders and formal charges below always come from a matching CCD graph.
        rdDetermineBonds.DetermineConnectivity(observed)
    except Exception as exc:
        raise RnPSimilarityError(
            "could not infer renamed ligand connectivity"
        ) from exc
    if len(Chem.GetMolFrags(observed)) != 1:
        raise RnPSimilarityError(
            "renamed ligand connectivity is not one connected molecule"
        )
    if observed.GetNumBonds() != len(ccd_bonds):
        raise RnPSimilarityError(
            "renamed ligand connectivity does not match the CCD template"
        )

    template_names = list(ccd_atoms)
    template_index = {
        template_name: index
        for index, template_name in enumerate(template_names)
    }
    template_editable = Chem.RWMol()
    for template_name in template_names:
        atom = Chem.Atom(ccd_atoms[template_name][0])
        atom.SetNoImplicit(True)
        template_editable.AddAtom(atom)
    for left_name, right_name, _order_name in ccd_bonds:
        template_editable.AddBond(
            template_index[left_name],
            template_index[right_name],
            Chem.BondType.SINGLE,
        )
    template = template_editable.GetMol()
    template.UpdatePropertyCache(strict=False)
    for atom in observed.GetAtoms():
        atom.SetNoImplicit(True)
    observed.UpdatePropertyCache(strict=False)

    maximum_matches = 4096
    matches = observed.GetSubstructMatches(
        template,
        uniquify=False,
        useChirality=False,
        maxMatches=maximum_matches + 1,
    )
    if not matches:
        raise RnPSimilarityError(
            "renamed ligand connectivity has no CCD-template match"
        )
    if len(matches) > maximum_matches:
        raise RnPSimilarityError(
            "renamed ligand connectivity has too many CCD-template matches"
        )

    assignments: dict[tuple[Any, ...], Any] = {}
    for match in matches:
        mapping = {
            template_name: match[index]
            for index, template_name in enumerate(template_names)
        }
        try:
            candidate = _authoritative_molecule(
                coordinate_atoms,
                ccd_atoms,
                ccd_bonds,
                mapping,
                Chem,
            )
        except RnPSimilarityError:
            continue
        assignments.setdefault(
            _chemistry_assignment_signature(candidate), candidate
        )
        if len(assignments) > 1:
            break
    if len(assignments) != 1:
        raise RnPSimilarityError(
            "renamed ligand connectivity has no unique valid CCD chemistry assignment"
        )
    return next(iter(assignments.values()))


def molecule_from_ccd_coordinates(
    residue: Any, component_id: str, ccd_path: str | Path
) -> Any:
    """Build a sanitized RDKit molecule using CCD topology and observed coordinates."""

    component = _component_id(component_id)
    gemmi, _numpy, Chem, _features_api, _config, alignment, _shape = _science()
    _FeatMaps, _rdMolAlign, rdDetermineBonds = alignment
    try:
        block = gemmi.cif.read_file(str(ccd_path)).sole_block()
    except Exception as exc:
        raise RnPSimilarityError(
            f"could not parse CCD chemistry for {component}"
        ) from exc

    atom_rows = _cif_table(
        block,
        "_chem_comp_atom.",
        ["comp_id", "atom_id", "type_symbol", "charge"],
    )
    ccd_atoms: dict[str, tuple[str, int]] = {}
    for row in atom_rows:
        if str(row[0]).upper() != component:
            continue
        name = str(row[1]).strip().upper()
        element = str(row[2]).strip().capitalize()
        if element.upper() == "H":
            continue
        try:
            charge = int(str(row[3]))
        except ValueError as exc:
            raise RnPSimilarityError(
                f"CCD chemistry for {component} has an invalid formal charge"
            ) from exc
        if not name or name in ccd_atoms:
            raise RnPSimilarityError(
                f"CCD chemistry for {component} has invalid atom identifiers"
            )
        ccd_atoms[name] = (element, charge)
    if not ccd_atoms:
        raise RnPSimilarityError(
            f"CCD chemistry for {component} has no heavy atoms"
        )

    bond_rows = _cif_table(
        block,
        "_chem_comp_bond.",
        ["comp_id", "atom_id_1", "atom_id_2", "value_order"],
    )
    ccd_bonds: list[tuple[str, str, str]] = []
    seen_bonds: set[tuple[str, str]] = set()
    for row in bond_rows:
        if str(row[0]).upper() != component:
            continue
        left_name = str(row[1]).strip().upper()
        right_name = str(row[2]).strip().upper()
        if left_name not in ccd_atoms or right_name not in ccd_atoms:
            continue
        edge = (min(left_name, right_name), max(left_name, right_name))
        if left_name == right_name or edge in seen_bonds:
            raise RnPSimilarityError(
                f"CCD chemistry for {component} has invalid bonds"
            )
        seen_bonds.add(edge)
        order_name = _BOND_ORDERS.get(str(row[3]).strip().upper())
        if order_name is None:
            raise RnPSimilarityError(
                f"CCD chemistry for {component} has an unsupported bond order"
            )
        ccd_bonds.append((left_name, right_name, order_name))
    if len(ccd_atoms) > 1 and not ccd_bonds:
        raise RnPSimilarityError(
            f"CCD chemistry for {component} has no heavy-atom bonds"
        )

    coordinate_atoms = _heavy_coordinate_atoms(residue)
    coordinate_names = [atom.name.strip().upper() for atom in coordinate_atoms]
    if set(coordinate_names) == set(ccd_atoms):
        exact_mapping = {
            coordinate_name: coordinate_index
            for coordinate_index, coordinate_name in enumerate(coordinate_names)
        }
        return _authoritative_molecule(
            coordinate_atoms,
            ccd_atoms,
            ccd_bonds,
            exact_mapping,
            Chem,
        )
    return _renamed_atom_molecule(
        coordinate_atoms,
        ccd_atoms,
        ccd_bonds,
        Chem,
        rdDetermineBonds,
    )


@lru_cache(maxsize=1)
def _feature_factory() -> Any:
    _gemmi, _numpy, _Chem, ChemicalFeatures, RDConfig, *_rest = _science()
    return ChemicalFeatures.BuildFeatureFactory(
        str(Path(RDConfig.RDDataDir) / "BaseFeatures.fdef")
    )


def _features(molecule: Any) -> list[Any]:
    return [
        feature
        for feature in _feature_factory().GetFeaturesForMol(molecule)
        if feature.GetFamily() in RNP_FEATURE_FAMILIES
    ]


def ligand_aligned_sucos(
    query_molecule: Any, training_molecule: Any
) -> dict[str, float]:
    """Align ligands independently of proteins, then calculate SuCOS components."""

    _gemmi, numpy, Chem, _features_api, _config, alignment, shape = _science()
    FeatMaps, rdMolAlign, _rdDetermineBonds = alignment
    rdShapeAlign, rdShapeHelpers = shape
    reference = Chem.Mol(query_molecule)
    probe = Chem.Mol(training_molecule)
    try:
        # Runs N' Poses alignment order and the SuCOS source it attributes:
        # https://github.com/plinder-org/runs-n-poses/blob/main/similarity_scoring.py
        # https://github.com/susanhleung/SuCOS
        # This is a clean implementation of the published sequence, not copied code.
        rdMolAlign.GetCrippenO3A(
            probe,
            reference,
            maxIters=RNP_ALIGNMENT_PREITERATIONS,
        ).Align()
        rdShapeAlign.AlignMol(
            reference,
            probe,
            useColors=True,
            max_preiters=RNP_ALIGNMENT_PREITERATIONS,
            max_postiters=RNP_ALIGNMENT_POSTITERATIONS,
        )
    except Exception as exc:
        raise RnPSimilarityError("ligand shape alignment failed") from exc

    reference_features = _features(reference)
    probe_features = _features(probe)
    if reference_features and probe_features:
        parameters = {
            family: FeatMaps.FeatMapParams() for family in RNP_FEATURE_FAMILIES
        }
        feature_map = FeatMaps.FeatMap(
            feats=reference_features,
            weights=[1.0] * len(reference_features),
            params=parameters,
        )
        feature_map.scoreMode = FeatMaps.FeatMapScoreMode.All
        feature_score = float(feature_map.ScoreFeats(probe_features)) / min(
            len(reference_features), len(probe_features)
        )
    else:
        feature_score = 0.0
    feature_score = float(numpy.clip(feature_score, 0.0, 1.0))
    protrude_distance = float(
        rdShapeHelpers.ShapeProtrudeDist(
            reference, probe, allowReordering=False
        )
    )
    protrude_distance = float(numpy.clip(protrude_distance, 0.0, 1.0))
    shape_similarity = 1.0 - protrude_distance
    sucos = 0.5 * feature_score + 0.5 * shape_similarity
    return {
        "pharmacophore_feature_map_score": feature_score,
        "shape_protrude_similarity": shape_similarity,
        "sucos": float(numpy.clip(sucos, 0.0, 1.0)),
    }


def _molecule_positions(molecule: Any) -> Any:
    _gemmi, numpy, *_rest = _science()
    conformer = molecule.GetConformer()
    return numpy.asarray(
        [
            [
                conformer.GetAtomPosition(index).x,
                conformer.GetAtomPosition(index).y,
                conformer.GetAtomPosition(index).z,
            ]
            for index in range(molecule.GetNumAtoms())
        ],
        dtype=float,
    )


def _residue_within(residue: Any, ligand_positions: Any, radius: float) -> bool:
    _gemmi, numpy, *_rest = _science()
    for atom in residue:
        if atom.element.name.upper() == "H":
            continue
        position = numpy.asarray([atom.pos.x, atom.pos.y, atom.pos.z], dtype=float)
        if numpy.any(numpy.linalg.norm(ligand_positions - position, axis=1) <= radius):
            return True
    return False


def pocket_query_coverage(
    query_polymer: Any,
    query_ligand_molecule: Any,
    target_polymer: Any,
    training_ligand_molecule: Any,
    query_residue_indices: Sequence[int],
    target_residue_indices: Sequence[int],
    *,
    radius: float = RNP_POCKET_RADIUS_ANGSTROM,
) -> float:
    """Return the aligned-target coverage of the query ligand's 6 Å pocket."""

    if radius <= 0.0:
        raise RnPSimilarityError("pocket radius must be positive")
    query_ligand_positions = _molecule_positions(query_ligand_molecule)
    training_ligand_positions = _molecule_positions(training_ligand_molecule)
    query_pocket = {
        index
        for index, residue in enumerate(query_polymer)
        if _residue_within(residue, query_ligand_positions, radius)
    }
    if not query_pocket:
        raise RnPSimilarityError("query ligand has no protein residues within 6 Å")
    correspondence: dict[int, int] = {}
    for query_index, target_index in zip(
        query_residue_indices, target_residue_indices
    ):
        if (
            query_index in correspondence
            and correspondence[query_index] != target_index
        ):
            raise RnPSimilarityError("Foldseek residue correspondence is ambiguous")
        correspondence[query_index] = target_index
    covered = 0
    for query_index in query_pocket:
        target_index = correspondence.get(query_index)
        if target_index is None or not 0 <= target_index < len(target_polymer):
            continue
        if _residue_within(
            target_polymer[target_index], training_ligand_positions, radius
        ):
            covered += 1
    return covered / len(query_pocket)


def _candidate_failure(analog: TrainingAnalog, reason: str) -> dict[str, Any]:
    return {
        "pdb_id": analog.pdb_id,
        "ligand": analog.ligand,
        "rank": analog.hit_rank,
        "reason": reason,
    }


def _classification(
    score: float | None,
    *,
    failures: Sequence[Mapping[str, Any]],
    query_failure: str | None = None,
) -> tuple[str, bool | None, str]:
    if query_failure is not None:
        return "unknown", None, "rnp-query-evaluation-failed"
    if score is not None:
        if score >= RNP_NOVELTY_THRESHOLD:
            return "familiar", False, "rnp-score-at-least-0.25"
        return "novel", True, "rnp-score-below-0.25"
    if failures:
        return "unknown", None, "incomplete-rnp-candidate-evaluation"
    if score is None:
        return "novel", True, "no-usable-rnp-training-analog"
    raise AssertionError("unreachable RnP classification state")


def _empty_result(
    *,
    failures: Sequence[Mapping[str, Any]],
    query_failure: str | None = None,
) -> dict[str, Any]:
    classification, novel, reason = _classification(
        None, failures=failures, query_failure=query_failure
    )
    return {
        "method": RNP_STYLE_METHOD,
        "version": RNP_STYLE_VERSION,
        "threshold": RNP_NOVELTY_THRESHOLD,
        "classification": classification,
        "novel": novel,
        "reason": reason,
        "status": "unknown" if classification == "unknown" else "ok",
        "maximum_hit_rank": TRAINING_HIT_LIMIT,
        "train_pdb": None,
        "train_het": None,
        "train_hit_rank": None,
        "pharmacophore_feature_map_score": None,
        "shape_protrude_similarity": None,
        "sucos": None,
        "pocket_qcov": None,
        "sucos_shape_pocket_qcov": None,
        "evaluated_candidate_count": 0,
        "candidate_failures": [dict(failure) for failure in failures],
        "query_failure": query_failure,
    }


def rnp_style_top25_similarity(
    query_structure: str | Path,
    query_ligand_structure: str | Path,
    query_component_id: str,
    analogs: Sequence[TrainingAnalog],
    *,
    ccd_cache_directory: str | Path,
    ccd_loader: Callable[[str], str | Path] | None = None,
) -> dict[str, Any]:
    """Score candidate ligands from the top 25 Foldseek hits and return the winner."""

    loader = ccd_loader or (
        lambda component: download_rcsb_ccd(component, ccd_cache_directory)
    )
    try:
        query_coordinates = _coordinate_ligand_residue(
            query_ligand_structure, query_component_id
        )
        query_molecule = molecule_from_ccd_coordinates(
            query_coordinates,
            query_component_id,
            loader(_component_id(query_component_id)),
        )
        query_model = _read_structure(query_structure)[0]
        query_polymer = _first_protein_polymer(query_model)
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, RnPSimilarityError)
            else type(exc).__name__
        )
        return _empty_result(failures=[], query_failure=reason)

    scored: list[tuple[float, int, str, str, dict[str, Any]]] = []
    failures: list[dict[str, Any]] = []
    for analog in analogs:
        if analog.hit_rank > TRAINING_HIT_LIMIT:
            continue
        if (
            analog._source_structure is None
            or analog._source_structure_sha256 is None
            or analog._ligand_chain_index is None
            or analog._ligand_residue_index is None
            or analog._query_residue_indices is None
            or analog._target_chain_index is None
            or analog._target_residue_indices is None
        ):
            failures.append(_candidate_failure(analog, "missing-pocket-provenance"))
            continue
        try:
            if (
                file_sha256(analog._source_structure)
                != analog._source_structure_sha256
            ):
                raise RnPSimilarityError("training source structure changed")
            source = _read_structure(analog._source_structure)
            model = source[0]
            ligand = model[analog._ligand_chain_index][
                analog._ligand_residue_index
            ]
            if ligand.name.upper() != analog.ligand.upper():
                raise RnPSimilarityError(
                    "training ligand provenance does not match"
                )
            target_polymer = model[analog._target_chain_index].get_polymer()
            if len(target_polymer) == 0:
                raise RnPSimilarityError(
                    "Foldseek target chain has no protein polymer"
                )
            training_molecule = molecule_from_ccd_coordinates(
                ligand,
                analog.ligand,
                loader(_component_id(analog.ligand)),
            )
            components = ligand_aligned_sucos(
                query_molecule, training_molecule
            )
            pocket_qcov = pocket_query_coverage(
                query_polymer,
                query_molecule,
                target_polymer,
                training_molecule,
                analog._query_residue_indices,
                analog._target_residue_indices,
            )
            combined = components["sucos"] * pocket_qcov
            candidate = {
                "method": RNP_STYLE_METHOD,
                "version": RNP_STYLE_VERSION,
                "threshold": RNP_NOVELTY_THRESHOLD,
                "status": "ok",
                "maximum_hit_rank": TRAINING_HIT_LIMIT,
                "train_pdb": analog.pdb_id,
                "train_het": analog.ligand,
                "train_hit_rank": analog.hit_rank,
                **components,
                "pocket_qcov": pocket_qcov,
                "sucos_shape_pocket_qcov": combined,
            }
            scored.append(
                (
                    combined,
                    analog.hit_rank,
                    analog.pdb_id,
                    analog.ligand,
                    candidate,
                )
            )
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, RnPSimilarityError)
                else type(exc).__name__
            )
            failures.append(_candidate_failure(analog, reason))

    if not scored:
        return _empty_result(failures=failures)
    _combined, _rank, _pdb_id, _ligand, winner = min(
        scored,
        key=lambda row: (-row[0], row[1], row[2], row[3]),
    )
    classification, novel, reason = _classification(
        _combined, failures=failures
    )
    winner["classification"] = classification
    winner["novel"] = novel
    winner["reason"] = reason
    # Runs N' Poses isolates per-ligand exceptions: logged skipped candidates
    # do not invalidate a score from a successfully evaluated candidate.
    winner["status"] = "ok"
    winner["evaluated_candidate_count"] = len(scored)
    winner["candidate_failures"] = failures
    winner["query_failure"] = None
    return winner


__all__ = [
    "RNP_ALIGNMENT_POSTITERATIONS",
    "RNP_ALIGNMENT_PREITERATIONS",
    "RNP_FEATURE_FAMILIES",
    "RNP_NOVELTY_THRESHOLD",
    "RNP_POCKET_RADIUS_ANGSTROM",
    "RNP_STYLE_METHOD",
    "RNP_STYLE_VERSION",
    "RnPSimilarityError",
    "download_rcsb_ccd",
    "ligand_aligned_sucos",
    "molecule_from_ccd_coordinates",
    "pocket_query_coverage",
    "rnp_style_top25_similarity",
]
