from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from foldarium_pipeline.contracts import validate_prediction_task
from foldarium_pipeline.intake import (
    IntakeError,
    WeeklyPolicy,
    build_weekly_plan,
    parse_wwpdb_snapshot,
    target_from_cameo,
    target_from_wwpdb,
)
from foldarium_pipeline.selection import select_ligand


SEQUENCE_TSV = (
    "PDB_ID\tSequence_Count\tSequence\n"
    "36IQ\t1\tMEKEIVEEALKLVQGFLDDPNDKAVLEAAAAFWANPENRKVVTDTIAKELGISSEELEARWREYDAAGRLAEANEIVAKGLRKALENLYFQSHHHHHH\n"
).encode()

NONPOLYMER_TSV = (
    "PDB_ID\tComponent_ID\tInChI\tSMILES string\n"
    "36IQ\tDM2\tInChI=fixture\tCCCCCCCCCCCCCCCC\n"
).encode()


def cameo_payload(target_id: str = "2026-06-20_00000082", *, component: str = "DM2") -> dict:
    return {
        "target": {
            "id": target_id,
            "week_id": target_id[:10],
            "pdbid": None,
            "labels_submission_3d": "hard",
        },
        "entities": [
            {
                "id": target_id + "_1",
                "entity_type": "protein",
                "canonical_sequence": "M" + "A" * 97,
            },
            {
                "id": target_id + "_np_1",
                "entity_type": "non_polymer",
                "component_id": component,
                "smiles": "CCCCCCCCCCCCCCCC",
                "inchi": "InChI=fixture",
            },
        ],
        "biounits": [],
        "predictions": [],
    }


class SelectionTests(unittest.TestCase):
    def test_heavy_atom_filter_and_largest_ligand(self) -> None:
        selected = select_ligand(
            [
                {"component_id": "SM1", "smiles": "CCCC"},
                {"component_id": "BIG", "smiles": "C" * 16},
                {"component_id": "GOL", "smiles": "C" * 30},
            ]
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["component_id"], "BIG")
        self.assertEqual(selected["heavy_atoms"], 16)

    def test_tep_is_used_only_without_an_alternative(self) -> None:
        tep = {"component_id": "TEP", "smiles": "C" * 20}
        other = {"component_id": "DRG", "smiles": "C" * 15}
        self.assertEqual(select_ligand([tep, other])["component_id"], "DRG")
        self.assertEqual(select_ligand([tep])["component_id"], "TEP")


class WwPdbTests(unittest.TestCase):
    def test_snapshot_records_hashes_and_counts(self) -> None:
        snapshot = parse_wwpdb_snapshot(SEQUENCE_TSV, NONPOLYMER_TSV)
        self.assertEqual(snapshot["entry_count"], 1)
        self.assertEqual(snapshot["sequence_rows"], 1)
        self.assertEqual(snapshot["nonpolymer_rows"], 1)
        self.assertEqual(len(snapshot["sequence_sha256"]), 64)

    def test_official_headerless_canonical_sequence_format(self) -> None:
        headerless = SEQUENCE_TSV.split(b"\n", 1)[1]
        snapshot = parse_wwpdb_snapshot(headerless, NONPOLYMER_TSV)
        self.assertEqual(snapshot["sequence_rows"], 1)
        self.assertIn("36IQ", snapshot["entries"])

    def test_noncanonical_sequence_is_rejected(self) -> None:
        broken = SEQUENCE_TSV.replace(b"MEK", b"M(EK)")
        with self.assertRaises(IntakeError):
            parse_wwpdb_snapshot(broken, NONPOLYMER_TSV)


