"""Deterministic clustering of receptor-aligned ligand-pose distances.

The scientific distance calculation lives with weekly assembly because it owns
the parsed coordinate models.  This module deliberately operates on an
already-computed distance matrix so its order-sensitive greedy rule and audit
contract can be tested without optional chemistry dependencies.
"""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from typing import Any, Mapping, Sequence

from .contracts import canonical_json, stable_id

CLUSTER_RMSD_ANGSTROM = 2.0
CLUSTERING_VERSION = (
    "foldarium-weekly-receptor-aligned-canonical-smiles-symmetry-greedy/v1"
)
DISTANCE_DIGEST_DECIMALS = 6


class PoseClusteringError(ValueError):
    """Raised when a pose ensemble cannot be clustered reproducibly."""


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PoseClusteringError(f"{field} must be a non-empty string")
    return value.strip()


def choice_order_digest(
    round_id: str,
    item_id: str,
    identity: Mapping[str, Any],
) -> str:
    """Return a stable pseudorandom ordering key that never reads a method label."""

    if not isinstance(identity, Mapping):
        raise PoseClusteringError("choice identity must be an object")
    payload = {
        "round_id": _nonempty(round_id, "round_id"),
        "item_id": _nonempty(item_id, "item_id"),
        "run_id": _nonempty(identity.get("run_id"), "identity.run_id"),
        "sample_id": _nonempty(identity.get("sample_id"), "identity.sample_id"),
        "artifact_sha256": _nonempty(
            identity.get("artifact_sha256"), "identity.artifact_sha256"
        ),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _validated_matrix(
    distance_matrix: Sequence[Sequence[float]], count: int
) -> list[list[float]]:
    if len(distance_matrix) != count or any(len(row) != count for row in distance_matrix):
        raise PoseClusteringError("distance matrix must be square and match the choices")
    matrix: list[list[float]] = []
    for row_index, row in enumerate(distance_matrix):
        normalized: list[float] = []
        for column_index, raw in enumerate(row):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise PoseClusteringError("distances must be finite non-negative numbers")
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise PoseClusteringError("distances must be finite non-negative numbers")
            if row_index == column_index and value > 1e-9:
                raise PoseClusteringError("distance matrix diagonal must be zero")
            normalized.append(value)
        matrix.append(normalized)
    for left in range(count):
        for right in range(left + 1, count):
            if not math.isclose(
                matrix[left][right], matrix[right][left], rel_tol=1e-9, abs_tol=1e-9
            ):
                raise PoseClusteringError("distance matrix must be symmetric")
    return matrix


def cluster_distance_matrix(
    round_id: str,
    item_id: str,
    identities: Sequence[Mapping[str, Any]],
    distance_matrix: Sequence[Sequence[float]],
    *,
    threshold: float = CLUSTER_RMSD_ANGSTROM,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Greedy-leader cluster a pose-distance matrix and choose each medoid.

    Choices are pseudorandomly ordered using identity hashes before the
    intentionally order-sensitive grouping.  Membership uses the historical
    strict ``distance < threshold`` rule.  A representative minimizes summed
    within-cluster distance, with the same method-neutral ordering hash as the
    deterministic tie breaker.
    """

    round_id = _nonempty(round_id, "round_id")
    item_id = _nonempty(item_id, "item_id")
    if not identities:
        raise PoseClusteringError("at least one choice is required")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise PoseClusteringError("cluster threshold must be a positive finite number")
    threshold = float(threshold)
    if not math.isfinite(threshold) or threshold <= 0:
        raise PoseClusteringError("cluster threshold must be a positive finite number")
    matrix = _validated_matrix(distance_matrix, len(identities))

    digests = [choice_order_digest(round_id, item_id, identity) for identity in identities]
    if len(set(digests)) != len(digests):
        raise PoseClusteringError("choice identities must be unique")
    order = sorted(range(len(identities)), key=lambda index: digests[index])

    labels: list[int | None] = [None] * len(identities)
    leaders: list[int] = []
    for leader in order:
        if labels[leader] is not None:
            continue
        cluster_index = len(leaders)
        leaders.append(leader)
        labels[leader] = cluster_index
        for candidate in order:
            if labels[candidate] is None and matrix[leader][candidate] < threshold:
                labels[candidate] = cluster_index

    representatives: dict[int, int] = {}
    cluster_ids: dict[int, str] = {}
    for cluster_index, leader in enumerate(leaders):
        members = [index for index in order if labels[index] == cluster_index]
        representatives[cluster_index] = min(
            members,
            key=lambda index: (
                sum(matrix[index][other] for other in members),
                digests[index],
            ),
        )
        cluster_ids[cluster_index] = stable_id(
            "cluster",
            {
                "version": CLUSTERING_VERSION,
                "round_id": round_id,
                "item_id": item_id,
                "leader_choice_digest": digests[leader],
            },
            length=12,
        )

    assignments = [
        {
            "choice_digest": digests[index],
            "cluster_id": cluster_ids[int(labels[index])],
            "is_rep": representatives[int(labels[index])] == index,
        }
        for index in range(len(identities))
    ]
    ordered_matrix = [
        [f"{matrix[left][right]:.{DISTANCE_DIGEST_DECIMALS}f}" for right in order]
        for left in order
    ]
    distance_payload = {
        "choice_order": [digests[index] for index in order],
        "distances_angstrom": ordered_matrix,
    }
    audit = {
        "version": CLUSTERING_VERSION,
        "threshold_angstrom": threshold,
        "threshold_comparison": "<",
        "distance_metric": (
            "canonical-SMILES graph-symmetry-aware heavy-atom RMSD after shared "
            "receptor alignment; adapter-preserved input atom order; no ligand "
            "superposition"
        ),
        "ordering": "sha256(round/item/run/sample/artifact identity); method label excluded",
        "anchor_choice_digest": digests[order[0]],
        "ordered_choice_digests": [digests[index] for index in order],
        "distance_matrix_sha256": hashlib.sha256(
            canonical_json(distance_payload).encode("utf-8")
        ).hexdigest(),
        "distance_digest_decimals": DISTANCE_DIGEST_DECIMALS,
        "cluster_count": len(leaders),
        "clusters": [
            {
                "cluster_id": cluster_ids[cluster_index],
                "leader_choice_digest": digests[leader],
                "representative_choice_digest": digests[representatives[cluster_index]],
                "member_choice_digests": [
                    digests[index]
                    for index in order
                    if labels[index] == cluster_index
                ],
            }
            for cluster_index, leader in enumerate(leaders)
        ],
    }
    return deepcopy(assignments), audit


__all__ = [
    "CLUSTERING_VERSION",
    "CLUSTER_RMSD_ANGSTROM",
    "DISTANCE_DIGEST_DECIMALS",
    "PoseClusteringError",
    "choice_order_digest",
    "cluster_distance_matrix",
]
