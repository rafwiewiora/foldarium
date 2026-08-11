"""Versioned ligand-selection policy shared by weekly and archive intake.

The exclusions preserve Foldarium's existing quiz behavior.  They remove ions,
solvents, crystallization components, common sugars/cofactors, and lipids before
the quiz's stricter heavy-atom threshold is applied.  This is intentionally a
checked-in policy rather than an unversioned query so every campaign records the
exact selection semantics used at intake.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .sizing import count_smiles_heavy_atoms

SELECTION_POLICY_VERSION = "cameo-drug-like/v4"
HEAVY_ATOM_MINIMUM = 15

# Pose quizzes need one drug-like organic molecule, not a disconnected salt,
# organometallic cofactor, or crystallographic metal complex whose fragments
# happen to exceed the heavy-atom minimum when counted together.
METAL_ELEMENTS = frozenset(
    {
        "Li", "Na", "K", "Rb", "Cs", "Fr", "Be", "Mg", "Ca", "Sr", "Ba", "Ra",
        "Al", "Ga", "In", "Tl", "Sn", "Pb", "Bi",
        "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
        "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
        "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
        "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho",
        "Er", "Tm", "Yb", "Lu", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am",
        "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr",
    }
)
_BRACKET_ELEMENT = re.compile(r"^\d*([A-Z][a-z]?|[bcnops])")

# Kept in sync with the historical CAMEO quiz builder.  A later migration to a
# versioned upstream artifact list should be a new selection-policy version.
ARTIFACT_COMPONENTS = frozenset(
    (
        "HOH DOD NA CL MG ZN CA K MN FE FE2 FE3 CU CU1 NI CO CD HG CS BA SR BR "
        "IOD I RB LI PB PT AU AG TL SM GD YB EU MO W V SE F ZN2 3CO 4MO OH O OXY "
        "SO4 PO4 PI NO3 ACT EDO GOL PEG PG4 PGE 1PE 2PE P6G MPD DMS BME MES EPE "
        "TRS TAR CIT FLC FMT IPA BO3 NH4 AZI CAC MLA OXL SCN 144 15P PE4 PEU DIO "
        "SIN MLI BCT CO3 UNX UNL UNK BU3 MRD IMD POL PGO PG0 12P 7PE DTT DTV TLA "
        "SUC NAG MAN BMA FUC GAL GLC NDG BGC FUL XYS RAM SIA NGA A2G GLA XYP GCU "
        "ADA RIB API MAL TRE LMT LMN DGD SGN BOG NAD NAP NDP NAI NAJ FAD FMN FDA "
        "ATP ADP AMP ANP ACP AGS APC GTP GDP GNP GSP GMP CTP UTP UDP UMP TTP TMP "
        "COA ACO SAM SAH SFG HEM HEC HEA HEB DHE HAS PLP PMP TPP TDP BTI BTN B12 "
        "COB H4B BH4 MGD PAP UD1 UPG 5GP PNS PLM CLR POV PTY CDL OLA OLB OLC STE "
        "MYR PEE PCW PC1 PEF LHG PGV PGW D10 DD9 HP6 Y01 HC3 PX4 3PE PEK PSC 17F "
        "PC7 PEV UND DAO LMG MC3 9PE PLC SPH CHS CHD EIC ARA HTG PX2 P4K TGL"
    ).split()
)

# Caffeine is a legitimate ligand, but in the A2A fragment-soak series it is a
# reference compound. Prefer another eligible ligand when one exists.
PREFER_ALTERNATIVE_TO = frozenset({"TEP"})


class SelectionError(ValueError):
    """Raised when an upstream ligand record is malformed."""


def _smiles_contains_metal(smiles: str) -> bool:
    """Return whether a SMILES contains a bracketed metal atom."""

    index = 0
    while index < len(smiles):
        if smiles[index] != "[":
            index += 1
            continue
        close = smiles.find("]", index + 1)
        if close == -1:
            return False
        match = _BRACKET_ELEMENT.match(smiles[index + 1 : close])
        if match and match.group(1).capitalize() in METAL_ELEMENTS:
            return True
        index = close + 1
    return False


def ligand_heavy_atoms(ligand: Mapping[str, Any]) -> int:
    """Return a dependency-free heavy-atom estimate from a public SMILES string."""

    smiles = ligand.get("smiles")
    if not isinstance(smiles, str) or not smiles.strip():
        raise SelectionError("ligand SMILES must be a non-empty string")
    return count_smiles_heavy_atoms(smiles)


def ligand_rejection_reason(
    ligand: Mapping[str, Any],
    *,
    heavy_atom_minimum: int = HEAVY_ATOM_MINIMUM,
) -> str | None:
    """Return the current versioned rejection code for one ligand, if any."""

    if isinstance(heavy_atom_minimum, bool) or not isinstance(heavy_atom_minimum, int):
        raise SelectionError("heavy_atom_minimum must be a positive integer")
    if heavy_atom_minimum < 1:
        raise SelectionError("heavy_atom_minimum must be a positive integer")
    component = ligand.get("component_id")
    smiles = ligand.get("smiles")
    if not isinstance(component, str) or not component.strip():
        return "invalid-component-id"
    if component.strip().upper() in ARTIFACT_COMPONENTS:
        return "artifact-component"
    if not isinstance(smiles, str) or not smiles.strip():
        return "missing-smiles"
    smiles = smiles.strip()
    if "." in smiles:
        return "disconnected-smiles"
    if _smiles_contains_metal(smiles):
        return "metal-containing-smiles"
    if ligand_heavy_atoms({"smiles": smiles}) < heavy_atom_minimum:
        return "below-heavy-atom-minimum"
    return None


def select_ligand(
    ligands: Iterable[Mapping[str, Any]],
    *,
    heavy_atom_minimum: int = HEAVY_ATOM_MINIMUM,
) -> dict[str, Any] | None:
    """Select the largest eligible ligand, preserving the historical TEP rule."""

    candidates: list[dict[str, Any]] = []
    for raw in ligands:
        ligand = dict(raw)
        component = ligand.get("component_id")
        smiles = ligand.get("smiles")
        if ligand_rejection_reason(
            ligand, heavy_atom_minimum=heavy_atom_minimum
        ) is not None:
            continue
        component = component.strip().upper()
        smiles = smiles.strip()
        heavy_atoms = ligand_heavy_atoms(ligand)
        ligand.update(component_id=component, smiles=smiles, heavy_atoms=heavy_atoms)
        candidates.append(ligand)

    if not candidates:
        return None
    alternatives = [
        ligand for ligand in candidates if ligand["component_id"] not in PREFER_ALTERNATIVE_TO
    ]
    pool = alternatives or candidates
    return max(pool, key=lambda ligand: (ligand["heavy_atoms"], ligand["component_id"]))


__all__ = [
    "ARTIFACT_COMPONENTS",
    "HEAVY_ATOM_MINIMUM",
    "METAL_ELEMENTS",
    "PREFER_ALTERNATIVE_TO",
    "SELECTION_POLICY_VERSION",
    "SelectionError",
    "ligand_heavy_atoms",
    "ligand_rejection_reason",
    "select_ligand",
]
