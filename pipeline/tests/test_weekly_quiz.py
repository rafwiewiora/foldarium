from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from foldarium_pipeline.contracts import make_prediction_task
from foldarium_pipeline import weekly_quiz as weekly_quiz_module
from foldarium_pipeline.weekly_quiz import (
    publish_staged_weekly_quiz,
    select_complete_method_pairs,
    stage_weekly_quiz,
)

try:
    import gemmi  # noqa: F401
    import numpy  # noqa: F401
    import rdkit  # noqa: F401

    HAS_ASSEMBLY_DEPS = True
except (ImportError, ModuleNotFoundError):
    HAS_ASSEMBLY_DEPS = False


def pdb_fixture(shift: float) -> bytes:
    lines: list[str] = []
    serial = 0
    for residue in range(1, 7):
        for name, offset, element in (("N", 0.0, "N"), ("CA", 1.2, "C"), ("C", 2.4, "C")):
            serial += 1
            x = shift + residue * 3.8 + offset
            lines.append(
                f"ATOM  {serial:5d} {name:<4s} ALA A{residue:4d}    "
                f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 50.00          {element:>2s}"
            )
    for atom_index in range(15):
        serial += 1
        # Covalent-like spacing keeps RDKit connectivity inference stable; the
        # old 0.2 A synthetic spacing created an impossible all-to-all graph.
        x = shift + 10.0 + atom_index * 1.5
        lines.append(
            f"HETATM{serial:5d} C{atom_index + 1:<3d} LIG B{1:4d}    "
            f"{x:8.3f}{2.0:8.3f}{0.0:8.3f}  1.00 70.00           C"
        )
    return ("\n".join(lines) + "\nEND\n").encode()


def target() -> dict:
    return {
        "target_id": "2026-08-08_00000001",
        "entities": [
            {"type": "protein", "chain_ids": ["A"], "sequence": "AAAAAA"},
            {"type": "ligand", "chain_ids": ["B"], "smiles": "CCCCCCCCCCCCCCC"},
        ],
        "source": {"kind": "cameo-prerelease", "week": "2026-08-08"},
        "metadata": {
            "selected_ligand": {"component_id": "DRG", "heavy_atoms": 15}
        },
    }


def run_row(method: str, content: bytes) -> tuple[dict, str]:
    version = "0.4.4" if method == "openfold3" else "2.2.1"
    task = make_prediction_task(
        campaign_id="weekly-2026-08-08",
        target=target(),
        method=method,
        method_version=version,
        container_image=f"registry.example/{method}@sha256:" + "a" * 64,
        config={"diffusion_samples": 1},
        output_uri_prefix="supabase://private/runs",
    )
    digest = hashlib.sha256(content).hexdigest()
    uri = f"supabase://private/sha256/{digest[:2]}/{digest}"
    sample_id = f"{method}-sample-1"
    return (
        {
            "run_id": task["task_id"],
            "target_id": task["target"]["target_id"],
            "method": method,
            "method_version": version,
            "task_payload": task,
            "status": "succeeded",
            "result": {"samples": [{"sample_id": sample_id}]},
            "samples": [
                {
                    "sample_id": sample_id,
                    "sample_index": 1,
                    "predicted_complex": {
                        "object_uri": uri,
                        "sha256": digest,
                        "media_type": "chemical/x-pdb",
                    },
                }
            ],
        },
        uri,
    )


class FakeCoordinator:
    def __init__(self, bucket: str) -> None:
        self.storage_bucket = bucket
        self.stored: list[tuple[bytes, str]] = []
        self.opened: dict | None = None
        self.public_bucket_checked = False

    def require_public_bucket(self) -> None:
        self.public_bucket_checked = True

    def store_bytes(self, content: bytes, media_type: str) -> dict:
        self.stored.append((content, media_type))
        digest = hashlib.sha256(content).hexdigest()
        return {
            "object_uri": f"supabase://{self.storage_bucket}/sha256/{digest[:2]}/{digest}",
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
        }

    def open_weekly_quiz_round(self, **kwargs):
        self.opened = kwargs
        return {"status": "open", "round_id": kwargs["round_id"]}


class WeeklyQuizPairSelectionTests(unittest.TestCase):
    def test_keeps_newest_complete_pair_and_reports_replacement_runs(self) -> None:
        rows = [
            {"target_id": "complete", "method": "boltz2", "run_id": "boltz-new"},
            {"target_id": "complete", "method": "boltz2", "run_id": "boltz-old"},
            {"target_id": "complete", "method": "openfold3", "run_id": "of3-new"},
            {"target_id": "complete", "method": "future-method", "run_id": "future"},
            {"target_id": "partial", "method": "openfold3", "run_id": "of3-only"},
        ]

        complete, omitted, replacements = select_complete_method_pairs(rows)

        self.assertEqual(
            {(row["method"], row["run_id"]) for row in complete},
            {("boltz2", "boltz-new"), ("openfold3", "of3-new")},
        )
        self.assertEqual(omitted, [{"target_id": "partial", "succeeded_methods": ["openfold3"]}])
        self.assertEqual(
            replacements,
            [{
                "target_id": "complete",
                "method": "boltz2",
                "selected_run_id": "boltz-new",
                "ignored_run_ids": ["boltz-old"],
            }],
        )


