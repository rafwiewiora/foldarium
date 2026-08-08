from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from foldarium_pipeline.methods.boltz2 import Boltz2Adapter
from foldarium_pipeline.methods.openfold3 import OpenFold3Adapter
from test_contracts import make_task


class MethodAdapterTests(unittest.TestCase):
    def test_openfold3_plan_is_safe_and_deterministic(self) -> None:
        task = make_task(
            "openfold3",
            {"checkpoint": "openfold3-p2-155k", "diffusion_samples": 5, "model_seeds": 1, "msa_mode": "server"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            plan = OpenFold3Adapter().plan(task, Path(temporary))
            payload = json.loads(plan.input_path.read_text())
            query = payload["queries"]["test-target"]
            self.assertEqual(query["chains"][0]["molecule_type"], "protein")
            self.assertEqual(plan.argv[:2], ("/bin/bash", "-lc"))
            self.assertEqual(plan.argv[3:5], ("foldarium-openfold3", "run_openfold"))
            self.assertIn("--query_json", plan.argv)
            self.assertIn("--output_dir", plan.argv)
            self.assertIn("--inference_ckpt_name", plan.argv)
            self.assertIn("--num_model_seeds", plan.argv)
            self.assertIn("--num_diffusion_samples", plan.argv)
            self.assertIn("--use_msa_server=True", plan.argv)
            self.assertNotIn("--query-json", plan.argv)
            self.assertNotIn("shell=True", plan.argv)

    def test_openfold3_plan_can_disable_msa_server(self) -> None:
        task = make_task("openfold3", {"msa_mode": "none"})
        with tempfile.TemporaryDirectory() as temporary:
            plan = OpenFold3Adapter().plan(task, Path(temporary))
            self.assertIn("--use_msa_server=False", plan.argv)

    def test_boltz2_plan_uses_json_compatible_yaml(self) -> None:
        task = make_task(
            "boltz2",
            {"diffusion_samples": 5, "max_parallel_samples": 1, "msa_mode": "empty", "seed": 9},
        )
        with tempfile.TemporaryDirectory() as temporary:
            plan = Boltz2Adapter().plan(task, Path(temporary))
            payload = json.loads(plan.input_path.read_text())
            protein = payload["sequences"][0]["protein"]
            self.assertEqual(protein["msa"], "empty")
            self.assertIn("--model", plan.argv)
            self.assertNotIn("--use_msa_server", plan.argv)

    def test_openfold3_collects_models_and_confidence(self) -> None:
        task = make_task("openfold3", {})
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            model = output / "test-target_seed_3_sample_1_model.cif"
            model.write_text("data_test\n")
            confidence = output / "test-target_seed_3_sample_1_confidences_aggregated.json"
            confidence.write_text('{"avg_pLDDT": 82.5, "ranking_score": 0.7}\n')
            samples = OpenFold3Adapter().collect(task, output)
            self.assertEqual(samples[0]["seed"], 3)
            self.assertEqual(samples[0]["sample_index"], 1)
            self.assertEqual(samples[0]["confidence"]["ranking_score"], 0.7)
            self.assertEqual(len(samples[0]["artifacts"][0]["sha256"]), 64)

    def test_boltz2_collects_nested_output(self) -> None:
        task = make_task("boltz2", {"seed": 4})
        with tempfile.TemporaryDirectory() as temporary:
            predictions = Path(temporary) / "boltz_results_target" / "predictions" / "target"
            predictions.mkdir(parents=True)
            (predictions / "target_model_0.cif").write_text("data_test\n")
            (predictions / "confidence_target_model_0.json").write_text('{"confidence_score": 0.9}\n')
            samples = Boltz2Adapter().collect(task, Path(temporary))
            self.assertEqual(samples[0]["seed"], 4)
            self.assertEqual(samples[0]["sample_index"], 0)
            self.assertEqual(samples[0]["confidence"]["confidence_score"], 0.9)


if __name__ == "__main__":
    unittest.main()