class TargetTests(unittest.TestCase):
    def test_wwpdb_target_does_not_wait_for_cameo(self) -> None:
        snapshot = parse_wwpdb_snapshot(SEQUENCE_TSV, NONPOLYMER_TSV)
        target = target_from_wwpdb(
            "36IQ", snapshot["entries"]["36IQ"], date(2026, 6, 20), WeeklyPolicy()
        )
        self.assertEqual(target["target_id"], "36IQ")
        self.assertEqual(target["source"]["kind"], "wwpdb-prerelease")
        self.assertEqual(target["metadata"]["selected_ligand"]["heavy_atoms"], 16)

    def test_wwpdb_target_rejects_ambiguous_nucleic_sequence(self) -> None:
        entry = {
            "sequences": ["ACGT" * 20],
            "ligands": [{"component_id": "DRG", "smiles": "C" * 16}],
        }
        with self.assertRaisesRegex(IntakeError, "ambiguous-or-nucleic"):
            target_from_wwpdb("1ABC", entry, date(2026, 6, 20), WeeklyPolicy())

    def test_cameo_target_records_unknown_stoichiometry_policy(self) -> None:
        target = target_from_cameo(cameo_payload(), WeeklyPolicy())
        self.assertIsNotNone(target)
        self.assertEqual(target["metadata"]["stoichiometry_policy"], "one-copy-per-distinct-prerelease-entity/v1")
        self.assertEqual(target["metadata"]["selected_ligand"]["heavy_atoms"], 16)
        self.assertEqual([entity["chain_ids"] for entity in target["entities"]], [["A"], ["B"]])

    def test_duplicate_polymer_entities_are_one_unknown_copy(self) -> None:
        source = cameo_payload()
        source["entities"].insert(1, dict(source["entities"][0], id="duplicate"))
        target = target_from_cameo(source, WeeklyPolicy())
        self.assertEqual(len(target["entities"]), 2)

    def test_target_without_eligible_ligand_is_skipped(self) -> None:
        self.assertIsNone(target_from_cameo(cameo_payload(component="GOL"), WeeklyPolicy()))


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = parse_wwpdb_snapshot(SEQUENCE_TSV, NONPOLYMER_TSV)
        self.generated = datetime(2026, 6, 20, 6, tzinfo=timezone.utc)

    def build(self, payloads: list[dict], cap: int = 8) -> dict:
        return build_weekly_plan(
            release_date=date(2026, 6, 20),
            ww_pdb_snapshot=self.snapshot,
            cameo_payloads=payloads,
            output_prefix="supabase://foldarium-predictions/runs",
            policy=WeeklyPolicy(max_targets=cap),
            generated_at=self.generated,
        )

    def test_one_target_produces_one_task_per_method(self) -> None:
        plan = self.build([cameo_payload()])
        self.assertEqual(plan["campaign"]["campaign_id"], "wwpdb-2026-06-20")
        self.assertEqual(plan["budget"]["selected_targets"], 1)
        self.assertEqual(plan["budget"]["gpu_tasks"], 2)
        self.assertEqual({task["method"] for task in plan["tasks"]}, {"openfold3", "boltz2"})
        self.assertTrue(plan["campaign"]["configuration"]["protein_only"])
        for task in plan["tasks"]:
            self.assertEqual(validate_prediction_task(task), task)
            self.assertEqual(task["resources"]["gpu_class"], "l4")

    def test_wwpdb_only_plan_uses_the_saturday_snapshot(self) -> None:
        plan = build_weekly_plan(
            release_date=date(2026, 6, 20),
            ww_pdb_snapshot=self.snapshot,
            output_prefix="supabase://foldarium-predictions/runs",
            policy=WeeklyPolicy(max_targets=1),
            generated_at=self.generated,
        )
        self.assertEqual([target["target_id"] for target in plan["targets"]], ["36IQ"])
        self.assertEqual(plan["campaign"]["configuration"]["intake_source"], "wwpdb-prerelease")
        self.assertEqual(plan["budget"]["gpu_tasks"], 2)

    def test_operator_can_pin_weekly_tasks_to_l4(self) -> None:
        plan = build_weekly_plan(
            release_date=date(2026, 6, 20),
            ww_pdb_snapshot=self.snapshot,
            output_prefix="supabase://foldarium-predictions/runs",
            policy=WeeklyPolicy(max_targets=1, gpu_class="l4"),
            generated_at=self.generated,
        )
        self.assertEqual({task["resources"]["gpu_class"] for task in plan["tasks"]}, {"l4"})
        self.assertEqual(plan["campaign"]["configuration"]["gpu_class_override"], "l4")

    def test_plan_is_replay_deterministic(self) -> None:
        first = self.build([cameo_payload()])
        second = self.build([cameo_payload()])
        self.assertEqual(first, second)

    def test_plan_identity_excludes_retry_timestamp(self) -> None:
        first = self.build([cameo_payload()])
        second = build_weekly_plan(
            release_date=date(2026, 6, 20),
            ww_pdb_snapshot=self.snapshot,
            cameo_payloads=[cameo_payload()],
            output_prefix="supabase://foldarium-predictions/runs",
            generated_at=datetime(2026, 6, 20, 7, tzinfo=timezone.utc),
        )
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])

    def test_cap_is_enforced_before_tasks_are_created(self) -> None:
        first = cameo_payload("2026-06-20_00000001")
        second = cameo_payload("2026-06-20_00000002")
        second["entities"][0]["canonical_sequence"] = "M" + "G" * 97
        plan = self.build([first, second], cap=1)
        self.assertEqual(len(plan["targets"]), 1)
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertIn("weekly-target-cap", {row["reason"] for row in plan["skipped"]})

    def test_only_one_ligand_target_per_polymer_set_is_selected(self) -> None:
        first = cameo_payload("2026-06-20_00000001")
        second = cameo_payload("2026-06-20_00000002")
        plan = self.build([first, second], cap=8)
        self.assertEqual(len(plan["targets"]), 1)
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertIn("duplicate-polymer-complex", {row["reason"] for row in plan["skipped"]})

    def test_wrong_week_never_creates_a_task(self) -> None:
        plan = self.build([cameo_payload("2026-06-13_00000001")])
        self.assertEqual(plan["tasks"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "wrong-release-week")

    def test_first_run_rejects_targets_the_evaluator_cannot_score(self) -> None:
        source = cameo_payload()
        source["entities"][0]["entity_type"] = "rna"
        source["entities"][0]["canonical_sequence"] = "A" * 98
        plan = self.build([source])
        self.assertEqual(plan["tasks"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "unsupported-nonprotein-polymer")

    def test_nonprotein_targets_can_be_enabled_explicitly_for_future_adapters(self) -> None:
        source = cameo_payload()
        source["entities"][0]["entity_type"] = "rna"
        source["entities"][0]["canonical_sequence"] = "A" * 98
        plan = build_weekly_plan(
            release_date=date(2026, 6, 20),
            ww_pdb_snapshot=self.snapshot,
            cameo_payloads=[source],
            output_prefix="supabase://foldarium-predictions/runs",
            policy=WeeklyPolicy(protein_only=False),
            generated_at=self.generated,
        )
        self.assertEqual(len(plan["tasks"]), 2)


if __name__ == "__main__":
    unittest.main()
