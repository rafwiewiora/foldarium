"""Backend-neutral accelerator sizing derived from the scientific target.

Sizing lives in the core rather than in an execution adapter so every backend
makes the same decision from the same inputs, and so the chosen class can travel
in the task's ``resources``.  The task identity hash deliberately excludes
``resources``, so recording the hardware never changes a run's scientific
identity: the same target and configuration keep the same run ID whether they are
scheduled onto an L4 or an A100.

**The thresholds below are provisional.**  They are ordered correctly by device
memory and are deliberately conservative, but they were not measured against
OpenFold3 or Boltz-2.  Calibrate them from ``peak_gpu_memory_mib`` recorded by the
worker before treating them as a cost or capacity policy.
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple

from .contracts import ContractError, validate_target

POLYMER_TYPES = frozenset({"protein", "dna", "rna"})

# Rough per-residue equivalent for a CCD ligand whose atom count is not known
# without a chemistry toolkit. Ligands are small next to polymers, so a crude
# over-estimate here is cheap insurance.
CCD_ATOM_ESTIMATE = 30

# Memory for pairformer/Evoformer-style pair representations grows roughly with
# the square of the token count, so token count is the sizing variable. These two
# multipliers are the least certain part of this module.
MSA_MEMORY_FACTOR = 1.5
NO_MSA_MODES = frozenset({"none", "empty"})


class SizingError(ValueError):
    """Raised when no configured accelerator class can hold a target."""


class GpuClass(NamedTuple):
    """A backend-neutral accelerator class and its provisional token ceiling."""

    name: str
    memory_gb: int
    max_tokens: int


# Ordered by device memory. Escalation stops at the largest configured class:
# a target that exceeds it must fail at planning time rather than be launched
# onto a card it cannot fit, because an out-of-memory failure is discovered only
# after the GPU has already been paid for.
GPU_LADDER: tuple[GpuClass, ...] = (
    GpuClass("l4", 24, 320),
    GpuClass("a100-40gb", 40, 768),
    GpuClass("l40s", 48, 1024),
    GpuClass("a100-80gb", 80, 2048),
)

GPU_CLASS_NAMES = frozenset(gpu.name for gpu in GPU_LADDER)


def _count_smiles_heavy_atoms(smiles: str) -> int:
    """Approximate heavy-atom count without a chemistry dependency.

    The core is deliberately dependency-free, and sizing only needs an
    order-of-magnitude figure: polymer length dominates the token count.
    """

    count = 0
    index = 0
    length = len(smiles)
    while index < length:
        char = smiles[index]
        if char == "[":
            close = smiles.find("]", index)
            if close == -1:
                break
            count += 1
            index = close + 1
            continue
        if smiles[index : index + 2] in ("Cl", "Br"):
            count += 1
            index += 2
            continue
        if char in "BCNOPSFI" or char in "bcnops":
            count += 1
        index += 1
    return count


def _entity_tokens(entity: Mapping[str, Any]) -> int:
    copies = len(entity["chain_ids"])
    if entity["type"] in POLYMER_TYPES:
        return len(entity["sequence"]) * copies
    if entity.get("smiles"):
        return max(1, _count_smiles_heavy_atoms(entity["smiles"])) * copies
    return len(entity.get("ccd_codes", [])) * CCD_ATOM_ESTIMATE * copies


def count_tokens(target: Mapping[str, Any]) -> int:
    """Return the token count of a target, counting each chain copy separately."""

    normalized = validate_target(target)
    return sum(_entity_tokens(entity) for entity in normalized["entities"])


def effective_tokens(
    target: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> int:
    """Return the token count adjusted for the settings that drive memory."""

    tokens = count_tokens(target)
    settings = dict(config or {})

    msa_mode = settings.get("msa_mode", "server")
    if msa_mode not in NO_MSA_MODES:
        tokens = int(tokens * MSA_MEMORY_FACTOR)

    parallel = settings.get("max_parallel_samples", 1)
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise ContractError("config.max_parallel_samples must be a positive integer")
    return tokens * parallel


def derive_gpu_class(
    target: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> str:
    """Return the smallest configured accelerator class that fits a target."""

    tokens = effective_tokens(target, config)
    for gpu in GPU_LADDER:
        if tokens <= gpu.max_tokens:
            return gpu.name
    largest = GPU_LADDER[-1]
    raise SizingError(
        f"target needs about {tokens} tokens, above the largest configured class "
        f"{largest.name} ({largest.max_tokens}); size it explicitly and extend "
        "GPU_LADDER rather than launching a run that may exhaust device memory"
    )


def validate_gpu_class(value: Any) -> str:
    """Validate an operator-supplied class, which always beats the heuristic."""

    if not isinstance(value, str) or value not in GPU_CLASS_NAMES:
        raise SizingError(f"gpu_class must be one of {sorted(GPU_CLASS_NAMES)}")
    return value


def resolve_gpu_class(
    target: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    explicit: Any = None,
) -> str:
    """Resolve the accelerator class, preferring an explicit operator choice.

    An explicit value must win so a benchmark can pin hardware and the heuristic
    cannot silently change what a comparison ran on.
    """

    if explicit is not None:
        return validate_gpu_class(explicit)
    return derive_gpu_class(target, config)


__all__ = [
    "GPU_CLASS_NAMES",
    "GPU_LADDER",
    "GpuClass",
    "SizingError",
    "count_tokens",
    "derive_gpu_class",
    "effective_tokens",
    "resolve_gpu_class",
    "validate_gpu_class",
]
