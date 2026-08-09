from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from foldarium_pipeline.pose_scoring import (
    PoseScoringError,
    SMINA_SCORE_PROTOCOL_VERSION,
    SMINA_SCORE_SCHEMA_VERSION,
    score_pose_smina,
)


class PoseScoringTests(unittest.TestCase):
    def fixture_paths(self, root: Path) -> tuple[Path, Path, Path]:
        binary = root / "smina"
        binary.write_bytes(b"pinned-smina-fixture")
        receptor = root / "protein.pdb"
        receptor.write_text(
            "ATOM      1  C   ALA A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n",
            encoding="utf-8",
        )
        ligand = root / "pose.sdf"
        ligand.write_text("fixture\n$$$$\n", encoding="utf-8")
        return binary, receptor, ligand

    def test_scores_exact_pair_with_bounded_protocol_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary, receptor, ligand = self.fixture_paths(root)
            receptor_sha256 = hashlib.sha256(receptor.read_bytes()).hexdigest()
            ligand_sha256 = hashlib.sha256(ligand.read_bytes()).hexdigest()
            binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
            calls: list[list[str]] = []

            def runner(argv, **kwargs):
                calls.append(list(argv))
                if argv[1:] == ["--version"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="smina 2020.12.10\n", stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv, 0, stdout="Affinity: -7.125 (kcal/mol)\n", stderr=""
                )

            result = score_pose_smina(
                receptor,
                ligand,
                smina_binary=binary,
                runner=runner,
                container_image="example.invalid/smina@sha256:" + "a" * 64,
            )

        self.assertEqual(result["schema_version"], SMINA_SCORE_SCHEMA_VERSION)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["scores"], {"smina_affinity_kcal_mol": -7.125})
        provenance = result["provenance"]
        self.assertEqual(provenance["protocol_version"], SMINA_SCORE_PROTOCOL_VERSION)
        self.assertEqual(provenance["mode"], "score_only")
        self.assertEqual(provenance["scoring_function"], "vina")
        self.assertEqual(provenance["seed"], 0)
        self.assertEqual(provenance["cpu"], 1)
        self.assertEqual(
            provenance["inputs"]["protein_sha256"],
            receptor_sha256,
        )
        self.assertEqual(
            provenance["inputs"]["ligand_pose_sha256"],
            ligand_sha256,
        )
        self.assertEqual(
            provenance["tool"]["binary_sha256"],
            binary_sha256,
        )
        score_command = calls[1]
        self.assertIn("--score_only", score_command)
        self.assertNotIn("--minimize", score_command)
        self.assertNotIn("--local_only", score_command)
        self.assertEqual(score_command[score_command.index("--cpu") + 1], "1")
        self.assertEqual(score_command[score_command.index("--seed") + 1], "0")
        self.assertFalse(
            any(temporary in value for value in provenance["command"]),
            provenance["command"],
        )

    def test_prefers_log_when_stdout_mirrors_the_same_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary, receptor, ligand = self.fixture_paths(root)

            def runner(argv, **kwargs):
                if argv[1:] == ["--version"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="smina 2020.12.10", stderr=""
                    )
                log_path = Path(argv[argv.index("--log") + 1])
                log_path.write_text(
                    "Affinity: -6.75 (kcal/mol)\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout="Affinity: -6.75 (kcal/mol)\n",
                    stderr="",
                )

            result = score_pose_smina(
                receptor, ligand, smina_binary=binary, runner=runner
            )

        self.assertEqual(result["scores"]["smina_affinity_kcal_mol"], -6.75)

    def test_rejects_wrong_tool_version_before_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary, receptor, ligand = self.fixture_paths(Path(temporary))
            calls = 0

            def runner(argv, **kwargs):
                nonlocal calls
                calls += 1
                return subprocess.CompletedProcess(
                    argv, 0, stdout="smina 2021.1", stderr=""
                )

            with self.assertRaisesRegex(PoseScoringError, "pinned 2020.12.10"):
                score_pose_smina(
                    receptor, ligand, smina_binary=binary, runner=runner
                )
        self.assertEqual(calls, 1)

    def test_rejects_unbounded_resources_and_unknown_scoring_function(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary, receptor, ligand = self.fixture_paths(Path(temporary))
            with self.assertRaisesRegex(PoseScoringError, "cpu"):
                score_pose_smina(
                    receptor, ligand, smina_binary=binary, cpu=5
                )
            with self.assertRaisesRegex(PoseScoringError, "timeout_seconds"):
                score_pose_smina(
                    receptor,
                    ligand,
                    smina_binary=binary,
                    timeout_seconds=301,
                )
            with self.assertRaisesRegex(PoseScoringError, "scoring_function"):
                score_pose_smina(
                    receptor,
                    ligand,
                    smina_binary=binary,
                    scoring_function="custom",
                )

    def test_rejects_multiple_affinities_as_an_ambiguous_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary, receptor, ligand = self.fixture_paths(Path(temporary))

            def runner(argv, **kwargs):
                if argv[1:] == ["--version"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="smina 2020.12.10", stderr=""
                    )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=(
                        "Affinity: -7.0 (kcal/mol)\n"
                        "Affinity: -6.0 (kcal/mol)\n"
                    ),
                    stderr="",
                )

            with self.assertRaisesRegex(PoseScoringError, "exactly one"):
                score_pose_smina(
                    receptor, ligand, smina_binary=binary, runner=runner
                )


if __name__ == "__main__":
    unittest.main()
