from __future__ import annotations

import copy
import unittest

from foldarium_pipeline.clustering import (
    CLUSTERING_VERSION,
    PoseClusteringError,
    choice_order_digest,
    cluster_distance_matrix,
)


def identity(name: str, method: str = "method-hidden-from-ordering") -> dict[str, str]:
    return {
        "run_id": f"run-{name}",
        "sample_id": f"sample-{name}",
        "artifact_sha256": (name.encode().hex() or "0").ljust(64, "0")[:64],
        "method": method,
    }


class PoseClusteringTests(unittest.TestCase):
    def test_keeps_all_ten_raw_choices_when_they_share_one_cluster(self) -> None:
        identities = [identity(str(index)) for index in range(10)]
        matrix = [
            [0.0 if left == right else 0.25 for right in range(10)]
            for left in range(10)
        ]
        assignments, audit = cluster_distance_matrix(
            "week", "item", identities, matrix
        )
        self.assertEqual(len(assignments), 10)
        self.assertEqual(len({row["cluster_id"] for row in assignments}), 1)
        self.assertEqual(sum(row["is_rep"] for row in assignments), 1)
        self.assertEqual(audit["cluster_count"], 1)

    def test_strict_threshold_and_minimum_summed_distance_medoid(self) -> None:
        identities = [identity(name) for name in ("a", "b", "c", "d")]
        ordered = sorted(
            range(len(identities)),
            key=lambda index: choice_order_digest("week", "item", identities[index]),
        )
        leader, middle, edge, separate = ordered
        matrix = [[0.0] * 4 for _ in range(4)]

        def pair(left: int, right: int, value: float) -> None:
            matrix[left][right] = matrix[right][left] = value

        # The stable first leader sees both members at <2 A. The middle member
        # has the lowest summed distance and is therefore the representative.
        pair(leader, middle, 0.8)
        pair(leader, edge, 1.7)
        pair(middle, edge, 1.0)
        # Exactly 2.0 A is deliberately outside the leader's cluster.
        pair(leader, separate, 2.0)
        pair(middle, separate, 2.5)
        pair(edge, separate, 2.4)

        assignments, audit = cluster_distance_matrix(
            "week", "item", identities, matrix
        )

        self.assertEqual(assignments[leader]["cluster_id"], assignments[middle]["cluster_id"])
        self.assertEqual(assignments[leader]["cluster_id"], assignments[edge]["cluster_id"])
        self.assertNotEqual(assignments[leader]["cluster_id"], assignments[separate]["cluster_id"])
        self.assertTrue(assignments[middle]["is_rep"])
        self.assertFalse(assignments[leader]["is_rep"])
        self.assertTrue(assignments[separate]["is_rep"])
        self.assertEqual(audit["version"], CLUSTERING_VERSION)
        self.assertEqual(audit["threshold_comparison"], "<")
        self.assertEqual(audit["cluster_count"], 2)
        self.assertEqual(len(audit["distance_matrix_sha256"]), 64)

    def test_input_permutation_does_not_change_clusters_or_digest(self) -> None:
        identities = [identity(name) for name in ("a", "b", "c")]
        matrix = [
            [0.0, 0.5, 4.0],
            [0.5, 0.0, 4.2],
            [4.0, 4.2, 0.0],
        ]
        original, original_audit = cluster_distance_matrix(
            "week", "item", identities, matrix
        )
        permutation = [2, 0, 1]
        permuted_identities = [identities[index] for index in permutation]
        permuted_matrix = [
            [matrix[left][right] for right in permutation]
            for left in permutation
        ]
        permuted, permuted_audit = cluster_distance_matrix(
            "week", "item", permuted_identities, permuted_matrix
        )
        original_by_run = {
            identities[index]["run_id"]: (
                value["cluster_id"],
                value["is_rep"],
                value["choice_digest"],
            )
            for index, value in enumerate(original)
        }
        permuted_by_run = {
            permuted_identities[index]["run_id"]: (
                value["cluster_id"],
                value["is_rep"],
                value["choice_digest"],
            )
            for index, value in enumerate(permuted)
        }
        self.assertEqual(original_by_run, permuted_by_run)
        self.assertEqual(
            original_audit["distance_matrix_sha256"],
            permuted_audit["distance_matrix_sha256"],
        )
        self.assertEqual(original_audit["clusters"], permuted_audit["clusters"])

    def test_method_label_never_changes_ordering_or_assignments(self) -> None:
        identities = [identity("a", "openfold3"), identity("b", "boltz2")]
        relabeled = copy.deepcopy(identities)
        relabeled[0]["method"] = "boltz2"
        relabeled[1]["method"] = "openfold3"
        matrix = [[0.0, 0.2], [0.2, 0.0]]
        self.assertEqual(
            cluster_distance_matrix("week", "item", identities, matrix),
            cluster_distance_matrix("week", "item", relabeled, matrix),
        )

    def test_rejects_non_symmetric_or_duplicate_identity_inputs(self) -> None:
        with self.assertRaisesRegex(PoseClusteringError, "symmetric"):
            cluster_distance_matrix(
                "week",
                "item",
                [identity("a"), identity("b")],
                [[0.0, 1.0], [1.5, 0.0]],
            )
        with self.assertRaisesRegex(PoseClusteringError, "unique"):
            cluster_distance_matrix(
                "week",
                "item",
                [identity("a"), identity("a")],
                [[0.0, 0.0], [0.0, 0.0]],
            )


if __name__ == "__main__":
    unittest.main()
