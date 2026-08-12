from __future__ import annotations

import importlib.util
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


DEPLOY_ROOT = Path(__file__).resolve().parents[1] / "deploy"
SPEC = importlib.util.spec_from_file_location(
    "foldarium_deploy_profile", DEPLOY_ROOT / "deploy_profile.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not import deploy_profile.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DeploymentProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_path = DEPLOY_ROOT / "profiles" / "molspace-main.json"

    def test_reviewed_profile_is_complete_and_publication_is_off(self) -> None:
        profile = MODULE.load_profile(self.profile_path)
        environment = profile["environment"]
        self.assertEqual(set(environment), MODULE.GATE_KEYS)
        self.assertEqual(environment["FOLDARIUM_WEEKLY_MAX_TARGETS"], "2")
        self.assertEqual(environment["FOLDARIUM_WEEKLY_GPU_CLASS"], "l4")
        self.assertEqual(environment["FOLDARIUM_PREDICTION_MAX_CONTAINERS"], "5")
        self.assertEqual(environment["FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH"], "0")

    def test_environment_scrubs_ambient_foldarium_values(self) -> None:
        profile = MODULE.load_profile(self.profile_path)
        environment = MODULE.deployment_environment(
            profile,
            {
                "PATH": "/bin",
                "FOLDARIUM_WEEKLY_MAX_TARGETS": "999",
                "FOLDARIUM_UNREVIEWED_FLAG": "unsafe",
                "MODAL_PROFILE": "wrong",
            },
        )
        self.assertEqual(environment["PATH"], "/bin")
        self.assertEqual(environment["FOLDARIUM_WEEKLY_MAX_TARGETS"], "2")
        self.assertNotIn("FOLDARIUM_UNREVIEWED_FLAG", environment)
        self.assertEqual(environment["MODAL_PROFILE"], "molspace-production")
        self.assertEqual(
            environment[MODULE.DEPLOYMENT_DIGEST_ENV], MODULE.profile_digest(profile)
        )

    def test_missing_gate_fails_closed(self) -> None:
        import json

        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        del profile["environment"]["FOLDARIUM_WEEKLY_SUBMIT"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing FOLDARIUM_WEEKLY_SUBMIT"):
                MODULE.load_profile(path)

    def test_publish_true_fails_closed(self) -> None:
        import json

        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        profile["environment"]["FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH"] = "1"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "requires.*PUBLISH='0'"):
                MODULE.load_profile(path)

    def test_default_invocation_is_validation_only(self) -> None:
        self.assertEqual(MODULE.main(["--profile", str(self.profile_path)]), 0)

    def test_apply_requires_typed_confirmation_before_any_subprocess(self) -> None:
        with patch.object(MODULE.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "--confirm must exactly equal"):
                MODULE.main(["--profile", str(self.profile_path), "--apply"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
