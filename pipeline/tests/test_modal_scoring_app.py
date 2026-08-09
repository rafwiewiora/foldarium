from __future__ import annotations

import hashlib
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


class ModalScoringAdapterTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        path = Path(__file__).resolve().parents[1] / "deploy" / "modal_scoring_app.py"
        spec = importlib.util.spec_from_file_location(
            "foldarium_test_modal_scoring_app", path
        )
        if spec is None or spec.loader is None:
            raise AssertionError("could not load Modal scoring adapter")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_is_separate_cpu_only_single_container_app(self) -> None:
        module = self.deployment_module()
        self.assertEqual(module.APP_NAME, "foldarium-weekly-scoring")
        self.assertEqual(module.SCORING_CPU, 1.0)
        self.assertEqual(module.SCORING_MEMORY_MIB, 2048)
        self.assertEqual(module.SCORING_TIMEOUT_SECONDS, 300)
        self.assertEqual(module.SCORING_MAX_CONTAINERS, 1)
        self.assertNotIn("GPU", module.__dict__)
        self.assertIn("@sha256:", module.SMINA_IMAGE_REF)
        self.assertTrue(
            module.SMINA_IMAGE_REF.endswith(
                "f76919d7c0d9f9a9b22e9bffe444dd611c9d8fef2f14e46d7b55e2276449334e"
            )
        )
        source = (
            Path(__file__).resolve().parents[1] / "deploy" / "modal_scoring_app.py"
        ).read_text()
        self.assertIn('"RUN cp \\"$(command -v smina)\\" /usr/local/bin/smina"', source)
        self.assertIn('"ENV PATH=/usr/local/bin:/usr/bin:/bin"', source)
        self.assertIn('"ENV LD_LIBRARY_PATH=/opt/conda/envs/smina/lib"', source)
        self.assertIn('"rdkit==2026.3.4"', source)
        self.assertIn('"prolif==2.2.0"', source)

    def test_payload_hashes_are_verified_before_scoring(self) -> None:
        module = self.deployment_module()
        protein = b"protein"
        ligand = b"ligand"
        with patch(
            "foldarium_pipeline.pose_scoring.score_pose_smina"
        ) as score, self.assertRaisesRegex(ValueError, "does not match"):
            module._score_payload(
                protein,
                ligand,
                "CC",
                "pose-1",
                "0" * 64,
                hashlib.sha256(ligand).hexdigest(),
            )
        score.assert_not_called()

    def test_payload_delegates_one_pair_without_credentials_or_publication(self) -> None:
        module = self.deployment_module()
        protein = b"protein"
        ligand = b"ligand"
        result = {
            "schema_version": "foldarium.pose-score/v1",
            "status": "succeeded",
            "scores": {"smina_affinity_kcal_mol": -5.0},
            "provenance": {},
        }
        with (
            patch(
                "foldarium_pipeline.pose_scoring.score_pose_smina",
                return_value=result,
            ) as score,
            patch(
                "foldarium_pipeline.interactions.calculate_interaction_summary_from_pose",
                return_value={"count": 7},
            ) as interactions,
        ):
            observed = module._score_payload(
                protein,
                ligand,
                "CC",
                "pose-1",
                hashlib.sha256(protein).hexdigest(),
                hashlib.sha256(ligand).hexdigest(),
            )

        self.assertEqual(observed, {
            "pose_id": "pose-1",
            **result,
            "interaction_summary": {"count": 7},
        })
        args, kwargs = score.call_args
        self.assertEqual(Path(args[0]).read_bytes() if Path(args[0]).exists() else protein, protein)
        self.assertEqual(kwargs["ligand_smiles"], "CC")
        self.assertEqual(kwargs["cpu"], 1)
        self.assertEqual(kwargs["timeout_seconds"], 120)
        self.assertEqual(kwargs["container_image"], module.SMINA_IMAGE_REF)
        interactions.assert_called_once()
        self.assertNotIn("SUPABASE", module.__dict__)


if __name__ == "__main__":
    unittest.main()
