from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from foldarium_pipeline.weekly_intake_recovery import validate_intake_replay_release_date


class ValidateIntakeReplayDateTests(unittest.TestCase):
    def test_rejects_future_and_non_saturday(self) -> None:
        saturday = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "Saturday"):
            validate_intake_replay_release_date("2026-09-04", now=saturday)
        with self.assertRaisesRegex(ValueError, "future"):
            validate_intake_replay_release_date("2026-09-12", now=saturday)
        self.assertEqual(
            validate_intake_replay_release_date("2026-09-05", now=saturday),
            "2026-09-05",
        )


def _install_modal_stub() -> None:
    modal_stub = types.ModuleType("modal")

    class _Fluent:
        def env(self, *_args, **_kwargs):
            return self

        def entrypoint(self, *_args, **_kwargs):
            return self

        def add_local_dir(self, *_args, **_kwargs):
            return self

        def add_local_file(self, *_args, **_kwargs):
            return self

        def apt_install(self, *_args, **_kwargs):
            return self

        def uv_pip_install(self, *_args, **_kwargs):
            return self

    _fluent = _Fluent()

    class _Cron:
        def __init__(self, *_args, **_kwargs):
            pass

    class _Retries:
        def __init__(self, **_kwargs):
            pass

    class _App:
        def __init__(self, *_args, **_kwargs):
            pass

        def function(self, **_kwargs):
            def decorator(fn):
                fn.get_raw_f = lambda: fn  # type: ignore[attr-defined]
                return fn

            return decorator

        def local_entrypoint(self):
            def decorator(fn):
                return fn

            return decorator

    modal_stub.is_local = lambda: False
    modal_stub.App = _App
    modal_stub.Cron = _Cron
    modal_stub.Retries = _Retries
    modal_stub.Volume = types.SimpleNamespace(
        from_name=lambda *_a, **_k: types.SimpleNamespace(
            commit=lambda: None,
            reload=lambda: None,
        )
    )
    modal_stub.Secret = types.SimpleNamespace(from_name=lambda *_a, **_k: object())
    modal_stub.Image = types.SimpleNamespace(
        from_registry=lambda *_a, **_k: _fluent,
        debian_slim=lambda **_k: _fluent,
    )
    modal_stub.__spec__ = importlib.machinery.ModuleSpec("modal", loader=None)
    sys.modules["modal"] = modal_stub


class WeeklyIntakeReplayModalTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        saved_modal = sys.modules.get("modal")
        _install_modal_stub()
        path = Path(__file__).resolve().parents[1] / "deploy" / "modal_app.py"
        spec = importlib.util.spec_from_file_location(
            "foldarium_test_modal_app_replay", path
        )
        if spec is None or spec.loader is None:
            raise AssertionError("could not load Modal deployment adapter")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            return module
        finally:
            if saved_modal is None:
                sys.modules.pop("modal", None)
            else:
                sys.modules["modal"] = saved_modal

    def test_dry_run_forces_register_and_submit_off(self) -> None:
        module = self.deployment_module()
        observed: dict[str, str | None] = {}

        def fake_pipeline(*, submit: bool | None = None, spawn_task=None):
            observed["submit"] = str(submit)
            observed["register"] = os.environ.get("FOLDARIUM_WEEKLY_REGISTER")
            observed["release"] = os.environ.get("FOLDARIUM_RELEASE_DATE")
            observed["spawn"] = str(spawn_task is not None)
            return {"status": "planned-not-submitted", "count": 4}

        raw = module.weekly_intake_replay.get_raw_f()
        with patch.dict(
            os.environ,
            {module.WEEKLY_HOOK_ENV: "foldarium_pipeline.weekly:modal_weekly_hook"},
            clear=False,
        ), patch.object(module, "_invoke_weekly_hook_pipeline", side_effect=fake_pipeline):
            outcome = raw("2026-09-05", False)

        self.assertEqual(outcome["mode"], "dry-run")
        self.assertEqual(outcome["planned_task_count"], 4)
        self.assertFalse(outcome["apply"])
        self.assertEqual(observed["register"], "0")
        self.assertEqual(observed["submit"], "False")
        self.assertEqual(observed["spawn"], "False")
        self.assertEqual(observed["release"], "2026-09-05")
        self.assertNotIn("FOLDARIUM_RELEASE_DATE", os.environ)

    def test_apply_reports_idempotent_already_registered(self) -> None:
        module = self.deployment_module()

        def fake_pipeline(*, submit: bool | None = None, spawn_task=None):
            return {
                "status": "already-registered",
                "count": 0,
                "registration": {"status": "already-registered"},
            }

        raw = module.weekly_intake_replay.get_raw_f()
        with patch.dict(
            os.environ,
            {module.WEEKLY_HOOK_ENV: "foldarium_pipeline.weekly:modal_weekly_hook"},
            clear=False,
        ), patch.object(module, "_invoke_weekly_hook_pipeline", side_effect=fake_pipeline):
            outcome = raw("2026-09-05", True)

        self.assertTrue(outcome["idempotent"])
        self.assertEqual(outcome["status"], "already-registered")
        self.assertEqual(outcome["planned_task_count"], 0)
        self.assertNotIn("FOLDARIUM_WEEKLY_SUBMIT", os.environ)


if __name__ == "__main__":
    unittest.main()
