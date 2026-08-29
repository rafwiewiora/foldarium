from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from foldarium_pipeline.rnp_similarity import (
    RNP_NOVELTY_THRESHOLD,
    RNP_STYLE_METHOD,
    RNP_STYLE_VERSION,
)
from foldarium_pipeline.training_similarity import TrainingSimilarityError
from foldarium_pipeline.weekly_training_audit import (
    AUDIT_FORMAT,
    BlindTarget,
    ExactTarget,
    WeeklyTrainingAuditError,
    load_all_targets,
    run_audit,
    score_blind_target,
    score_exact_target,
    targets_from_detail,
)


def _rnp_result(
    score: float, *, train_pdb: str = "2DEF", train_het: str = "DRG"
) -> dict:
    return {
        "method": RNP_STYLE_METHOD,
        "version": RNP_STYLE_VERSION,
        "threshold": RNP_NOVELTY_THRESHOLD,
        "classification": "familiar" if score >= RNP_NOVELTY_THRESHOLD else "novel",
        "novel": score < RNP_NOVELTY_THRESHOLD,
        "reason": "test",
        "sucos_shape_pocket_qcov": score,
        "train_pdb": train_pdb,
        "train_het": train_het,
    }


def _detail() -> dict:
    reference = "https://files.rcsb.org/download/1ABC.cif.gz"
    return {
        "format_version": "foldarium.weekly-retrospective-detail/v1",
        "round": {
            "round_id": "weekly-test-v1",
            "blind_week": "2026-01-01",
            "item_count": 1,
        },
        "blind_manifest": {
            "items": [
                {
                    "id": "1ABC",
                    "ligand": {"component_id": "DRG"},
                    "protein_uri": "https://example.supabase.co/storage/v1/object/public/bucket/"
                    + "a" * 64,
                    "pocket_uri": "https://example.supabase.co/storage/v1/object/public/bucket/"
                    + "b" * 64,
                    "choices": [
                        {
                            "id": "pose-1",
                            "pose_uri": "https://example.supabase.co/storage/v1/object/public/bucket/"
                            + "c" * 64,
                        }
                    ],
                }
            ]
        },
        "reveal_manifest": {
            "items": [
                {
                    "id": "1ABC",
                    "choices": [
                        {
                            "id": "pose-1",
                            "correct": True,
                            "reference_uri": reference,
                        }
                    ],
                }
            ]
        },
        "answer_overlays": [
            {
                "item_id": "1ABC",
                "crystal_ligand_pdb": (
                    "HETATM    1 C1   LIG X   1       0.000   0.000   0.000"
                    "  1.00  0.00           C\nEND\n"
                ),
            }
        ],
        "retrospective": {
            "questions": [
                {
                    "item_id": "1ABC",
                    "automated_entries": [
                        {"participant": "Boltz-2", "correct": True}
                    ],
                }
            ]
        },
    }


