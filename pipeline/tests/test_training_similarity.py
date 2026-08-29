from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

from foldarium_pipeline import training_similarity
from foldarium_pipeline.training_similarity import (
    AtomCloud,
    TrainingAnalog,
    TrainingSimilarityError,
    cache_training_overlay,
    classify_similarity,
    import_local_foldseek_tsv,
    is_druglike_ligand,
    materialize_training_overlay,
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
        self.assertEqual(hits[0]["target"], "pdb|1ABC_A")
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
    @staticmethod
    def _overlay_analog(
        root: Path, *, ligand_in_polymer: bool = False
    ) -> TrainingAnalog:
        import gemmi

        structure = gemmi.Structure()
        structure.name = "source"
        model = gemmi.Model("1")
        ligand_name = "MLE" if ligand_in_polymer else "DRG"
        ligand_residue_index = 1 if ligand_in_polymer else 2
        for chain_name, offset in (("AA", 0.0), ("longB", 10.0)):
            chain = gemmi.Chain(chain_name)
            residue_numbers = (
                range(1, 4)
                if chain_name == "AA" and ligand_in_polymer
                else range(1, 3)
            )
            for index in residue_numbers:
                if chain_name == "AA" and ligand_in_polymer and index == 2:
                    ligand = gemmi.Residue()
                    ligand.name = ligand_name
                    ligand.het_flag = "H"
                    ligand.seqid = gemmi.SeqId(index, " ")
                    for name, element, position in (
                        ("C1", "C", (2.0, 3.0, 4.0)),
                        ("O1", "O", (3.0, 3.0, 4.0)),
                    ):
                        atom = gemmi.Atom()
                        atom.name = name
                        atom.element = gemmi.Element(element)
                        atom.pos = gemmi.Position(*position)
                        ligand.add_atom(atom)
                    chain.add_residue(ligand)
                    continue
                residue = gemmi.Residue()
                residue.name = "ALA"
                residue.seqid = gemmi.SeqId(index, " ")
                atom = gemmi.Atom()
                atom.name = "CA"
                atom.element = gemmi.Element("C")
                atom.pos = gemmi.Position(offset + index, 0.0, 0.0)
                residue.add_atom(atom)
                chain.add_residue(residue)
            if chain_name == "AA" and not ligand_in_polymer:
                ligand = gemmi.Residue()
                ligand.name = ligand_name
                ligand.het_flag = "H"
                ligand.seqid = gemmi.SeqId(9, " ")
                for name, element, position in (
                    ("C1", "C", (2.0, 3.0, 4.0)),
                    ("O1", "O", (3.0, 3.0, 4.0)),
                ):
                    atom = gemmi.Atom()
                    atom.name = name
                    atom.element = gemmi.Element(element)
                    atom.pos = gemmi.Position(*position)
                    ligand.add_atom(atom)
                chain.add_residue(ligand)
            model.add_chain(chain)
        structure.add_model(model)
        source = root / "source.cif"
        structure.make_mmcif_document().write_file(str(source))
        rotation = numpy.asarray(
            [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
        )
        translation = numpy.asarray([5.123456789, -2.987654321, 1.111111111])
        ligand_positions = numpy.asarray([[2.0, 3.0, 4.0], [3.0, 3.0, 4.0]])
        transformed = (rotation @ ligand_positions.T).T + translation
        return TrainingAnalog(
            pdb_id="1ABC",
            ligand=ligand_name,
            identity=0.42,
            local_rmsd=0.8,
            local_residue_count=7,
            hit_rank=1,
            cloud=AtomCloud(
                positions=transformed,
                radii=numpy.asarray([1.7, 1.52]),
            ),
            _source_structure=str(source),
            _source_structure_sha256=sha256(source.read_bytes()).hexdigest(),
            _ligand_chain_index=0,
            _ligand_residue_index=ligand_residue_index,
            _rotation=rotation,
            _translation=translation,
        )

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

    def test_pdb100_assembly_copy_identifier_selects_base_chain(self) -> None:
        import gemmi

        structure = gemmi.Structure()
        model = gemmi.Model("1")
        for chain_name in ("A", "B"):
            chain = gemmi.Chain(chain_name)
            for index in range(1, 3):
                residue = gemmi.Residue()
                residue.name = "ALA"
                residue.seqid = gemmi.SeqId(index, " ")
                atom = gemmi.Atom()
                atom.name = "CA"
                atom.element = gemmi.Element("C")
                atom.pos = gemmi.Position(float(index), 0.0, 0.0)
                residue.add_atom(atom)
                chain.add_residue(residue)
            model.add_chain(chain)
        structure.add_model(model)
        structure.setup_entities()

        provenance = training_similarity._target_polymer_provenance(
            structure[0],
            {
                "target": "5om0-assembly2_A-2",
                "qAln": "AA",
                "dbAln": "AA",
            },
            2,
        )

        self.assertEqual(provenance, ((0, 1), 0, (0, 1)))

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

    def test_overlay_contains_all_polymers_and_only_the_scored_ligand(self) -> None:
        import gemmi

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analog = self._overlay_analog(root)
            content = materialize_training_overlay(analog)
            overlay = gemmi.read_pdb_string(content.decode("utf-8"))
            overlay.setup_entities()
            polymer_chains = [
                chain for chain in overlay[0] if len(chain.get_polymer()) > 0
            ]
            ligands = [
                residue
                for chain in overlay[0]
                for residue in chain
                if residue.het_flag == "H"
            ]
            self.assertEqual(len(polymer_chains), 2)
            self.assertEqual([chain.name for chain in polymer_chains], ["A", "B"])
            self.assertEqual([residue.name for residue in ligands], ["DRG"])
            coordinate_lines = [
                line
                for line in content.decode("ascii").splitlines()
                if line.startswith(("ATOM  ", "HETATM"))
            ]
            self.assertTrue(
                all(
                    len(line) >= 80 and line[21] in {"A", "B"}
                    for line in coordinate_lines
                )
            )
            cached = cache_training_overlay(analog, root / "cache")
            path = (
                root
                / "cache"
                / "training-overlays"
                / "sha256"
                / cached["sha256"][:2]
                / f"{cached['sha256']}.pdb"
            )
            self.assertEqual(path.read_bytes(), content)

    def test_overlay_does_not_duplicate_scored_het_in_polymer_span(self) -> None:
        import gemmi

        with tempfile.TemporaryDirectory() as temporary:
            analog = self._overlay_analog(
                Path(temporary), ligand_in_polymer=True
            )
            source = gemmi.read_structure(analog._source_structure)
            source.setup_entities()
            scored_residue = source[0][analog._ligand_chain_index][
                analog._ligand_residue_index
            ]
            self.assertIn(scored_residue, list(source[0][0].get_polymer()))

            content = materialize_training_overlay(analog)
            overlay = gemmi.read_pdb_string(content.decode("ascii"))
            matching = [
                residue
                for chain in overlay[0]
                for residue in chain
                if residue.het_flag == "H" and residue.name == "MLE"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(
                sum(len(chain) for chain in overlay[0]),
                5,
            )

    def test_overlay_rejects_a_ligand_cloud_different_from_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            analog = self._overlay_analog(Path(temporary))
            changed = TrainingAnalog(
                **{
                    **analog.__dict__,
                    "cloud": AtomCloud(
                        analog.cloud.positions + 1.0,
                        analog.cloud.radii,
                    ),
                }
            )
            with self.assertRaisesRegex(TrainingSimilarityError, "scored cloud"):
                materialize_training_overlay(changed)

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
