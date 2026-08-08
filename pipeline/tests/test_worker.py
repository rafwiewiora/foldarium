from __future__ import annotations

import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from foldarium_pipeline.worker import execute_task_json
from test_contracts import make_task


class WorkerTests(unittest.TestCase):
    def test_dry_run_needs_no_gpu_package(self) -> None:
        task = make_task("boltz2", {"msa_mode": "empty", "seed": 0})
        with tempfile.TemporaryDirectory() as temporary:
            result = execute_task_json(task, temporary, dry_run=True)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["method"], "boltz2")
        self.assertEqual(result["plan"]["argv"][0], "boltz")
        self.assertEqual(result["plan"]["environment_keys"], ["BOLTZ_CACHE"])

    def test_missing_model_runtime_becomes_a_publishable_failure(self) -> None:
        task = make_task("boltz2", {"msa_mode": "empty", "seed": 0})
        with tempfile.TemporaryDirectory() as temporary:
            with patch("foldarium_pipeline.worker.subprocess.run", side_effect=FileNotFoundError):
                result = execute_task_json(task, temporary, dry_run=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "launch_failed")
        self.assertNotIn("argv", result)

    def test_cuda_oom_becomes_an_actionable_failure_code(self) -> None:
        task = make_task("boltz2", {"msa_mode": "empty", "seed": 0})
        completed = CompletedProcess([], 1, stdout="", stderr="CUDA out of memory")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("foldarium_pipeline.worker._GpuMemorySampler"):
                with patch("foldarium_pipeline.worker.subprocess.run", return_value=completed):
                    result = execute_task_json(task, temporary, dry_run=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "gpu_out_of_memory")


if __name__ == "__main__":
    unittest.main()
