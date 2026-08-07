from __future__ import annotations

import unittest

from foldarium_pipeline.contracts import SCHEMA_VERSION
from foldarium_pipeline.sizing import (
    GPU_LADDER,
    SizingError,
    count_tokens,
    derive_gpu_class,
    effective_tokens,
    resolve_gpu_class,
    validate_gpu_class,
)


def target(entities: list[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "target_id": "sizing-test",
        "entities": entities,
    }


SMALL = target(
    [
        {"type": "protein", "chain_ids": ["A"], "sequence": "A" * 29},
        {"type": "ligand", "chain_ids": ["L"], "smiles": "CCO"},
    ]
)


class TokenCountTests(unittest.TestCase):
    def test_polymer_and_ligand_are_both_counted(self) -> None:
        # 29 residues + ethanol's three heavy atoms.
        self.assertEqual(count_tokens(SMALL), 32)

    def test_each_chain_copy_counts_separately(self) -> None:
        one = target([{"type": "protein", "chain_ids": ["A"], "sequence": "A" * 100}])
        two = target(
            [{"type": "protein", "chain_ids": ["A", "B"], "sequence": "A" * 100}]
        )
        self.assertEqual(count_tokens(one), 100)
        self.assertEqual(count_tokens(two), 200)

    def test_bracket_and_two_letter_atoms_are_counted(self) -> None:
        ligand = target(
            [
                {"type": "protein", "chain_ids": ["A"], "sequence": "A"},
                {"type": "ligand", "chain_ids": ["L"], "smiles": "ClCBr[Se]c1ccccc1"},
            ]
        )
        # Cl, C, Br, [Se], then six aromatic carbons.
        self.assertEqual(count_tokens(ligand) - 1, 10)

    def test_ccd_ligands_use_a_conservative_estimate(self) -> None:
        ccd = target(
            [
                {"type": "protein", "chain_ids": ["A"], "sequence": "A" * 10},
                {"type": "ligand", "chain_ids": ["L"], "ccd_codes": ["ATP"]},
            ]
        )
        self.assertEqual(count_tokens(ccd), 40)


class EffectiveTokenTests(unittest.TestCase):
    def test_absent_msa_is_not_inflated(self) -> None:
        self.assertEqual(effective_tokens(SMALL, {"msa_mode": "none"}), 32)
        self.assertEqual(effective_tokens(SMALL, {"msa_mode": "empty"}), 32)

    def test_a_real_msa_raises_the_estimate(self) -> None:
        self.assertGreater(
            effective_tokens(SMALL, {"msa_mode": "server"}),
            effective_tokens(SMALL, {"msa_mode": "none"}),
        )

    def test_msa_is_assumed_when_unspecified(self) -> None:
        # The method adapters default to a server MSA, so sizing must not assume
        # the cheaper case when the config is silent.
        self.assertEqual(
            effective_tokens(SMALL, {}), effective_tokens(SMALL, {"msa_mode": "server"})
        )

    def test_parallel_samples_scale_the_estimate(self) -> None:
        base = effective_tokens(SMALL, {"msa_mode": "none"})
        self.assertEqual(
            effective_tokens(SMALL, {"msa_mode": "none", "max_parallel_samples": 4}),
            base * 4,
        )

    def test_invalid_parallel_samples_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            effective_tokens(SMALL, {"max_parallel_samples": 0})


class LadderTests(unittest.TestCase):
    def test_ladder_is_ordered_by_capacity(self) -> None:
        ceilings = [gpu.max_tokens for gpu in GPU_LADDER]
        self.assertEqual(ceilings, sorted(ceilings))
        self.assertEqual(len(set(gpu.name for gpu in GPU_LADDER)), len(GPU_LADDER))

    def test_small_target_gets_the_smallest_class(self) -> None:
        self.assertEqual(derive_gpu_class(SMALL, {"msa_mode": "none"}), "l4")

    def test_larger_targets_escalate(self) -> None:
        big = target([{"type": "protein", "chain_ids": ["A"], "sequence": "A" * 900}])
        self.assertEqual(derive_gpu_class(big, {"msa_mode": "none"}), "l40s")

    def test_oversized_target_fails_at_planning_time(self) -> None:
        # Failing here is the point: an out-of-memory failure would only be
        # discovered after the GPU had already been paid for.
        huge = target([{"type": "protein", "chain_ids": ["A"], "sequence": "A" * 9000}])
        with self.assertRaises(SizingError):
            derive_gpu_class(huge, {"msa_mode": "none"})


class ResolutionTests(unittest.TestCase):
    def test_explicit_choice_beats_the_heuristic(self) -> None:
        self.assertEqual(
            resolve_gpu_class(SMALL, {"msa_mode": "none"}, "a100-80gb"), "a100-80gb"
        )

    def test_explicit_choice_is_validated(self) -> None:
        with self.assertRaises(SizingError):
            resolve_gpu_class(SMALL, {}, "rtx-4090")

    def test_explicit_choice_may_be_smaller_than_derived(self) -> None:
        # Pinning down is allowed: an operator with real measurements outranks a
        # provisional table.
        big = target([{"type": "protein", "chain_ids": ["A"], "sequence": "A" * 900}])
        self.assertEqual(resolve_gpu_class(big, {"msa_mode": "none"}, "l4"), "l4")

    def test_validate_rejects_non_strings(self) -> None:
        with self.assertRaises(SizingError):
            validate_gpu_class(40)


class IdentityTests(unittest.TestCase):
    def test_gpu_class_does_not_change_the_run_id(self) -> None:
        from foldarium_pipeline.contracts import make_prediction_task

        def build(resources: dict) -> str:
            return make_prediction_task(
                campaign_id="c",
                target=SMALL,
                method="boltz2",
                method_version="2.2.1",
                container_image="registry.example/x@sha256:" + "a" * 64,
                config={"seed": 0},
                output_uri_prefix="s3://bucket/runs",
                resources=resources,
            )["task_id"]

        self.assertEqual(
            build({"timeout_seconds": 900, "gpu_class": "l4"}),
            build({"timeout_seconds": 900, "gpu_class": "a100-80gb"}),
        )


if __name__ == "__main__":
    unittest.main()