class WeeklyTrainingAuditContractTests(unittest.TestCase):
    def test_full_audit_requires_exactly_three_publications(self) -> None:
        with mock.patch(
            "foldarium_pipeline.weekly_training_audit.load_publications",
            return_value=[],
        ), self.assertRaisesRegex(WeeklyTrainingAuditError, "3 published rounds"):
            load_all_targets("https://www.foldarium.org", "/tmp/cache")

    def test_blind_target_contains_no_reveal_side_fields(self) -> None:
        blind, exact = targets_from_detail(_detail())
        self.assertEqual(len(blind), 1)
        self.assertEqual(len(exact), 1)
        blind_payload = asdict(blind[0])
        self.assertEqual(
            set(blind_payload),
            {
                "round_id",
                "blind_week",
                "item_id",
                "ligand_component_id",
                "protein_uri",
                "pocket_uri",
                "choices",
            },
        )
        serialized = repr(blind_payload)
        for forbidden in ("correct", "reference_uri", "crystal", "rmsd", "overlay"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_reference_must_match_the_item_pdb(self) -> None:
        detail = _detail()
        detail["reveal_manifest"]["items"][0]["choices"][0][
            "reference_uri"
        ] = "https://files.rcsb.org/download/2DEF.cif.gz"
        with self.assertRaisesRegex(WeeklyTrainingAuditError, "reference URI"):
            targets_from_detail(detail)

    def test_blind_type_rejects_accidental_extra_fields(self) -> None:
        with self.assertRaises(TypeError):
            BlindTarget(
                round_id="round",
                blind_week="2026-01-01",
                item_id="1ABC",
                ligand_component_id="DRG",
                protein_uri="protein",
                pocket_uri="pocket",
                choices=(("pose", "uri"),),
                correct=True,  # type: ignore[call-arg]
            )

    def test_exact_scoring_caches_the_winning_overlay(self) -> None:
        target = ExactTarget(
            round_id="weekly-test-v1",
            blind_week="2026-01-01",
            item_id="1ABC",
            ligand_component_id="DRG",
            reference_uri="https://files.rcsb.org/download/1ABC.cif.gz",
            crystal_ligand_pdb="HETATM\n",
            has_correct_pose=True,
            correct_choice_ids=("pose-1",),
            automated_correct=(),
        )
        winner = object()
        result = {
            "classification": "familiar",
            "train_pdb": "2DEF",
            "train_het": "DRG",
            "train_shape_overlap": 0.75,
            "scorer_version": "foldseek-pdb100-carried-ligand-overlap/v7",
        }
        with (
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.download_rcsb_structure",
                return_value="/tmp/query.cif",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit._crystal_ligand_path",
                return_value="/tmp/ligand.pdb",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.ligand_cloud",
                return_value=object(),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.search_pre_cutoff",
                return_value=[],
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.collect_training_analogs",
                return_value=([winner], []),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.similarity_result_with_winner",
                return_value=(result, winner),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.cache_training_overlay",
                return_value={
                    "sha256": "a" * 64,
                    "size_bytes": 123,
                    "media_type": "chemical/x-pdb",
                },
            ) as cache_overlay,
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_path",
                return_value="/tmp/hits.json",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.file_sha256",
                return_value="b" * 64,
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_provenance",
                return_value={},
            ),
        ):
            scored = score_exact_target(target, "/tmp/cache")
        cache_overlay.assert_called_once_with(winner, mock.ANY)
        self.assertEqual(
            scored["training_system_overlay_cache"]["sha256"], "a" * 64
        )
        self.assertEqual(scored["training_system_overlay_status"], "available")
        self.assertIsNone(
            scored["training_system_overlay_unavailable_reason"]
        )

    def test_overlay_failure_does_not_change_a_complete_familiar_score(self) -> None:
        target = ExactTarget(
            round_id="weekly-test-v1",
            blind_week="2026-01-01",
            item_id="1ABC",
            ligand_component_id="DRG",
            reference_uri="https://files.rcsb.org/download/1ABC.cif.gz",
            crystal_ligand_pdb="HETATM\n",
            has_correct_pose=True,
            correct_choice_ids=("pose-1",),
            automated_correct=(),
        )
        winner = object()
        scientific_result = {
            "classification": "familiar",
            "reason": "training-ligand-overlap-at-least-0.25",
            "train_pdb": "2DEF",
            "train_het": "DRG",
            "train_shape_overlap": 0.75,
            "scorer_version": "foldseek-pdb100-carried-ligand-overlap/v7",
        }
        with (
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.download_rcsb_structure",
                return_value="/tmp/query.cif",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit._crystal_ligand_path",
                return_value="/tmp/ligand.pdb",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.ligand_cloud",
                return_value=object(),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.search_pre_cutoff",
                return_value=[],
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.collect_training_analogs",
                return_value=([winner], []),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.similarity_result_with_winner",
                return_value=(scientific_result, winner),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.cache_training_overlay",
                side_effect=TrainingSimilarityError("PDB chain limit exceeded"),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_path",
                return_value="/tmp/hits.json",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.file_sha256",
                return_value="b" * 64,
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_provenance",
                return_value={},
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.load_all_targets",
                return_value=([], [target]),
            ),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                audit = run_audit(
                    origin="https://www.foldarium.org",
                    cache_directory=Path(temporary) / "cache",
                    output=Path(temporary) / "exact.json",
                    mode="exact",
                )
            scored = audit["records"][0]
        self.assertEqual(scored["status"], "complete")
        self.assertEqual(scored["classification"], "familiar")
        self.assertEqual(scored["train_shape_overlap"], 0.75)
        self.assertEqual(scored["training_system_overlay_status"], "unavailable")
        self.assertIsNone(scored["training_system_overlay_cache"])
        self.assertIn(
            "PDB chain limit exceeded",
            scored["training_system_overlay_unavailable_reason"],
        )

    def test_exact_adds_rnp_result_without_changing_canonical_winner(self) -> None:
        target = ExactTarget(
            round_id="weekly-test-v1",
            blind_week="2026-01-01",
            item_id="1ABC",
            ligand_component_id="DRG",
            reference_uri="https://files.rcsb.org/download/1ABC.cif.gz",
            crystal_ligand_pdb="HETATM\n",
            has_correct_pose=True,
            correct_choice_ids=("pose-1",),
            automated_correct=(),
        )
        analogs = [object()]
        canonical = {
            "classification": "familiar",
            "train_pdb": "2DEF",
            "train_het": "CAN",
            "train_shape_overlap": 0.8,
            "scorer_version": "foldseek-pdb100-carried-ligand-overlap/v7",
        }
        rnp = _rnp_result(0.6, train_pdb="3GHI", train_het="RNP")
        with (
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.download_rcsb_structure",
                return_value="/tmp/query.cif",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit._crystal_ligand_path",
                return_value="/tmp/crystal-ligand.pdb",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.ligand_cloud",
                return_value=object(),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.search_pre_cutoff",
                return_value=[],
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.collect_training_analogs",
                return_value=(analogs, []),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.similarity_result_with_winner",
                return_value=(canonical, None),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.rnp_style_top25_similarity",
                return_value=rnp,
            ) as score_rnp,
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_path",
                return_value="/tmp/hits.json",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.file_sha256",
                return_value="a" * 64,
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_provenance",
                return_value={},
            ),
        ):
            scored = score_exact_target(target, "/tmp/cache")

        self.assertEqual(scored["train_pdb"], "2DEF")
        self.assertEqual(scored["train_het"], "CAN")
        self.assertEqual(scored["rnp_style_top25"], rnp)
        score_rnp.assert_called_once_with(
            "/tmp/query.cif",
            "/tmp/crystal-ligand.pdb",
            "DRG",
            analogs,
            ccd_cache_directory=Path("/tmp/cache"),
        )

    def test_blind_scores_each_pose_and_selects_maximum_rnp_score(self) -> None:
        target = BlindTarget(
            round_id="weekly-test-v1",
            blind_week="2026-01-01",
            item_id="1ABC",
            ligand_component_id="DRG",
            protein_uri="protein-uri",
            pocket_uri="pocket-uri",
            choices=(("pose-a", "pose-a-uri"), ("pose-b", "pose-b-uri")),
        )
        paths = {
            "protein-uri": Path("/tmp/blind-protein.pdb"),
            "pocket-uri": Path("/tmp/blind-pocket.pdb"),
            "pose-a-uri": Path("/tmp/pose-a.pdb"),
            "pose-b-uri": Path("/tmp/pose-b.pdb"),
        }
        canonical = {
            "classification": "familiar",
            "train_shape_overlap": 0.5,
        }
        rnp_a = _rnp_result(0.2, train_pdb="2AAA")
        rnp_b = _rnp_result(0.8, train_pdb="2BBB")
        with (
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.download_blind_asset",
                side_effect=lambda uri, _cache: paths[uri],
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.read_model",
                return_value=object(),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.atom_cloud",
                return_value=object(),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.ligand_cloud",
                return_value=object(),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.search_pre_cutoff",
                return_value=[],
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.collect_training_analogs",
                return_value=([], []),
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.similarity_result",
                return_value=canonical,
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.rnp_style_top25_similarity",
                side_effect=(rnp_a, rnp_b),
            ) as score_rnp,
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_path",
                return_value="/tmp/hits.json",
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.file_sha256",
                return_value="a" * 64,
            ),
            mock.patch(
                "foldarium_pipeline.weekly_training_audit.foldseek_cache_provenance",
                return_value={},
            ),
        ):
            scored = score_blind_target(target, "/tmp/cache")

        self.assertEqual(score_rnp.call_count, 2)
        self.assertEqual(
            [call.args[:3] for call in score_rnp.call_args_list],
            [
                (paths["protein-uri"], paths["pose-a-uri"], "DRG"),
                (paths["protein-uri"], paths["pose-b-uri"], "DRG"),
            ],
        )
        self.assertEqual(
            scored["choices"][0]["rnp_style_top25"]["train_pdb"], "2AAA"
        )
        self.assertEqual(scored["rnp_style_top25"]["choice_id"], "pose-b")
        self.assertEqual(scored["rnp_style_top25"]["score"], 0.8)
        self.assertEqual(scored["rnp_style_top25"]["classification"], "familiar")
        self.assertFalse(scored["rnp_style_top25"]["predict_none"])

    def test_resume_rescores_complete_old_rows_without_current_rnp(self) -> None:
        target = ExactTarget(
            round_id="weekly-test-v1",
            blind_week="2026-01-01",
            item_id="1ABC",
            ligand_component_id="DRG",
            reference_uri="https://files.rcsb.org/download/1ABC.cif.gz",
            crystal_ligand_pdb="HETATM\n",
            has_correct_pose=True,
            correct_choice_ids=("pose-1",),
            automated_correct=(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "exact.json"
            output.write_text(
                json.dumps(
                    {
                        "format_version": AUDIT_FORMAT,
                        "mode": "exact",
                        "scorer_version": (
                            "foldseek-pdb100-carried-ligand-overlap/v7"
                        ),
                        "generated_at": None,
                        "records": [
                            {
                                "mode": "exact",
                                "round_id": target.round_id,
                                "blind_week": target.blind_week,
                                "item_id": target.item_id,
                                "status": "complete",
                                "scorer_version": (
                                    "foldseek-pdb100-carried-ligand-overlap/v7"
                                ),
                                "train_pdb": None,
                                "training_system_overlay_status": (
                                    "not-applicable"
                                ),
                                "training_system_overlay_cache": None,
                                "training_system_overlay_unavailable_reason": None,
                            }
                        ],
                    }
                )
            )
            current = {
                "mode": "exact",
                "round_id": target.round_id,
                "blind_week": target.blind_week,
                "item_id": target.item_id,
                "scorer_version": (
                    "foldseek-pdb100-carried-ligand-overlap/v7"
                ),
                "rnp_style_top25": _rnp_result(0.5),
            }
            with (
                mock.patch(
                    "foldarium_pipeline.weekly_training_audit.load_all_targets",
                    return_value=([], [target]),
                ),
                mock.patch(
                    "foldarium_pipeline.weekly_training_audit.score_exact_target",
                    return_value=current,
                ) as score_exact,
            ):
                audit = run_audit(
                    origin="https://www.foldarium.org",
                    cache_directory=Path(temporary) / "cache",
                    output=output,
                    mode="exact",
                )

        score_exact.assert_called_once()
        self.assertEqual(
            audit["records"][0]["rnp_style_top25"]["version"],
            RNP_STYLE_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
