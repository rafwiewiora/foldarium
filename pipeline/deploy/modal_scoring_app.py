"""Isolated CPU-only Modal adapter for bounded weekly pose scoring.

This app is intentionally separate from ``foldarium-predictions``. Deploying it
cannot replace Brian's prediction app, reserve a GPU, access Supabase, or publish
quiz data. Each function call accepts exactly one receptor/ligand pair and has a
single-container ceiling.

Build/deploy only after review::

    modal deploy pipeline/deploy/modal_scoring_app.py

Run one local pair synchronously::

    modal run pipeline/deploy/modal_scoring_app.py::score_local \
      --protein-path protein.pdb --ligand-path pose.pdb \
      --ligand-smiles 'CCO' --pose-id example-1
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

try:  # Modal is optional in the core/test environment.
    import modal
except ModuleNotFoundError:  # pragma: no cover
    modal = None  # type: ignore[assignment]


APP_NAME = "foldarium-weekly-scoring"
SMINA_VERSION = "2020.12.10"
SMINA_IMAGE_REF = (
    "docker.io/dabbleofdevops/smina:2020.12.10@"
    "sha256:f76919d7c0d9f9a9b22e9bffe444dd611c9d8fef2f14e46d7b55e2276449334e"
)
SCORING_CPU = 1.0
SCORING_MEMORY_MIB = 2048
SCORING_TIMEOUT_SECONDS = 5 * 60
SCORING_MAX_CONTAINERS = 1
MAX_RECEPTOR_BYTES = 25 * 1024 * 1024
MAX_LIGAND_BYTES = 2 * 1024 * 1024

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_CORE_SOURCE = _PIPELINE_ROOT / "src" / "foldarium_pipeline"
_REMOTE_SOURCE_ROOT = "/opt/foldarium"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_POSE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _checked_payload(
    content: bytes,
    expected_sha256: str,
    *,
    field: str,
    maximum_bytes: int,
) -> bytes:
    if not isinstance(content, bytes) or not 1 <= len(content) <= maximum_bytes:
        raise ValueError(f"{field} must contain 1 to {maximum_bytes} bytes")
    if not isinstance(expected_sha256, str) or not _DIGEST_RE.fullmatch(
        expected_sha256
    ):
        raise ValueError(f"{field}_sha256 must be a lowercase SHA-256")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError(f"{field} does not match {field}_sha256")
    return content


def _normalise_pose_id(pose_id: str) -> str:
    if not isinstance(pose_id, str) or not _POSE_ID_RE.fullmatch(pose_id):
        raise ValueError("pose_id must be a safe identifier")
    return pose_id


def _score_payload(
    protein_pdb: bytes,
    ligand_pose_pdb: bytes,
    ligand_smiles: str,
    pose_id: str,
    protein_sha256: str,
    ligand_sha256: str,
) -> dict[str, Any]:
    """Validate and score one exact in-memory artifact pair without publication."""

    from foldarium_pipeline.pose_scoring import score_pose_smina
    from foldarium_pipeline.interactions import calculate_interaction_summary_from_pose

    selected_pose_id = _normalise_pose_id(pose_id)
    protein = _checked_payload(
        protein_pdb,
        protein_sha256,
        field="protein",
        maximum_bytes=MAX_RECEPTOR_BYTES,
    )
    ligand = _checked_payload(
        ligand_pose_pdb,
        ligand_sha256,
        field="ligand",
        maximum_bytes=MAX_LIGAND_BYTES,
    )
    with tempfile.TemporaryDirectory(prefix="foldarium-modal-smina-") as temporary:
        root = Path(temporary)
        protein_path = root / "protein.pdb"
        ligand_path = root / "ligand.pdb"
        protein_path.write_bytes(protein)
        ligand_path.write_bytes(ligand)
        result = score_pose_smina(
            protein_path,
            ligand_path,
            ligand_smiles=ligand_smiles,
            cpu=1,
            timeout_seconds=120,
            expected_smina_version=SMINA_VERSION,
            container_image=SMINA_IMAGE_REF,
        )
        try:
            interaction_summary = calculate_interaction_summary_from_pose(
                protein_path,
                ligand_path,
                ligand_smiles,
            )
        except Exception as exc:
            raise RuntimeError(
                f"interaction scoring failed for opaque pose {selected_pose_id}: {exc}"
            ) from exc
    return {
        "pose_id": selected_pose_id,
        **result,
        "interaction_summary": interaction_summary,
    }


if modal is not None:
    _IS_LOCAL = modal.is_local()
    if _IS_LOCAL and not _CORE_SOURCE.is_dir():
        raise RuntimeError(f"Foldarium core source directory not found: {_CORE_SOURCE}")

    app = modal.App(APP_NAME, include_source=False)
    scoring_image = (
        modal.Image.from_registry(
            SMINA_IMAGE_REF,
            add_python="3.12",
            setup_dockerfile_commands=[
                "RUN cp \"$(command -v smina)\" /usr/local/bin/smina",
                "ENV PATH=/usr/local/bin:/usr/bin:/bin",
                "ENV LD_LIBRARY_PATH=/opt/conda/envs/smina/lib",
            ],
        )
        .entrypoint([])
        .uv_pip_install(
            "rdkit==2026.3.4",
            "prolif==2.2.0",
        )
    )
    if _IS_LOCAL:
        scoring_image = scoring_image.add_local_dir(
            _CORE_SOURCE,
            remote_path=f"{_REMOTE_SOURCE_ROOT}/foldarium_pipeline",
            copy=True,
        ).add_local_file(
            Path(__file__).resolve(),
            remote_path=f"{_REMOTE_SOURCE_ROOT}/modal_scoring_app.py",
            copy=True,
        )
    scoring_image = scoring_image.env({"PYTHONPATH": _REMOTE_SOURCE_ROOT})

    @app.function(
        image=scoring_image,
        cpu=SCORING_CPU,
        memory=SCORING_MEMORY_MIB,
        timeout=SCORING_TIMEOUT_SECONDS,
        max_containers=SCORING_MAX_CONTAINERS,
    )
    def score_pose(
        protein_pdb: bytes,
        ligand_pose_pdb: bytes,
        ligand_smiles: str,
        pose_id: str,
        protein_sha256: str,
        ligand_sha256: str,
    ) -> dict[str, Any]:
        """Score one fixed pose; this function has no credentials or write path."""

        return _score_payload(
            protein_pdb,
            ligand_pose_pdb,
            ligand_smiles,
            pose_id,
            protein_sha256,
            ligand_sha256,
        )

    @app.local_entrypoint()
    def score_local(
        protein_path: str,
        ligand_path: str,
        ligand_smiles: str,
        pose_id: str,
    ) -> None:
        """Synchronously score one reviewed local pair and print its result."""

        protein = Path(protein_path).read_bytes()
        ligand = Path(ligand_path).read_bytes()
        result = score_pose.remote(
            protein,
            ligand,
            ligand_smiles,
            pose_id,
            hashlib.sha256(protein).hexdigest(),
            hashlib.sha256(ligand).hexdigest(),
        )
        print(json.dumps(result, indent=2, sort_keys=True))

else:
    app = None