class WeeklyQuizReceptorMedoidTests(unittest.TestCase):
    def test_selects_minimum_total_pairwise_rmsd_without_method_labels(self) -> None:
        choices = [
            {
                "run_id": f"run-{label}",
                "sample_id": f"sample-{label}",
                "artifact_sha256": label * 64,
                "model": label,
            }
            for label in ("a", "b", "c")
        ]
        positions = {"a": 0.0, "b": 2.0, "c": 10.0}

        medoid, audit = weekly_quiz_module._select_receptor_medoid(
            choices,
            round_id="weekly-test-v3",
            target_id="target-1",
            aligner=lambda reference, predicted: {
                "receptor_rmsd": abs(positions[reference] - positions[predicted])
            },
        )

        self.assertEqual(medoid["model"], "b")
        self.assertEqual(
            audit["policy"], weekly_quiz_module.RECEPTOR_ANCHOR_POLICY
        )
        self.assertEqual(audit["total_pairwise_receptor_rmsd"], 10.0)
        self.assertRegex(audit["choice_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(audit["distance_matrix_sha256"], r"^[0-9a-f]{64}$")


def fake_ligand(atomic_numbers: list[int]) -> list[SimpleNamespace]:
    from rdkit import Chem

    periodic_table = Chem.GetPeriodicTable()
    return [
        SimpleNamespace(
            name=f"{periodic_table.GetElementSymbol(number)}{index + 1}",
            element=SimpleNamespace(
                atomic_number=number,
                name=periodic_table.GetElementSymbol(number),
            ),
        )
        for index, number in enumerate(atomic_numbers)
    ]


@unittest.skipUnless(HAS_ASSEMBLY_DEPS, "weekly assembly dependencies are optional")
class PairwisePoseDistanceTests(unittest.TestCase):
    def test_uses_shared_receptor_frame_without_ligand_kabsch(self) -> None:
        import numpy
        from rdkit import Chem

        coordinates = [
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
        ]
        matrix, audit = weekly_quiz_module._pairwise_pose_distances(
            [fake_ligand([6, 6]), fake_ligand([6, 6])],
            coordinates,
            ligand_smiles="CC",
            numpy=numpy,
            Chem=Chem,
        )
        # A ligand-only fit would collapse this translation to zero.
        self.assertAlmostEqual(matrix[0][1], 1.0)
        self.assertEqual(audit["policy"], weekly_quiz_module.LEGACY_LIGAND_ORDER_POLICY)
        self.assertEqual(audit["heavy_atom_count"], 2)

    def test_uses_smiles_topology_even_when_coordinates_are_distorted(self) -> None:
        import numpy
        from rdkit import Chem

        matrix, _audit = weekly_quiz_module._pairwise_pose_distances(
            [fake_ligand([6, 6]), fake_ligand([6, 6])],
            [
                [[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]],
                [[0.0, 0.0, 0.0], [50.0, 0.0, 0.0]],
            ],
            ligand_smiles="CC",
            numpy=numpy,
            Chem=Chem,
        )
        self.assertGreater(matrix[0][1], 30.0)

    def test_scores_symmetric_source_atom_permutation_as_equivalent(self) -> None:
        import numpy
        from rdkit import Chem

        matrix, audit = weekly_quiz_module._pairwise_pose_distances(
            [fake_ligand([6, 8, 6]), fake_ligand([6, 8, 6])],
            [
                [[-1.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
                [[1.0, 0.0, 0.0], [0.0, 0.0, 0.0], [-1.0, 0.0, 0.0]],
            ],
            ligand_smiles="COC",
            numpy=numpy,
            Chem=Chem,
        )
        self.assertAlmostEqual(matrix[0][1], 0.0)
        self.assertGreaterEqual(audit["automorphism_count"], 2)

    def test_wrong_output_element_order_fails_closed(self) -> None:
        import numpy
        from rdkit import Chem

        with self.assertRaisesRegex(
            weekly_quiz_module.WeeklyQuizAssemblyError,
            "does not preserve task-SMILES heavy-atom order",
        ):
            weekly_quiz_module._pairwise_pose_distances(
                [fake_ligand([6, 7]), fake_ligand([6, 6])],
                [
                    [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
                    [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
                ],
                ligand_smiles="CN",
                numpy=numpy,
                Chem=Chem,
            )


@unittest.skipUnless(HAS_ASSEMBLY_DEPS, "weekly assembly dependencies are optional")
class WeeklyQuizAssemblyTests(unittest.TestCase):
    def test_aligns_cross_method_poses_and_publishes_only_sanitized_assets(self) -> None:
        openfold, openfold_uri = run_row("openfold3", pdb_fixture(0.0))
        boltz, boltz_uri = run_row("boltz2", pdb_fixture(20.0))
        downloads = {
            openfold_uri: pdb_fixture(0.0),
            boltz_uri: pdb_fixture(20.0),
        }

        def download(uri: str, *, expected_sha256: str) -> bytes:
            content = downloads[uri]
            self.assertEqual(hashlib.sha256(content).hexdigest(), expected_sha256)
            return content

        with tempfile.TemporaryDirectory() as temporary:
            stage = stage_weekly_quiz(
                [boltz, openfold],
                temporary,
                round_id="weekly-2026-08-08",
                campaign_id="weekly-2026-08-08",
                downloader=download,
            )
            self.assertEqual(len(stage["items"]), 1)
            self.assertEqual(len(stage["items"][0]["choices"]), 2)
            self.assertEqual(stage["items"][0]["clustering"]["cluster_count"], 1)
            self.assertEqual(
                sum(choice["is_rep"] for choice in stage["items"][0]["choices"]),
                1,
            )
            self.assertEqual(
                len({choice["cluster_id"] for choice in stage["items"][0]["choices"]}),
                1,
            )
            for choice in stage["items"][0]["choices"]:
                self.assertTrue(Path(temporary, choice["protein_path"]).is_file())
                self.assertTrue(Path(temporary, choice["pocket_path"]).is_file())
            poses = [
                Path(temporary, choice["pose_path"]).read_text()
                for choice in stage["items"][0]["choices"]
            ]
            self.assertNotIn("openfold", "".join(poses).lower())
            self.assertNotIn("boltz", "".join(poses).lower())
            xyz = []
            for pose in poses:
                rows = [line for line in pose.splitlines() if line.startswith("HETATM")]
                xyz.append([[float(line[30:38]), float(line[38:46]), float(line[46:54])] for line in rows])
            self.assertLess(
                max(abs(left - right) for a, b in zip(xyz[0], xyz[1]) for left, right in zip(a, b)),
                0.01,
            )

            private = FakeCoordinator("private")
            public = FakeCoordinator("quiz-public")
            summary = publish_staged_weekly_quiz(
                temporary,
                private_coordinator=private,
                public_coordinator=public,
                opens_at="2026-08-08T03:00:00Z",
                closes_at="2026-08-12T00:00:00Z",
                open_round=True,
            )
            self.assertEqual(summary["status"], "opened")
            self.assertEqual(summary["choice_count"], 2)
            self.assertTrue(public.public_bucket_checked)
            blind = private.opened["blind_manifest"]
            self.assertNotIn("method", json.dumps(blind))
            self.assertNotIn("clustering", blind["items"][0])
            self.assertEqual(len(blind["items"][0]["choices"]), 2)
            self.assertTrue(all("cluster_id" in choice for choice in blind["items"][0]["choices"]))
            self.assertTrue(all("is_rep" in choice for choice in blind["items"][0]["choices"]))
            self.assertTrue(all("protein_uri" in choice for choice in blind["items"][0]["choices"]))
            self.assertTrue(all("pocket_uri" in choice for choice in blind["items"][0]["choices"]))
            self.assertTrue(
                blind["items"][0]["choices"][0]["pose_uri"].startswith(
                    "supabase://quiz-public/"
                )
            )
            private_index = json.loads(private.stored[0][0])
            self.assertEqual(
                {choice["method"] for choice in private_index["items"][0]["choices"]},
                {"openfold3", "boltz2"},
            )
            clustering = private_index["items"][0]["clustering"]
            self.assertIn("distance_matrix_sha256", clustering)
            self.assertEqual(clustering["threshold_angstrom"], 2.0)
            self.assertIn("no ligand superposition", clustering["distance_metric"])
            self.assertEqual(
                clustering["receptor_anchor"]["policy"],
                weekly_quiz_module.RECEPTOR_ANCHOR_POLICY,
            )
            mapping = clustering["ligand_atom_mapping"]
            self.assertEqual(
                mapping["policy"],
                weekly_quiz_module.LEGACY_LIGAND_ORDER_POLICY,
            )
            self.assertEqual(mapping["heavy_atom_count"], 15)
            self.assertEqual(
                {(row["method"], row["method_version"]) for row in mapping["choices"]},
                {("openfold3", "0.4.4"), ("boltz2", "2.2.1")},
            )


if __name__ == "__main__":
    unittest.main()
