from __future__ import annotations

import unittest

from foldarium_pipeline.contracts import (
    ContractError,
    SCHEMA_VERSION,
    make_prediction_task,
    validate_prediction_task,
    validate_target,
)


TARGET = {
    "schema_version": SCHEMA_VERSION,
    "target_id": "test-target",
    "entities": [
        {"type": "protein", "chain_ids": ["A"], "sequence": "ACDEFGHIK"},
        {"type": "ligand", "chain_ids": ["L"], "smiles": "CCO"},
    ],
}


def make_task(method: str, config: dict) -> dict:
    return make_prediction_task(
        campaign_id="test-campaign",
        target=TARGET,
        method=method,
        method_version="test-version",
        container_image="registry.example/foldarium/test@sha256:" + "a" * 64,
        config=config,
        output_uri_prefix="s3://foldarium-test/predictions",
        resources={"timeout_seconds": 60},
    )


class ContractTests(unittest.TestCase):
    def test_task_identity_is_deterministic(self) -> None:
        first = make_task("boltz2", {"seed": 7})
        second = make_task("boltz2", {"seed": 7})
        self.assertEqual(first, second)
        self.assertEqual(validate_prediction_task(first), first)
        self.assertTrue(first["output_uri_prefix"].endswith(first["task_id"]))

    def test_config_change_changes_task_identity(self) -> None:
        first = make_task("boltz2", {"seed": 7})
        second = make_task("boltz2", {"seed": 8})
        self.assertNotEqual(first["task_id"], second["task_id"])

    def test_rejects_duplicate_chain_ids(self) -> None:
        bad = {**TARGET, "entities": [TARGET["entities"][0], {**TARGET["entities"][1], "chain_ids": ["A"]}]}
        with self.assertRaisesRegex(ContractError, "unique"):
            validate_target(bad)

    def test_rejects_task_id_tampering(self) -> None:
        task = make_task("openfold3", {})
        task["task_id"] = "run_wrong"
        with self.assertRaisesRegex(ContractError, "task_id"):
            validate_prediction_task(task)


if __name__ == "__main__":
    unittest.main()
