"""Bounded, reproducible score-only evaluation of one cofolded ligand pose.

This module deliberately knows nothing about Modal, Supabase, or weekly-round
assembly.  Callers must select an exact pose-specific receptor and ligand.  The
returned record is suitable for attaching to a private pose record before a
separate publication step.

Smina requires ligand bond order even when only evaluating fixed coordinates.
Weekly Foldarium poses are heavy-atom PDB files whose atom order is inherited
from the task SMILES.  When such a PDB is supplied, ``ligand_smiles`` is required
and RDKit reconstructs an SDF with the task graph and the predicted coordinates.
No docking or minimization is performed.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence


SMINA_SCORE_SCHEMA_VERSION = "foldarium.pose-score/v1"
SMINA_SCORE_PROTOCOL_VERSION = "foldarium.smina-score-only/v1"
SMINA_LIGAND_PREPARATION_VERSION = "task-smiles-pose-coordinates-rdkit/v1"
SMINA_EXPECTED_VERSION = "2020.12.10"
SMINA_DEFAULT_SCORING_FUNCTION = "vina"
SMINA_ALLOWED_SCORING_FUNCTIONS = frozenset({"vina", "vinardo"})
SMINA_MAX_CPU = 4
SMINA_MAX_TIMEOUT_SECONDS = 5 * 60
SMINA_MAX_RECEPTOR_BYTES = 25 * 1024 * 1024
SMINA_MAX_LIGAND_BYTES = 2 * 1024 * 1024

_AFFINITY_RE = re.compile(
    r"(?mi)^\s*Affinity:\s*"
    r"(?P<affinity>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*\(kcal/mol\)\s*$"
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class PoseScoringError(RuntimeError):
    """Raised when an input or score violates the fixed scoring protocol."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_file(path: str | Path, field: str, maximum_bytes: int) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise PoseScoringError(f"{field} is not a file")
    size = resolved.stat().st_size
    if size < 1:
        raise PoseScoringError(f"{field} is empty")
    if size > maximum_bytes:
        raise PoseScoringError(f"{field} exceeds {maximum_bytes} bytes")
    return resolved


def _resolve_binary(binary: str | Path) -> Path:
    candidate = str(binary)
    resolved = shutil.which(candidate) if "/" not in candidate else candidate
    if not resolved:
        raise PoseScoringError("smina binary was not found")
    path = Path(resolved).resolve()
    if not path.is_file():
        raise PoseScoringError("smina binary was not found")
    return path


def _validate_limits(cpu: int, timeout_seconds: int) -> None:
    if isinstance(cpu, bool) or not isinstance(cpu, int) or not 1 <= cpu <= SMINA_MAX_CPU:
        raise PoseScoringError(f"cpu must be an integer from 1 to {SMINA_MAX_CPU}")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= SMINA_MAX_TIMEOUT_SECONDS
    ):
        raise PoseScoringError(
            "timeout_seconds must be an integer from 1 to "
            f"{SMINA_MAX_TIMEOUT_SECONDS}"
        )


def _pose_pdb_to_sdf(
    pose_path: Path,
    ligand_smiles: str,
    output_path: Path,
) -> str:
    """Combine the task graph with the predicted heavy-atom coordinates."""

    if not isinstance(ligand_smiles, str) or not ligand_smiles.strip():
        raise PoseScoringError("ligand_smiles is required for a PDB ligand pose")
    try:
        from rdkit import Chem, rdBase
    except (ImportError, ModuleNotFoundError) as exc:
        raise PoseScoringError(
            "RDKit is required to reconstruct a PDB pose's ligand bond order"
        ) from exc

    template = Chem.MolFromSmiles(ligand_smiles.strip())
    if template is None:
        raise PoseScoringError("ligand_smiles could not be parsed")
    template = Chem.RemoveHs(template)
    pose = Chem.MolFromPDBFile(
        str(pose_path),
        sanitize=False,
        removeHs=True,
        proximityBonding=False,
    )
    if pose is None or pose.GetNumConformers() != 1:
        raise PoseScoringError("ligand pose PDB could not be parsed")
    expected = [atom.GetAtomicNum() for atom in template.GetAtoms()]
    observed = [atom.GetAtomicNum() for atom in pose.GetAtoms()]
    if observed != expected:
        raise PoseScoringError(
            "ligand pose atom order does not match task-SMILES heavy-atom order"
        )

    prepared = Chem.Mol(template)
    coordinates = pose.GetConformer()
    conformer = Chem.Conformer(prepared.GetNumAtoms())
    for index in range(prepared.GetNumAtoms()):
        conformer.SetAtomPosition(index, coordinates.GetAtomPosition(index))
    prepared.RemoveAllConformers()
    prepared.AddConformer(conformer, assignId=True)
    writer = Chem.SDWriter(str(output_path))
    try:
        writer.write(prepared)
    finally:
        writer.close()
    if not output_path.is_file() or output_path.stat().st_size < 1:
        raise PoseScoringError("ligand SDF preparation produced no output")
    return str(rdBase.rdkitVersion)


def _prepare_ligand(
    ligand_path: Path,
    ligand_smiles: str | None,
    work_directory: Path,
) -> tuple[Path, dict[str, Any]]:
    suffix = ligand_path.suffix.casefold()
    if suffix in {".sdf", ".mol2", ".pdbqt"}:
        return ligand_path, {
            "protocol": "caller-supplied-typed-ligand/v1",
            "input_format": suffix.removeprefix("."),
        }
    if suffix != ".pdb":
        raise PoseScoringError(
            "ligand pose must be SDF, MOL2, PDBQT, or PDB with ligand_smiles"
        )
    prepared = work_directory / "ligand.sdf"
    rdkit_version = _pose_pdb_to_sdf(ligand_path, ligand_smiles or "", prepared)
    return prepared, {
        "protocol": SMINA_LIGAND_PREPARATION_VERSION,
        "input_format": "pdb",
        "output_format": "sdf",
        "rdkit_version": rdkit_version,
        "hydrogens": "added-by-smina",
    }


