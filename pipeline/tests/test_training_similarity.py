from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foldarium_pipeline.training_similarity import (
    AtomCloud,
    TrainingAnalog,
    TrainingSimilarityError,
    classify_similarity,
    import_local_foldseek_tsv,
    is_druglike_ligand,
    parse_foldseek_hits,
    search_pre_cutoff,
    similarity_result,
    volume_tanimoto,
)


class TrainingSimilarityPolicyTests(unittest.TestCase):
    def test_foldseek_hits_are_unique_self_excluded_and_strictly_pre_cutoff(self) -> None:
        result = {
            "results": [
                {
                    "alignments": [
                        [
                            {
                                "target": "pdb|1ABC_A",
                                "seqId": 52.0,
                                "qAln": "AAAA",
                                "dbAln": "AAAA",
                                "tCa": "0,0,0,1,0,0,2,0,0,3,0,0",
                            },
                            {"target": "1ABC_B", "seqId": 51.0},
                            {"target": "2DEF_A", "seqId": 40.0},
                            {"target": "3GHI_A", "seqId": 30.0},
                            {"target": "9XYZ_A", "seqId": 99.0},
                        ]
                    ]
                }
            ]
        }
        hits = parse_foldseek_hits(
            result,
            exclude_pdb="9XYZ",
            release_dates={
                "1ABC": "2021-09-29",
                "2DEF": "2021-09-30",
                "3GHI": "2022-01-01",
            },
        )

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["pdb"], "1ABC")
        self.assertEqual(hits[0]["identity"], 0.52)

    def test_invalid_foldseek_shape_is_an_error_not_an_empty_result(self) -> None:
        with self.assertRaisesRegex(TrainingSimilarityError, "alignment table"):
            parse_foldseek_hits(
                {"results": []},
                exclude_pdb="9XYZ",
                release_dates={},
            )

    def test_missing_release_date_is_unknown_not_silently_excluded(self) -> None:
        result = {
            "results": [
                {"alignments": [[{"target": "1ABC_A", "seqId": 40.0}]]}
            ]
        }
        with self.assertRaisesRegex(TrainingSimilarityError, "release date"):
            parse_foldseek_hits(
                result,
                exclude_pdb="9XYZ",
                release_dates={"1ABC": None},
            )

    def test_classification_is_fail_closed(self) -> None:
        self.assertEqual(
            classify_similarity(0.5, failures=[{"rank": 2}], hit_count=3)[0],
            "familiar",
        )
        self.assertEqual(
            classify_similarity(0.1, failures=[{"rank": 2}], hit_count=3)[0],
            "unknown",
        )
        self.assertEqual(
            classify_similarity(0.1, failures=[], hit_count=3)[0],
            "novel",
        )
        self.assertEqual(
            classify_similarity(None, failures=[], hit_count=0),
            ("novel", "confirmed-empty-pre-cutoff-foldseek-result"),
        )

    def test_ligand_filter_excludes_common_additives(self) -> None:
        self.assertTrue(is_druglike_ligand("R16"))
        self.assertFalse(is_druglike_ligand("HOH"))
        self.assertFalse(is_druglike_ligand("ATP"))
        self.assertFalse(is_druglike_ligand("X"))

    def test_local_foldseek_tsv_seeds_api_compatible_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "queries": [
                            {
                                "item_id": "1ABC",
                                "cache_label": "exact",
                                "source_sha256": "a" * 64,
                            }
                        ],
                        "database_provenance": {"foldseek_version": "test"},
                    }
                )
            )
            result = root / "result.tsv"
            result.write_text(
                "1ABC_A\t2DEF_A\t0.5\t1\t2\tAAAA\tAAAA\t"
                "0,0,0,1,0,0,2,0,0,3,0,0\n"
            )
            with mock.patch(
                "foldarium_pipeline.training_similarity.foldseek.release_dates",
                return_value={"2DEF": "2020-01-01"},
            ):
                counts = import_local_foldseek_tsv(
                    result, manifest, root / "cache"
                )
            self.assertEqual(counts["hit_count"], 1)
            cache = json.loads(
                (
                    root
                    / "cache"
                    / "foldseek-hits"
                    / f"exact-1ABC-{'a' * 16}.json"
                ).read_text()
            )
            self.assertEqual(cache["backend"], "local-foldseek-batch")
            self.assertEqual(cache["hits"][0]["identity"], 0.5)


try:
    import numpy
except ImportError:  # pragma: no cover - exercised by dependency-light CI
    numpy = None


@unittest.skipIf(numpy is None, "numpy evaluation extra is unavailable")
class TrainingSimilarityGeometryTests(unittest.TestCase):
    def test_volume_tanimoto_handles_identical_and_disjoint_clouds(self) -> None:
        cloud = AtomCloud(
            positions=numpy.asarray([[0.0, 0.0, 0.0], [1.4, 0.0, 0.0]]),
            radii=numpy.asarray([1.7, 1.7]),
        )
        disjoint = AtomCloud(
            positions=numpy.asarray([[100.0, 0.0, 0.0]]),
            radii=numpy.asarray([1.7]),
        )
        self.assertEqual(volume_tanimoto(cloud, cloud), 1.0)
        self.assertEqual(volume_tanimoto(cloud, disjoint), 0.0)

    def test_result_records_nearest_analog_and_policy(self) -> None:
        query = AtomCloud(
            positions=numpy.asarray([[0.0, 0.0, 0.0]]),
            radii=numpy.asarray([1.7]),
        )
        analog = TrainingAnalog(
            pdb_id="1ABC",
            ligand="DRG",
            identity=0.42,
            local_rmsd=0.8,
            local_residue_count=7,
            hit_rank=1,
            cloud=query,
        )
        result = similarity_result(
            query,
            [analog],
            [],
            [{"pdb": "1ABC", "identity": 0.42}],
        )
        self.assertEqual(result["classification"], "familiar")
        self.assertEqual(result["train_pdb"], "1ABC")
        self.assertEqual(result["train_shape_overlap"], 1.0)
        self.assertEqual(result["train_local_residue_count"], 7)

    def test_timed_out_foldseek_ticket_is_resumed_without_resubmission(self) -> None:
        lines = [
            (
                f"ATOM  {index:5d}  CA  ALA A{index:4d}    "
                f"{float(index):8.3f}{0.0:8.3f}{0.0:8.3f}"
                "  1.00 20.00           C"
            )
            for index in range(1, 7)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            query = root / "query.pdb"
            query.write_text("\n".join(lines + ["END", ""]))
            result = {"results": [{"alignments": [[]]}]}
            with (
                mock.patch(
                    "foldarium_pipeline.training_similarity.foldseek.submit",
                    return_value=("ticket-1", "PENDING"),
                ) as submit,
                mock.patch(
                    "foldarium_pipeline.training_similarity.foldseek.poll",
                    side_effect=["TIMEOUT", "COMPLETE"],
                ),
                mock.patch(
                    "foldarium_pipeline.training_similarity.foldseek.fetch_result",
                    return_value=result,
                ),
            ):
                with self.assertRaisesRegex(TrainingSimilarityError, "TIMEOUT"):
                    search_pre_cutoff(
                        query,
                        exclude_pdb="9XYZ",
                        cache_directory=root / "cache",
                        cache_label="test",
                    )
                self.assertEqual(
                    search_pre_cutoff(
                        query,
                        exclude_pdb="9XYZ",
                        cache_directory=root / "cache",
                        cache_label="test",
                    ),
                    [],
                )
                self.assertEqual(submit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
