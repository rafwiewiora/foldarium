from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foldarium_pipeline.contracts import make_prediction_task
from foldarium_pipeline import weekly_quiz as weekly_quiz_module
from foldarium_pipeline.weekly_quiz import (
    clone_weekly_quiz_manifests,
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


def target(target_id: str = "2026-08-08_00000001") -> dict:
    return {
        "target_id": target_id,
        "entities": [
            {"type": "protein", "chain_ids": ["A"], "sequence": "AAAAAA"},
            {"type": "ligand", "chain_ids": ["B"], "smiles": "CCCCCCCCCCCCCCC"},
        ],
        "source": {"kind": "cameo-prerelease", "week": "2026-08-08"},
        "metadata": {
            "selected_ligand": {"component_id": "DRG", "heavy_atoms": 15}
        },
    }


def run_row(
    method: str,
    content: bytes,
    *,
    target_payload: dict | None = None,
) -> tuple[dict, str]:
    version = "0.4.4" if method == "openfold3" else "2.2.1"
    task = make_prediction_task(
        campaign_id="weekly-2026-08-08",
        target=target_payload or target(),
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
        self.cache_controls: list[str | None] = []
        self.opened: dict | None = None
        self.public_bucket_checked = False
        self.registered_selector_kits: list[dict] = []

    def require_public_bucket(self) -> None:
        self.public_bucket_checked = True

    def store_bytes(
        self,
        content: bytes,
        media_type: str,
        *,
        cache_control: str | None = None,
    ) -> dict:
        self.stored.append((content, media_type))
        self.cache_controls.append(cache_control)
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

    def register_weekly_selector_kit(self, **kwargs):
        self.registered_selector_kits.append(kwargs)
        return {"status": "registered", "round_id": kwargs["round_id"]}


class TrackingPublicCoordinator(FakeCoordinator):
    def __init__(self, bucket: str) -> None:
        super().__init__(bucket)
        self._lock = threading.Lock()
        self.active_uploads = 0
        self.maximum_active_uploads = 0

    def store_bytes(
        self,
        content: bytes,
        media_type: str,
        *,
        cache_control: str | None = None,
    ) -> dict:
        with self._lock:
            self.active_uploads += 1
            self.maximum_active_uploads = max(
                self.maximum_active_uploads, self.active_uploads
            )
        try:
            digest = hashlib.sha256(content).hexdigest()
            time.sleep(0.01 + (int(digest[0], 16) % 3) * 0.005)
            return super().store_bytes(
                content,
                media_type,
                cache_control=cache_control,
            )
        finally:
            with self._lock:
                self.active_uploads -= 1


class FailingPendingExecutor:
    latest = None

    def __init__(self, *args, **kwargs) -> None:
        self.futures: list[Future] = []
        type(self).latest = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        pass

    def submit(self, function, *args, **kwargs):
        future = Future()
        self.futures.append(future)
        if len(self.futures) == 1:
            future.set_exception(RuntimeError("public upload failed"))
        return future


class WeeklyQuizPairSelectionTests(unittest.TestCase):
    def test_revalidates_historical_ligands_and_records_exact_reason(self) -> None:
        eligible = weekly_quiz_module._weekly_ligand_eligibility(
            "DRG", 15, "C" * 15
        )
        disconnected = weekly_quiz_module._weekly_ligand_eligibility(
            "402", 18, "NCCS.[Fe+2].C#O.C#N.CCCCCCCCCCCCCCC"
        )
        peg = weekly_quiz_module._weekly_ligand_eligibility(
            "P4K", 46, "O" + "CCO" * 15
        )

        self.assertTrue(eligible["passed"])
        self.assertIsNone(eligible["reason"])
        self.assertFalse(disconnected["passed"])
        self.assertEqual(disconnected["reason"], "disconnected-smiles")
        self.assertFalse(peg["passed"])
        self.assertEqual(peg["reason"], "artifact-component")

    def test_binds_multi_cluster_first_order_into_both_manifests(self) -> None:
        blind = {
            "round_id": "preview",
            "items": [{"id": "single"}, {"id": "multi-b"}, {"id": "multi-a"}],
        }
        private = {
            "round_id": "preview",
            "items": [{"id": "multi-a"}, {"id": "single"}, {"id": "multi-b"}],
        }

        weekly_quiz_module._order_weekly_manifests(
            blind, private, ["multi-a", "multi-b", "single"]
        )

        self.assertEqual(
            [item["id"] for item in blind["items"]],
            ["multi-a", "multi-b", "single"],
        )
        self.assertEqual(
            [item["id"] for item in private["items"]],
            ["multi-a", "multi-b", "single"],
        )
        self.assertEqual(
            private["blind_manifest_sha256"],
            weekly_quiz_module.manifest_sha256(blind),
        )

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

    def test_clones_digest_bound_manifests_without_changing_choice_ids(self) -> None:
        blind = {
            "schema_version": 1,
            "round_id": "preview-round",
            "items": [{"id": "item-1", "choices": [{"id": "choice-1"}]}],
        }
        private = {
            "schema_version": 1,
            "round_id": "preview-round",
            "items": [{
                "id": "item-1",
                "choices": [{"id": "choice-1", "run_id": "run-1"}],
            }],
            "blind_manifest_sha256": weekly_quiz_module.manifest_sha256(blind),
        }

        promoted_blind, promoted_private = clone_weekly_quiz_manifests(
            blind, private, round_id="production-beta-round"
        )

        self.assertEqual(promoted_blind["round_id"], "production-beta-round")
        self.assertEqual(promoted_private["round_id"], "production-beta-round")
        self.assertEqual(
            promoted_private["blind_manifest_sha256"],
            weekly_quiz_module.manifest_sha256(promoted_blind),
        )
        self.assertEqual(
            promoted_private["items"][0]["choices"][0]["run_id"], "run-1"
        )
        self.assertEqual(blind["round_id"], "preview-round")

    def test_rejects_a_private_index_not_bound_to_the_blind_manifest(self) -> None:
        blind = {
            "schema_version": 1,
            "round_id": "preview-round",
            "items": [{"id": "item-1", "choices": [{"id": "choice-1"}]}],
        }
        private = {
            "schema_version": 1,
            "round_id": "preview-round",
            "items": [{"id": "item-1", "choices": [{"id": "choice-1"}]}],
            "blind_manifest_sha256": "0" * 64,
        }
        with self.assertRaisesRegex(
            weekly_quiz_module.WeeklyQuizAssemblyError, "not digest-bound"
        ):
            clone_weekly_quiz_manifests(blind, private, round_id="replacement")

    def test_clones_an_exact_nonempty_item_subset_and_rebinds_its_digest(self) -> None:
        blind = {
            "schema_version": 1,
            "round_id": "preview-round",
            "items": [
                {"id": "keep", "choices": [{"id": "choice-1"}]},
                {"id": "drop", "choices": [{"id": "choice-2"}]},
            ],
        }
        private = {
            "schema_version": 1,
            "round_id": "preview-round",
            "items": [
                {"id": "keep", "choices": [{"id": "choice-1", "run_id": "run-1"}]},
                {"id": "drop", "choices": [{"id": "choice-2", "run_id": "run-2"}]},
            ],
            "blind_manifest_sha256": weekly_quiz_module.manifest_sha256(blind),
        }

        promoted_blind, promoted_private = clone_weekly_quiz_manifests(
            blind,
            private,
            round_id="filtered-round",
            include_item_ids={"keep"},
        )

        self.assertEqual([item["id"] for item in promoted_blind["items"]], ["keep"])
        self.assertEqual([item["id"] for item in promoted_private["items"]], ["keep"])
        self.assertEqual(
            promoted_private["blind_manifest_sha256"],
            weekly_quiz_module.manifest_sha256(promoted_blind),
        )
        with self.assertRaisesRegex(
            weekly_quiz_module.WeeklyQuizAssemblyError, "cannot be empty"
        ):
            clone_weekly_quiz_manifests(
                blind,
                private,
                round_id="empty-round",
                include_item_ids=set(),
            )


class WeeklyQuizReceptorMedoidTests(unittest.TestCase):
    def test_weekly_alignment_uses_the_complete_task_complex_and_global_tm(self) -> None:
        expected = {
            "receptor_rmsd": 1.0,
            "receptor_tm_score": 0.9,
            "sequence_similarity": 1.0,
            "reference_chains": ["A", "B"],
            "predicted_chains": ["A", "B"],
            "sequence_binding_policy": "exact-task-chain-id-and-sequence/v1",
            "global_coverage": {
                "policy": "fixed-correspondence-normalized-tm-irls/v1",
                "retained_residue_count": 100,
            },
        }
        with patch.object(
            weekly_quiz_module,
            "exact_complex_tm_superposition",
            return_value=expected,
        ) as aligner:
            result = weekly_quiz_module._weekly_receptor_superposition(
                "reference",
                "predicted",
                expected_chain_sequences={"A": "AAAA", "B": "AAAAAA"},
            )

        self.assertEqual(result["receptor_rmsd"], 1.0)
        self.assertEqual(
            result["chain_selection_policy"],
            weekly_quiz_module.RECEPTOR_ALIGNMENT_POLICY,
        )
        self.assertEqual(
            result["global_coverage"]["policy"],
            "fixed-correspondence-normalized-tm-irls/v1",
        )
        self.assertEqual(
            result["sequence_binding_policy"],
            "exact-task-chain-id-and-sequence/v1",
        )
        aligner.assert_called_once_with(
            "reference",
            "predicted",
            expected_chain_sequences={"A": "AAAA", "B": "AAAAAA"},
        )

    def test_binds_every_input_protein_chain_without_ligand_input(self) -> None:
        receptor_target = {
            "entities": [
                {"type": "protein", "chain_ids": ["A"], "sequence": "AAAA"},
                {"type": "ligand", "chain_ids": ["B"], "smiles": "CC"},
                {
                    "type": "protein",
                    "chain_ids": ["Z", "C"],
                    "sequence": "GGGGGG",
                },
                {"type": "protein", "chain_ids": ["D"], "sequence": "TTTTTT"},
            ],
            "metadata": {
                "selected_ligand": {"component_id": "DRG", "heavy_atoms": 2}
            },
        }

        selected = weekly_quiz_module._receptor_complex(receptor_target)
        ligand_changed = weekly_quiz_module._receptor_complex(
            {
                **receptor_target,
                "entities": [
                    *receptor_target["entities"][:1],
                    {"type": "ligand", "chain_ids": ["Q"], "smiles": "NNNN"},
                    *receptor_target["entities"][2:],
                ],
                "metadata": {
                    "selected_ligand": {"component_id": "OTHER", "heavy_atoms": 4}
                },
            }
        )

        self.assertEqual(selected, ligand_changed)
        provenance, sequences = selected
        self.assertEqual(provenance["policy"], weekly_quiz_module.RECEPTOR_ENTITY_POLICY)
        self.assertEqual(sequences, {"A": "AAAA", "C": "GGGGGG", "D": "TTTTTT", "Z": "GGGGGG"})
        self.assertEqual(provenance["chain_count"], 4)
        self.assertEqual(provenance["total_sequence_length"], 22)

    def test_selects_minimum_total_symmetric_tm_distance_without_method_labels(self) -> None:
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

        comparisons: list[tuple[str, str]] = []

        def align(reference: str, predicted: str) -> dict[str, float]:
            comparisons.append((reference, predicted))
            distance = abs(positions[reference] - positions[predicted]) / 10.0
            return {"receptor_tm_score": 1.0 - distance}

        medoid, audit = weekly_quiz_module._select_receptor_medoid(
            choices,
            round_id="weekly-test-v3",
            target_id="target-1",
            aligner=align,
        )

        self.assertEqual(medoid["model"], "b")
        self.assertEqual(
            audit["policy"], weekly_quiz_module.RECEPTOR_ANCHOR_POLICY
        )
        self.assertAlmostEqual(audit["total_pairwise_receptor_distance"], 1.0)
        digest_by_model = {
            choice["model"]: weekly_quiz_module.choice_order_digest(
                "weekly-test-v3",
                "target-1",
                {
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "artifact_sha256": choice["artifact_sha256"],
                },
            )
            for choice in choices
        }
        self.assertEqual(
            comparisons,
            [
                (left, right)
                for index, left in enumerate(
                    sorted(digest_by_model, key=digest_by_model.get)
                )
                for right in sorted(digest_by_model, key=digest_by_model.get)[
                    index + 1 :
                ]
            ],
        )
        self.assertRegex(audit["choice_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(audit["distance_matrix_sha256"], r"^[0-9a-f]{64}$")
        _reverse_medoid, reverse_audit = weekly_quiz_module._select_receptor_medoid(
            list(reversed(choices)),
            round_id="weekly-test-v3",
            target_id="target-1",
            aligner=align,
        )
        self.assertEqual(reverse_audit, audit)

    @staticmethod
    def alignment_qa_fixture(*, aligned, retained, per_chain):
        return {
            "global_coverage": {
                "aligned_residue_count": aligned,
                "retained_residue_count": retained,
                "per_chain": per_chain,
            },
            "post_transform_ca": {
                "policy": "all-sequence-matched-ca-displacement-without-refit/v1",
                "count": aligned,
                "within_5_angstrom_count": retained,
                "rmsd": 12.0,
                "p50": 1.0,
                "p90": 20.0,
                "p95": 30.0,
                "p99": 40.0,
                "max": 50.0,
                "per_chain": [],
            },
        }

    def test_display_qa_rejects_a_small_core_confined_away_from_the_pocket(self) -> None:
        result = weekly_quiz_module._weekly_display_alignment_qa(
            self.alignment_qa_fixture(
                aligned=653,
                retained=116,
                per_chain=[
                    {
                        "chain_id": "A",
                        "aligned_residue_count": 144,
                        "retained_residue_count": 116,
                    },
                    {
                        "chain_id": "B",
                        "aligned_residue_count": 342,
                        "retained_residue_count": 0,
                    },
                    {
                        "chain_id": "C",
                        "aligned_residue_count": 167,
                        "retained_residue_count": 0,
                    },
                ],
            ),
            contact_residue_counts={"B": 17, "C": 12},
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["complex"]["minimum_retained_residue_count"], 131)
        self.assertEqual(
            [failure["code"] for failure in result["failures"]],
            [
                "insufficient_complex_global_coverage",
                "unsupported_ligand_contact_chain",
                "unsupported_ligand_contact_chain",
            ],
        )

    def test_display_qa_rejects_an_unsupported_significant_contact_chain(self) -> None:
        result = weekly_quiz_module._weekly_display_alignment_qa(
            self.alignment_qa_fixture(
                aligned=337,
                retained=316,
                per_chain=[
                    {
                        "chain_id": "A",
                        "aligned_residue_count": 316,
                        "retained_residue_count": 316,
                    },
                    {
                        "chain_id": "B",
                        "aligned_residue_count": 21,
                        "retained_residue_count": 0,
                    },
                ],
            ),
            contact_residue_counts={"A": 4, "B": 4},
        )

        self.assertFalse(result["passed"])
        self.assertEqual(
            [failure["code"] for failure in result["failures"]],
            ["unsupported_ligand_contact_chain"],
        )
        self.assertEqual(result["failures"][0]["minimum_retained_residue_count"], 5)

    def test_display_qa_allows_flexible_noncontact_chains_and_incidental_contacts(self) -> None:
        result = weekly_quiz_module._weekly_display_alignment_qa(
            self.alignment_qa_fixture(
                aligned=140,
                retained=80,
                per_chain=[
                    {
                        "chain_id": "A",
                        "aligned_residue_count": 60,
                        "retained_residue_count": 40,
                    },
                    {
                        "chain_id": "B",
                        "aligned_residue_count": 80,
                        "retained_residue_count": 40,
                    },
                ],
            ),
            contact_residue_counts={"A": 10, "B": 2},
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["failures"], [])


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

    def test_legacy_topology_digest_matches_full_audit(self) -> None:
        smiles = "CCCCCCCCCCCCCCCCC"
        audit = weekly_quiz_module.legacy_ligand_topology_audit(smiles)
        digest = weekly_quiz_module.legacy_ligand_topology_digest(smiles)
        self.assertEqual(
            digest["source_topology_sha256"],
            audit["source_topology_sha256"],
        )
        self.assertEqual(
            digest["source_smiles_sha256"],
            audit["source_smiles_sha256"],
        )
        self.assertEqual(digest["heavy_atom_count"], audit["heavy_atom_count"])

    def test_legacy_topology_ignores_leading_explicit_hydrogen(self) -> None:
        ordinary = "CCCCCCCCCCCCCCCCC"
        explicit = "[H]" + ordinary
        ordinary_digest = weekly_quiz_module.legacy_ligand_topology_digest(ordinary)
        explicit_digest = weekly_quiz_module.legacy_ligand_topology_digest(explicit)
        self.assertEqual(explicit_digest["heavy_atom_count"], 17)
        self.assertEqual(
            explicit_digest["source_topology_sha256"],
            ordinary_digest["source_topology_sha256"],
        )
        self.assertNotEqual(
            explicit_digest["source_smiles_sha256"],
            ordinary_digest["source_smiles_sha256"],
        )

    def test_legacy_topology_matches_stored_explicit_hydrogen_mappings(self) -> None:
        cases = (
            (
                "[H]/N=C(/NCCC[C@@H](C(=O)O)N)\\NP(=O)(O)O",
                "de98809d40b37b01f9a2cc86baf55b0dd6e4aa4160f2eaf611470ec9bb10a4f6",
                16,
            ),
            (
                "[H]/N=C\\c1ncc(cn1)NC(=O)[C@H](c2ccc(cc2)Cl)C3CCN(CC3)C(=O)C",
                "ea900dae651fccbbad512bca1ba3af6d6a10ddfcb33e56551620cc9d37e069a0",
                28,
            ),
        )
        for smiles, expected_topology_sha256, expected_heavy_atoms in cases:
            with self.subTest(expected_topology_sha256=expected_topology_sha256[:8]):
                digest = weekly_quiz_module.legacy_ligand_topology_digest(smiles)
                audit = weekly_quiz_module.legacy_ligand_topology_audit(smiles)
                self.assertEqual(digest["heavy_atom_count"], expected_heavy_atoms)
                self.assertEqual(
                    digest["source_topology_sha256"],
                    expected_topology_sha256,
                )
                self.assertEqual(
                    audit["source_topology_sha256"],
                    expected_topology_sha256,
                )

    def test_legacy_topology_digest_succeeds_when_automorphism_cap_is_exceeded(self) -> None:
        import numpy
        from rdkit import Chem

        smiles = "COC"
        original_cap = weekly_quiz_module.LIGAND_AUTOMORPHISM_CAP
        try:
            weekly_quiz_module.LIGAND_AUTOMORPHISM_CAP = 1
            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "exceeds the clustering automorphism limit",
            ):
                weekly_quiz_module.legacy_ligand_topology_audit(smiles)
            digest = weekly_quiz_module.legacy_ligand_topology_digest(smiles)
            self.assertEqual(digest["heavy_atom_count"], 3)
            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "exceeds the clustering automorphism limit",
            ):
                weekly_quiz_module._pairwise_pose_distances(
                    [fake_ligand([6, 8, 6]), fake_ligand([6, 8, 6])],
                    [
                        [[-1.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
                        [[1.0, 0.0, 0.0], [0.0, 0.0, 0.0], [-1.0, 0.0, 0.0]],
                    ],
                    ligand_smiles=smiles,
                    numpy=numpy,
                    Chem=Chem,
                )
        finally:
            weekly_quiz_module.LIGAND_AUTOMORPHISM_CAP = original_cap

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
    def test_filters_ineligible_historical_ligand_before_download_or_scoring(self) -> None:
        valid_target = target("2026-08-08_valid")
        invalid_target = target("2026-08-08_disconnected")
        invalid_smiles = "NCCS.[Fe+2].C#O.C#N.CCCCCCCCCCCCCCC"
        invalid_target["entities"][1]["smiles"] = invalid_smiles
        invalid_target["metadata"]["selected_ligand"] = {
            "component_id": "402",
            "heavy_atoms": 18,
        }
        valid_openfold, valid_openfold_uri = run_row(
            "openfold3", pdb_fixture(0.0), target_payload=valid_target
        )
        valid_boltz, valid_boltz_uri = run_row(
            "boltz2", pdb_fixture(20.0), target_payload=valid_target
        )
        invalid_openfold, invalid_openfold_uri = run_row(
            "openfold3", pdb_fixture(40.0), target_payload=invalid_target
        )
        invalid_boltz, invalid_boltz_uri = run_row(
            "boltz2", pdb_fixture(60.0), target_payload=invalid_target
        )
        downloads = {
            valid_openfold_uri: pdb_fixture(0.0),
            valid_boltz_uri: pdb_fixture(20.0),
            invalid_openfold_uri: pdb_fixture(40.0),
            invalid_boltz_uri: pdb_fixture(60.0),
        }
        downloaded: list[str] = []

        def download(uri, **_):
            downloaded.append(uri)
            return downloads[uri]

        with tempfile.TemporaryDirectory() as temporary:
            stage = stage_weekly_quiz(
                [valid_openfold, valid_boltz, invalid_openfold, invalid_boltz],
                temporary,
                round_id="weekly-filtered",
                campaign_id="weekly-2026-08-08",
                downloader=download,
            )

        self.assertEqual(
            [item["target_id"] for item in stage["items"]], ["2026-08-08_valid"]
        )
        self.assertEqual(
            stage["ligand_eligibility_rejections"],
            [
                {
                    "target_id": "2026-08-08_disconnected",
                    "policy": "cameo-drug-like/v4",
                    "passed": False,
                    "component_id": "402",
                    "heavy_atoms": 18,
                    "smiles": invalid_smiles,
                    "smiles_sha256": hashlib.sha256(
                        invalid_smiles.encode("utf-8")
                    ).hexdigest(),
                    "reason": "disconnected-smiles",
                }
            ],
        )
        self.assertEqual(set(downloaded), {valid_openfold_uri, valid_boltz_uri})

    class InlineProcessPool:
        """Exercise process orchestration deterministically in the unit sandbox."""

        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            pass

        def submit(self, function, *args, **kwargs):
            future = Future()
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future

    @staticmethod
    def two_target_rows_and_downloads():
        rows = []
        downloads = {}
        for target_id in ("2026-08-08_00000001", "2026-08-08_00000002"):
            target_payload = target(target_id)
            for method, shift in (("openfold3", 0.0), ("boltz2", 20.0)):
                row, uri = run_row(
                    method,
                    pdb_fixture(shift),
                    target_payload=target_payload,
                )
                rows.append(row)
                downloads[uri] = pdb_fixture(shift)
        return rows, downloads

    def test_parallel_medoid_precomputation_is_digest_identical_and_cache_reusable(
        self,
    ) -> None:
        rows, downloads = self.two_target_rows_and_downloads()
        download_calls: list[str] = []

        def download(uri, *, expected_sha256):
            download_calls.append(uri)
            content = downloads[uri]
            self.assertEqual(hashlib.sha256(content).hexdigest(), expected_sha256)
            return content

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary, "cache")
            sequential = Path(temporary, "sequential")
            parallel = Path(temporary, "parallel")
            stage_sequential = stage_weekly_quiz(
                rows,
                sequential,
                round_id="weekly-parallel-determinism",
                campaign_id="weekly-2026-08-08",
                downloader=download,
                artifact_cache_directory=cache,
                artifact_download_workers=2,
            )
            # Both targets share the two exact coordinate payloads.
            self.assertEqual(len(download_calls), 2)
            download_calls.clear()
            with patch.object(
                weekly_quiz_module,
                "ProcessPoolExecutor",
                self.InlineProcessPool,
            ):
                stage_parallel = stage_weekly_quiz(
                    rows,
                    parallel,
                    round_id="weekly-parallel-determinism",
                    campaign_id="weekly-2026-08-08",
                    downloader=download,
                    target_workers=2,
                    artifact_download_workers=2,
                    artifact_cache_directory=cache,
                )
            self.assertEqual(download_calls, [])
            self.assertEqual(stage_parallel, stage_sequential)
            self.assertEqual(
                Path(parallel, "stage.json").read_bytes(),
                Path(sequential, "stage.json").read_bytes(),
            )

    def test_tampered_artifact_cache_fails_closed(self) -> None:
        rows, downloads = self.two_target_rows_and_downloads()

        def download(uri, *, expected_sha256):
            return downloads[uri]

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary, "cache")
            stage_weekly_quiz(
                rows,
                Path(temporary, "first"),
                round_id="weekly-cache-tamper",
                campaign_id="weekly-2026-08-08",
                downloader=download,
                artifact_cache_directory=cache,
            )
            cached_path = next(cache.glob("*/*"))
            cached_path.write_bytes(b"tampered")
            second = Path(temporary, "second")
            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "failed SHA-256 verification",
            ):
                stage_weekly_quiz(
                    rows,
                    second,
                    round_id="weekly-cache-tamper",
                    campaign_id="weekly-2026-08-08",
                    downloader=download,
                    artifact_cache_directory=cache,
                )
            self.assertFalse(Path(second, "stage.json").exists())

    def test_parallel_worker_failure_writes_no_publishable_stage(self) -> None:
        rows, downloads = self.two_target_rows_and_downloads()
        destination = None
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary, "failed")
            with patch.object(
                weekly_quiz_module,
                "ProcessPoolExecutor",
                self.InlineProcessPool,
            ), patch.object(
                weekly_quiz_module,
                "_receptor_medoid_job",
                side_effect=RuntimeError("worker failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "worker failed"):
                    stage_weekly_quiz(
                        rows,
                        destination,
                        round_id="weekly-worker-failure",
                        campaign_id="weekly-2026-08-08",
                        downloader=lambda uri, **_: downloads[uri],
                        target_workers=2,
                    )
            self.assertFalse(Path(destination, "stage.json").exists())
            self.assertFalse(Path(destination, "raw").exists())
            self.assertFalse(Path(destination, "assets").exists())

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

        scoring_calls = []

        def score_choice(*, protein_path, ligand_path, ligand_smiles, pose_id):
            self.assertTrue(Path(protein_path).is_file())
            self.assertTrue(Path(ligand_path).is_file())
            self.assertEqual(ligand_smiles, "CCCCCCCCCCCCCCC")
            scoring_calls.append(pose_id)
            return {
                "pose_id": pose_id,
                "schema_version": "foldarium.pose-score/v1",
                "status": "succeeded",
                "scores": {"smina_affinity_kcal_mol": -7.25},
                "provenance": {
                    "mode": "score_only",
                    "scoring_function": "vina",
                },
                "interaction_summary": {
                    "engine": "prolif",
                    "policy": "prolif-implicit-hbond-unique-protein-residue/v1",
                    "count": 8,
                },
            }

        with tempfile.TemporaryDirectory() as temporary:
            stage = stage_weekly_quiz(
                [boltz, openfold],
                temporary,
                round_id="weekly-2026-08-08",
                campaign_id="weekly-2026-08-08",
                downloader=download,
                choice_scorer=score_choice,
            )
            self.assertEqual(stage["schema_version"], 11)
            self.assertEqual(
                stage["ligand_eligibility_policy"], "cameo-drug-like/v4"
            )
            self.assertEqual(stage["ligand_eligibility_rejections"], [])
            self.assertEqual(
                stage["presentation_policy"],
                weekly_quiz_module.WEEKLY_PRESENTATION_POLICY,
            )
            self.assertEqual(stage["alignment_warnings"], [])
            self.assertEqual(len(stage["items"]), 1)
            self.assertEqual(len(stage["items"][0]["choices"]), 2)
            self.assertEqual(len(scoring_calls), 2)
            self.assertEqual(
                {choice["method"] for choice in stage["items"][0]["choices"]},
                {"openfold3", "boltz2"},
            )
            for choice in stage["items"][0]["choices"]:
                self.assertTrue(choice["alignment"]["display_qa"]["passed"])
                self.assertEqual(
                    choice["alignment"]["display_qa"]["policy"],
                    weekly_quiz_module.DISPLAY_ALIGNMENT_QA_POLICY,
                )
                self.assertEqual(
                    choice["confidence"],
                    {
                        "metric": "ligand_plddt",
                        "value": 70.0,
                        "scale_min": 0.0,
                        "scale_max": 100.0,
                        "aggregation": "arithmetic-mean-selected-ligand-heavy-atoms",
                    },
                )
                self.assertEqual(choice["smina_score"]["value"], -7.25)
                self.assertEqual(choice["interaction_count"]["value"], 8)
                self.assertEqual(
                    choice["interaction_count"]["metric"],
                    "prolif_hbond_residue_count",
                )
            self.assertEqual(stage["items"][0]["clustering"]["cluster_count"], 1)
            self.assertTrue(stage["items"][0]["ligand_eligibility"]["passed"])
            self.assertEqual(
                stage["items"][0]["presentation_group"],
                weekly_quiz_module.WEEKLY_PRESENTATION_SINGLE_CLUSTER,
            )
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
                round_environment="preview",
                round_metadata={"release_channel": "beta"},
            )
            self.assertEqual(summary["status"], "opened")
            self.assertEqual(summary["environment"], "preview")
            self.assertEqual(private.opened["environment"], "preview")
            self.assertEqual(
                private.opened["metadata"]["release_channel"], "beta"
            )
            self.assertIn("stage_sha256", private.opened["metadata"])
            self.assertEqual(summary["choice_count"], 2)
            self.assertEqual(summary["display_alignment_warned_target_count"], 0)
            self.assertEqual(summary["display_alignment_warned_target_ids"], [])
            self.assertEqual(summary["ligand_eligibility_rejected_target_count"], 0)
            self.assertEqual(summary["ligand_eligibility_rejected_target_ids"], [])
            self.assertEqual(summary["multi_cluster_item_count"], 0)
            self.assertEqual(summary["single_cluster_item_count"], 1)
            self.assertTrue(public.public_bucket_checked)
            blind = private.opened["blind_manifest"]
            self.assertNotIn("run_id", json.dumps(blind))
            self.assertNotIn("clustering", blind["items"][0])
            self.assertEqual(
                blind["items"][0]["metadata"]["presentation"],
                {
                    "policy": weekly_quiz_module.WEEKLY_PRESENTATION_POLICY,
                    "group": weekly_quiz_module.WEEKLY_PRESENTATION_SINGLE_CLUSTER,
                    "cluster_count": 1,
                },
            )
            self.assertEqual(len(blind["items"][0]["choices"]), 2)
            self.assertTrue(all("cluster_id" in choice for choice in blind["items"][0]["choices"]))
            self.assertTrue(all("is_rep" in choice for choice in blind["items"][0]["choices"]))
            self.assertTrue(all("protein_uri" in choice for choice in blind["items"][0]["choices"]))
            self.assertTrue(all("pocket_uri" in choice for choice in blind["items"][0]["choices"]))
            self.assertEqual(
                {choice["method"] for choice in blind["items"][0]["choices"]},
                {"openfold3", "boltz2"},
            )
            self.assertTrue(
                all(choice["confidence"]["metric"] == "ligand_plddt"
                    for choice in blind["items"][0]["choices"])
            )
            self.assertTrue(
                all(choice["smina_score"]["metric"] == "smina_affinity"
                    for choice in blind["items"][0]["choices"])
            )
            self.assertTrue(
                all(choice["interaction_count"]["value"] == 8
                    for choice in blind["items"][0]["choices"])
            )
            self.assertTrue(
                blind["items"][0]["choices"][0]["pose_uri"].startswith(
                    "supabase://quiz-public/"
                )
            )
            private_payloads = [
                json.loads(content)
                for content, media_type in private.stored
                if media_type == "application/json"
            ]
            private_index = next(
                payload
                for payload in private_payloads
                if "blind_manifest_sha256" in payload
            )
            warning_index = next(
                payload for payload in private_payloads if "warnings" in payload
            )
            eligibility_index = next(
                payload for payload in private_payloads if "rejections" in payload
            )
            self.assertEqual(
                warning_index,
                {
                    "policy": weekly_quiz_module.DISPLAY_ALIGNMENT_QA_POLICY,
                    "warnings": [],
                    "round_id": "weekly-2026-08-08",
                    "schema_version": 1,
                },
            )
            self.assertEqual(
                eligibility_index,
                {
                    "policy": "cameo-drug-like/v4",
                    "rejections": [],
                    "round_id": "weekly-2026-08-08",
                    "schema_version": 1,
                },
            )
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
            self.assertEqual(
                clustering["receptor_anchor"]["task_receptor_complex"],
                {
                    "policy": weekly_quiz_module.RECEPTOR_ENTITY_POLICY,
                    "chain_count": 1,
                    "total_sequence_length": 6,
                    "chains": [
                        {
                            "chain_id": "A",
                            "sequence_length": 6,
                            "sequence_sha256": hashlib.sha256(b"AAAAAA").hexdigest(),
                        }
                    ],
                },
            )
            for choice in private_index["items"][0]["choices"]:
                self.assertEqual(choice["alignment"]["reference_chains"], ["A"])
                self.assertEqual(choice["alignment"]["predicted_chains"], ["A"])
                self.assertEqual(
                    choice["alignment"]["chain_selection_policy"],
                    weekly_quiz_module.RECEPTOR_ALIGNMENT_POLICY,
                )
                self.assertEqual(
                    choice["alignment"]["sequence_binding_policy"],
                    "exact-task-chain-id-and-sequence/v1",
                )
                self.assertEqual(
                    choice["alignment"]["global_coverage"]["policy"],
                    "fixed-correspondence-normalized-tm-irls/v1",
                )
                self.assertEqual(
                    choice["alignment"]["global_coverage"]["seed_policy"],
                    "identity-all-ca-each-chain-and-24-ca-overlapping-windows/v1",
                )
                self.assertTrue(choice["alignment"]["display_qa"]["passed"])
                self.assertEqual(
                    choice["alignment"]["global_coverage"]["per_chain"][0]["chain_id"],
                    "A",
                )
                self.assertEqual(
                    choice["alignment"]["global_coverage"]["per_chain"][0][
                        "retained_fraction"
                    ],
                    1.0,
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

            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "cannot override publication metadata",
            ):
                publish_staged_weekly_quiz(
                    temporary,
                    private_coordinator=private,
                    public_coordinator=public,
                    opens_at="2026-08-08T03:00:00Z",
                    closes_at="2026-08-12T00:00:00Z",
                    round_metadata={"stage_sha256": "operator-value"},
                )

    def test_preserves_a_target_with_a_warning_when_any_pose_has_weak_display_support(
        self,
    ) -> None:
        first = target("2026-08-08_00000001")
        second = target("2026-08-08_00000002")
        rows_and_uris = [
            run_row(method, pdb_fixture(shift), target_payload=target_payload)
            for target_payload in (first, second)
            for method, shift in (("openfold3", 0.0), ("boltz2", 20.0))
        ]
        rows = [row for row, _uri in rows_and_uris]
        downloads = {
            uri: pdb_fixture(0.0 if row["method"] == "openfold3" else 20.0)
            for row, uri in rows_and_uris
        }
        passing = WeeklyQuizReceptorMedoidTests.alignment_qa_fixture(
            aligned=6,
            retained=6,
            per_chain=[{
                "chain_id": "A",
                "aligned_residue_count": 6,
                "retained_residue_count": 6,
            }],
        )
        passing = weekly_quiz_module._weekly_display_alignment_qa(
            passing,
            contact_residue_counts={"A": 3},
        )
        failing = json.loads(json.dumps(passing))
        failing["passed"] = False
        failing["failures"] = [{"code": "unsupported_ligand_contact_chain"}]
        scored_pose_ids: list[str] = []

        def score_batch(requests):
            self.assertEqual(len(requests), 4)
            scored_pose_ids.extend(request["pose_id"] for request in requests)
            return [
                {
                    "pose_id": request["pose_id"],
                    "schema_version": "foldarium.pose-score/v1",
                    "status": "succeeded",
                    "scores": {"smina_affinity_kcal_mol": -7.0},
                    "provenance": {
                        "mode": "score_only",
                        "scoring_function": "vina",
                    },
                    "interaction_summary": {
                        "engine": "prolif",
                        "policy": "test-policy/v1",
                        "count": 1,
                    },
                }
                for request in requests
            ]

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            weekly_quiz_module,
            "_weekly_display_alignment_qa",
            side_effect=[failing, failing, passing, passing] * 2,
        ):
            stage = stage_weekly_quiz(
                rows,
                temporary,
                round_id="weekly-two-targets",
                campaign_id="weekly-2026-08-08",
                downloader=lambda uri, **_: downloads[uri],
                choice_batch_scorer=score_batch,
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
                round_environment="preview",
            )
            blind = private.opened["blind_manifest"]
            warning_index = next(
                json.loads(content)
                for content, media_type in private.stored
                if media_type == "application/json"
                and "warnings" in json.loads(content)
            )

        self.assertEqual(
            [item["target_id"] for item in stage["items"]],
            [first["target_id"], second["target_id"]],
        )
        self.assertEqual(len(scored_pose_ids), 4)
        self.assertEqual(
            stage["items"][0]["alignment_warning"],
            {
                "code": weekly_quiz_module.DISPLAY_ALIGNMENT_WARNING_CODE,
                "message": weekly_quiz_module.DISPLAY_ALIGNMENT_WARNING_MESSAGE,
                "policy": weekly_quiz_module.DISPLAY_ALIGNMENT_QA_POLICY,
                "failed_choice_count": 2,
            },
        )
        self.assertNotIn("alignment_warning", stage["items"][1])
        self.assertEqual(
            [row["target_id"] for row in stage["alignment_warnings"]],
            [first["target_id"]],
        )
        self.assertEqual(len(stage["alignment_warnings"][0]["failed_choices"]), 2)
        self.assertEqual(summary["item_count"], 2)
        self.assertEqual(summary["display_alignment_warned_target_count"], 1)
        self.assertEqual(
            summary["display_alignment_warned_target_ids"], [first["target_id"]]
        )
        warning_by_item = {
            item["id"]: item.get("metadata", {}).get("display_alignment")
            for item in blind["items"]
        }
        self.assertEqual(
            warning_by_item[first["target_id"]],
            {
                "code": weekly_quiz_module.DISPLAY_ALIGNMENT_WARNING_CODE,
                "message": weekly_quiz_module.DISPLAY_ALIGNMENT_WARNING_MESSAGE,
            },
        )
        self.assertIsNone(warning_by_item[second["target_id"]])
        self.assertEqual(
            [row["target_id"] for row in warning_index["warnings"]],
            [first["target_id"]],
        )

    def test_public_uploads_are_bounded_and_preserve_manifest_order(self) -> None:
        openfold, openfold_uri = run_row("openfold3", pdb_fixture(0.0))
        boltz, boltz_uri = run_row("boltz2", pdb_fixture(20.0))
        downloads = {
            openfold_uri: pdb_fixture(0.0),
            boltz_uri: pdb_fixture(20.0),
        }

        with tempfile.TemporaryDirectory() as temporary:
            stage_weekly_quiz(
                [boltz, openfold],
                temporary,
                round_id="weekly-parallel-publication",
                campaign_id="weekly-2026-08-08",
                downloader=lambda uri, **_: downloads[uri],
            )
            sequential_private = FakeCoordinator("private")
            sequential_public = TrackingPublicCoordinator("quiz-public")
            sequential_summary = publish_staged_weekly_quiz(
                temporary,
                private_coordinator=sequential_private,
                public_coordinator=sequential_public,
                opens_at="2026-08-08T03:00:00Z",
                closes_at="2026-08-12T00:00:00Z",
                open_round=True,
                round_environment="preview",
            )

            parallel_private = FakeCoordinator("private")
            parallel_public = TrackingPublicCoordinator("quiz-public")
            parallel_summary = publish_staged_weekly_quiz(
                temporary,
                private_coordinator=parallel_private,
                public_coordinator=parallel_public,
                opens_at="2026-08-08T03:00:00Z",
                closes_at="2026-08-12T00:00:00Z",
                open_round=True,
                round_environment="preview",
                public_upload_workers=2,
            )

        self.assertEqual(sequential_public.maximum_active_uploads, 1)
        self.assertGreater(parallel_public.maximum_active_uploads, 1)
        self.assertLessEqual(parallel_public.maximum_active_uploads, 2)
        self.assertTrue(sequential_public.cache_controls)
        self.assertEqual(
            set(sequential_public.cache_controls),
            {weekly_quiz_module.IMMUTABLE_PUBLIC_CACHE_CONTROL},
        )
        self.assertEqual(set(sequential_private.cache_controls), {None})
        self.assertEqual(
            parallel_private.opened["blind_manifest"],
            sequential_private.opened["blind_manifest"],
        )
        self.assertEqual(parallel_private.stored, sequential_private.stored)
        self.assertEqual(
            parallel_summary["blind_manifest_sha256"],
            sequential_summary["blind_manifest_sha256"],
        )

    def test_public_upload_failure_cancels_pending_and_never_writes_private_state(
        self,
    ) -> None:
        openfold, openfold_uri = run_row("openfold3", pdb_fixture(0.0))
        boltz, boltz_uri = run_row("boltz2", pdb_fixture(20.0))
        downloads = {
            openfold_uri: pdb_fixture(0.0),
            boltz_uri: pdb_fixture(20.0),
        }

        with tempfile.TemporaryDirectory() as temporary:
            stage_weekly_quiz(
                [boltz, openfold],
                temporary,
                round_id="weekly-publication-failure",
                campaign_id="weekly-2026-08-08",
                downloader=lambda uri, **_: downloads[uri],
            )
            private = FakeCoordinator("private")
            public = FakeCoordinator("quiz-public")
            with patch.object(
                weekly_quiz_module,
                "ThreadPoolExecutor",
                FailingPendingExecutor,
            ), self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "public Storage upload batch failed",
            ):
                publish_staged_weekly_quiz(
                    temporary,
                    private_coordinator=private,
                    public_coordinator=public,
                    opens_at="2026-08-08T03:00:00Z",
                    closes_at="2026-08-12T00:00:00Z",
                    open_round=True,
                    public_upload_workers=2,
                )

            executor = FailingPendingExecutor.latest
            self.assertIsNotNone(executor)
            self.assertGreater(len(executor.futures), 1)
            self.assertTrue(all(future.cancelled() for future in executor.futures[1:]))
            self.assertEqual(private.stored, [])
            self.assertIsNone(private.opened)
            self.assertFalse(Path(temporary, "blind-manifest.json").exists())
            self.assertFalse(Path(temporary, "private-index.json").exists())

    def test_public_upload_result_must_match_exact_content_digest(self) -> None:
        class InvalidResultCoordinator(FakeCoordinator):
            def store_bytes(self, content: bytes, media_type: str) -> dict:
                result = super().store_bytes(content, media_type)
                result["sha256"] = "0" * 64
                return result

        openfold, openfold_uri = run_row("openfold3", pdb_fixture(0.0))
        boltz, boltz_uri = run_row("boltz2", pdb_fixture(20.0))
        downloads = {
            openfold_uri: pdb_fixture(0.0),
            boltz_uri: pdb_fixture(20.0),
        }

        with tempfile.TemporaryDirectory() as temporary:
            stage_weekly_quiz(
                [boltz, openfold],
                temporary,
                round_id="weekly-publication-invalid-result",
                campaign_id="weekly-2026-08-08",
                downloader=lambda uri, **_: downloads[uri],
            )
            private = FakeCoordinator("private")
            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "does not match its content digest",
            ):
                publish_staged_weekly_quiz(
                    temporary,
                    private_coordinator=private,
                    public_coordinator=InvalidResultCoordinator("quiz-public"),
                    opens_at="2026-08-08T03:00:00Z",
                    closes_at="2026-08-12T00:00:00Z",
                    open_round=True,
                    public_upload_workers=1,
                )
            self.assertEqual(private.stored, [])
            self.assertIsNone(private.opened)

    def test_publication_rejects_missing_display_qa_before_any_storage_access(self) -> None:
        openfold, openfold_uri = run_row("openfold3", pdb_fixture(0.0))
        boltz, boltz_uri = run_row("boltz2", pdb_fixture(20.0))
        downloads = {
            openfold_uri: pdb_fixture(0.0),
            boltz_uri: pdb_fixture(20.0),
        }

        with tempfile.TemporaryDirectory() as temporary:
            stage = stage_weekly_quiz(
                [boltz, openfold],
                temporary,
                round_id="weekly-tampered",
                campaign_id="weekly-2026-08-08",
                downloader=lambda uri, **_: downloads[uri],
            )
            alignment = stage["items"][0]["choices"][0]["alignment"]
            display_qa = alignment.pop("display_qa")
            unhashed = {key: value for key, value in stage.items() if key != "stage_sha256"}
            stage["stage_sha256"] = hashlib.sha256(
                weekly_quiz_module.canonical_json(unhashed).encode("utf-8")
            ).hexdigest()
            Path(temporary, "stage.json").write_text(
                json.dumps(stage, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            private = FakeCoordinator("private")
            public = FakeCoordinator("quiz-public")

            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "lacks a valid display alignment QA",
            ):
                publish_staged_weekly_quiz(
                    temporary,
                    private_coordinator=private,
                    public_coordinator=public,
                    opens_at="2026-08-08T03:00:00Z",
                    closes_at="2026-08-12T00:00:00Z",
                )

            self.assertFalse(public.public_bucket_checked)
            self.assertEqual(public.stored, [])
            self.assertEqual(private.stored, [])

            alignment["display_qa"] = display_qa
            alignment["global_coverage"]["retained_residue_count"] = 0
            alignment["global_coverage"]["per_chain"][0]["retained_residue_count"] = 0
            alignment["global_coverage"]["per_chain"][0]["retained_fraction"] = 0.0
            unhashed = {key: value for key, value in stage.items() if key != "stage_sha256"}
            stage["stage_sha256"] = hashlib.sha256(
                weekly_quiz_module.canonical_json(unhashed).encode("utf-8")
            ).hexdigest()
            Path(temporary, "stage.json").write_text(
                json.dumps(stage, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            private = FakeCoordinator("private")
            public = FakeCoordinator("quiz-public")

            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "does not match its provenance",
            ):
                publish_staged_weekly_quiz(
                    temporary,
                    private_coordinator=private,
                    public_coordinator=public,
                    opens_at="2026-08-08T03:00:00Z",
                    closes_at="2026-08-12T00:00:00Z",
                )

            self.assertFalse(public.public_bucket_checked)
            self.assertEqual(public.stored, [])
            self.assertEqual(private.stored, [])

    def test_batch_scores_only_after_all_exact_inputs_are_staged_in_order(self) -> None:
        openfold, openfold_uri = run_row("openfold3", pdb_fixture(0.0))
        boltz, boltz_uri = run_row("boltz2", pdb_fixture(20.0))
        downloads = {
            openfold_uri: pdb_fixture(0.0),
            boltz_uri: pdb_fixture(20.0),
        }
        observed_pose_ids: list[str] = []

        def score_batch(requests):
            self.assertIsInstance(requests, tuple)
            self.assertEqual(len(requests), 2)
            self.assertTrue(
                all(
                    Path(request["protein_path"]).is_file()
                    and Path(request["ligand_path"]).is_file()
                    for request in requests
                )
            )
            observed_pose_ids.extend(request["pose_id"] for request in requests)
            return [
                {
                    "pose_id": request["pose_id"],
                    "schema_version": "foldarium.pose-score/v1",
                    "status": "succeeded",
                    "scores": {"smina_affinity_kcal_mol": -7.0 - index},
                    "provenance": {
                        "mode": "score_only",
                        "scoring_function": "vina",
                    },
                    "interaction_summary": {
                        "engine": "prolif",
                        "policy": "prolif-implicit-hbond-unique-protein-residue/v1",
                        "count": index,
                    },
                }
                for index, request in enumerate(requests)
            ]

        with tempfile.TemporaryDirectory() as temporary:
            stage = stage_weekly_quiz(
                [boltz, openfold],
                temporary,
                round_id="weekly-batch",
                campaign_id="weekly-2026-08-08",
                downloader=lambda uri, **_: downloads[uri],
                choice_batch_scorer=score_batch,
            )

        choices = stage["items"][0]["choices"]
        self.assertEqual(
            [choice["scoring"]["pose_id"] for choice in choices],
            observed_pose_ids,
        )
        self.assertEqual(
            [choice["smina_score"]["value"] for choice in choices],
            [-7.0, -8.0],
        )

    def test_batch_score_failure_never_writes_a_publishable_stage(self) -> None:
        openfold, openfold_uri = run_row("openfold3", pdb_fixture(0.0))
        boltz, boltz_uri = run_row("boltz2", pdb_fixture(20.0))
        downloads = {
            openfold_uri: pdb_fixture(0.0),
            boltz_uri: pdb_fixture(20.0),
        }

        def score_batch(requests):
            results = []
            for request in requests:
                results.append(
                    {
                        "pose_id": request["pose_id"],
                        "schema_version": "foldarium.pose-score/v1",
                        "status": "succeeded",
                        "scores": {"smina_affinity_kcal_mol": -7.0},
                        "provenance": {
                            "mode": "score_only",
                            "scoring_function": "vina",
                        },
                        "interaction_summary": {
                            "engine": "prolif",
                            "policy": "test-policy/v1",
                            "count": 1,
                        },
                    }
                )
            results[-1]["pose_id"] = "wrong-pose-id"
            return results

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                weekly_quiz_module.WeeklyQuizAssemblyError,
                "wrong pose identity",
            ):
                stage_weekly_quiz(
                    [boltz, openfold],
                    temporary,
                    round_id="weekly-batch-failure",
                    campaign_id="weekly-2026-08-08",
                    downloader=lambda uri, **_: downloads[uri],
                    choice_batch_scorer=score_batch,
                )
            self.assertFalse(Path(temporary, "stage.json").exists())


if __name__ == "__main__":
    unittest.main()