def _public_command(argv: Sequence[str], receptor: Path, ligand: Path) -> list[str]:
    """Retain the scientific command without leaking local absolute paths."""

    public: list[str] = []
    for index, value in enumerate(argv):
        if index == 0:
            public.append("smina")
        elif value == str(receptor):
            public.append(receptor.name)
        elif value == str(ligand):
            public.append(ligand.name)
        elif index > 0 and argv[index - 1] == "--log":
            public.append("smina.log")
        else:
            public.append(value)
    return public


def _parse_affinity(output: str) -> float:
    matches = [float(match.group("affinity")) for match in _AFFINITY_RE.finditer(output)]
    if len(matches) != 1 or not math.isfinite(matches[0]):
        raise PoseScoringError("smina output did not contain exactly one finite affinity")
    return matches[0]


def score_pose_smina(
    protein_path: str | Path,
    ligand_path: str | Path,
    *,
    ligand_smiles: str | None = None,
    scoring_function: str = SMINA_DEFAULT_SCORING_FUNCTION,
    cpu: int = 1,
    timeout_seconds: int = 120,
    smina_binary: str | Path = "smina",
    expected_smina_version: str = SMINA_EXPECTED_VERSION,
    container_image: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Score one fixed cofolded pose without docking, search, or minimization."""

    _validate_limits(cpu, timeout_seconds)
    if scoring_function not in SMINA_ALLOWED_SCORING_FUNCTIONS:
        raise PoseScoringError(
            f"scoring_function must be one of {sorted(SMINA_ALLOWED_SCORING_FUNCTIONS)}"
        )
    if not isinstance(expected_smina_version, str) or not expected_smina_version:
        raise PoseScoringError("expected_smina_version is required")

    receptor = _bounded_file(
        protein_path, "protein_path", SMINA_MAX_RECEPTOR_BYTES
    )
    ligand_input = _bounded_file(
        ligand_path, "ligand_path", SMINA_MAX_LIGAND_BYTES
    )
    binary = _resolve_binary(smina_binary)
    protein_sha256 = _sha256(receptor)
    ligand_sha256 = _sha256(ligand_input)
    binary_sha256 = _sha256(binary)

    try:
        version_completed = runner(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(timeout_seconds, 30),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PoseScoringError("could not execute smina --version") from exc
    version_output = (
        (version_completed.stdout or "") + "\n" + (version_completed.stderr or "")
    ).strip()
    if version_completed.returncode != 0 or expected_smina_version not in version_output:
        raise PoseScoringError(
            f"smina version does not match pinned {expected_smina_version} runtime"
        )

    with tempfile.TemporaryDirectory(prefix="foldarium-smina-") as temporary:
        work_directory = Path(temporary)
        prepared_ligand, ligand_preparation = _prepare_ligand(
            ligand_input, ligand_smiles, work_directory
        )
        log_path = work_directory / "smina.log"
        argv = [
            str(binary),
            "--receptor",
            str(receptor),
            "--ligand",
            str(prepared_ligand),
            "--score_only",
            "--scoring",
            scoring_function,
            "--cpu",
            str(cpu),
            "--seed",
            "0",
            "--log",
            str(log_path),
        ]
        started = time.monotonic()
        try:
            completed = runner(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise PoseScoringError("smina score-only command timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise PoseScoringError("could not execute smina score-only command") from exc
        duration_seconds = round(time.monotonic() - started, 3)
        if completed.returncode != 0:
            raise PoseScoringError(
                f"smina score-only command failed with exit code {completed.returncode}"
            )
        log_output = ""
        if log_path.is_file():
            log_output = log_path.read_text(encoding="utf-8", errors="replace")
        process_output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        # With ``--log``, some smina builds mirror the same report to stdout.
        # Prefer the log when it contains an affinity so one physical score is
        # never misclassified as two scores merely because it was duplicated.
        affinity_source = log_output if _AFFINITY_RE.search(log_output) else process_output
        affinity = _parse_affinity(affinity_source)

        provenance: dict[str, Any] = {
            "protocol_version": SMINA_SCORE_PROTOCOL_VERSION,
            "mode": "score_only",
            "scoring_function": scoring_function,
            "seed": 0,
            "cpu": cpu,
            "timeout_seconds": timeout_seconds,
            "command": _public_command(argv, receptor, prepared_ligand),
            "tool": {
                "name": "smina",
                "expected_version": expected_smina_version,
                "version_output": version_output,
                "binary_sha256": binary_sha256,
            },
            "inputs": {
                "protein_sha256": protein_sha256,
                "ligand_pose_sha256": ligand_sha256,
            },
            "receptor_preparation": "caller-supplied-predicted-polymer/v1",
            "ligand_preparation": ligand_preparation,
        }
        if container_image is not None:
            provenance["container_image"] = container_image
        return {
            "schema_version": SMINA_SCORE_SCHEMA_VERSION,
            "status": "succeeded",
            "duration_seconds": duration_seconds,
            "scores": {"smina_affinity_kcal_mol": affinity},
            "provenance": provenance,
        }


__all__ = [
    "PoseScoringError",
    "SMINA_EXPECTED_VERSION",
    "SMINA_SCORE_PROTOCOL_VERSION",
    "SMINA_SCORE_SCHEMA_VERSION",
    "score_pose_smina",
]
