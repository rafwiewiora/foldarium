#!/usr/bin/env python3
"""Fail-closed deployment wrapper for the production Foldarium Modal app.

The reviewed profile contains non-secret deploy-time gates only. Modal secrets
remain managed by Modal and are never read or printed here. Without ``--apply``
this program performs local validation and prints the effective configuration;
it never contacts Modal or changes a deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = Path(__file__).resolve().parent / "profiles" / "molspace-main.json"
DEPLOYMENT_DIGEST_ENV = "FOLDARIUM_DEPLOYMENT_CONFIG_SHA256"
GATE_KEYS = frozenset(
    {
        "FOLDARIUM_ENABLE_WEEKLY_CRON",
        "FOLDARIUM_WEEKLY_CRON",
        "FOLDARIUM_WEEKLY_HOOK",
        "FOLDARIUM_WEEKLY_REGISTER",
        "FOLDARIUM_WEEKLY_SUBMIT",
        "FOLDARIUM_WEEKLY_MAX_TARGETS",
        "FOLDARIUM_WEEKLY_GPU_CLASS",
        "FOLDARIUM_PUBLIC_QUIZ_BUCKET",
        "FOLDARIUM_PREDICTION_MAX_CONTAINERS",
        "FOLDARIUM_ENABLE_WEDNESDAY_REVEAL",
        "FOLDARIUM_WEDNESDAY_REVEAL_CRON",
        "FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH",
    }
)
EXPECTED_FIXED_VALUES = {
    "FOLDARIUM_ENABLE_WEEKLY_CRON": "1",
    "FOLDARIUM_WEEKLY_HOOK": "foldarium_pipeline.weekly:modal_weekly_hook",
    "FOLDARIUM_WEEKLY_REGISTER": "1",
    "FOLDARIUM_WEEKLY_SUBMIT": "1",
    "FOLDARIUM_WEEKLY_GPU_CLASS": "l4",
    "FOLDARIUM_PUBLIC_QUIZ_BUCKET": "foldarium-weekly-quiz",
    "FOLDARIUM_ENABLE_WEDNESDAY_REVEAL": "1",
    "FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH": "0",
}


def load_profile(path: Path) -> dict[str, object]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise ValueError("deployment profile must be a JSON object")
    if profile.get("schema_version") != 1:
        raise ValueError("deployment profile schema_version must be 1")
    expected_top_level = {
        "schema_version",
        "profile_name",
        "modal_profile",
        "modal_environment",
        "app_name",
        "entrypoint",
        "environment",
        "review_notes",
    }
    unknown_top_level = set(profile).difference(expected_top_level)
    if unknown_top_level:
        raise ValueError(
            "unknown deployment profile keys: " + ", ".join(sorted(unknown_top_level))
        )
    for key in expected_top_level.difference(
        {"schema_version", "environment", "review_notes"}
    ):
        if not isinstance(profile.get(key), str) or not profile[key]:
            raise ValueError(f"deployment profile {key} must be a non-empty string")
    review_notes = profile.get("review_notes")
    if not isinstance(review_notes, dict) or not review_notes:
        raise ValueError("deployment profile review_notes must be a non-empty object")
    if any(not isinstance(value, str) or not value for value in review_notes.values()):
        raise ValueError("every deployment review note must be a non-empty string")
    environment = profile.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("deployment profile environment must be an object")
    if set(environment) != GATE_KEYS:
        missing = GATE_KEYS.difference(environment)
        unknown = set(environment).difference(GATE_KEYS)
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError("invalid deployment gate set: " + "; ".join(details))
    if any(not isinstance(value, str) or not value for value in environment.values()):
        raise ValueError("every deployment gate must be a non-empty string")
    for key, expected in EXPECTED_FIXED_VALUES.items():
        if environment[key] != expected:
            raise ValueError(f"reviewed production profile requires {key}={expected!r}")
    for key in ("FOLDARIUM_WEEKLY_MAX_TARGETS", "FOLDARIUM_PREDICTION_MAX_CONTAINERS"):
        try:
            value = int(environment[key])
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if value < 1:
            raise ValueError(f"{key} must be positive")
    if int(environment["FOLDARIUM_WEEKLY_MAX_TARGETS"]) != 2:
        raise ValueError(
            "the reviewed profile retains the two-target safety cap; change it only "
            "in a separately reviewed commit"
        )
    if int(environment["FOLDARIUM_PREDICTION_MAX_CONTAINERS"]) != 5:
        raise ValueError("the reviewed production concurrency must remain 5")
    return profile


def canonical_profile(profile: Mapping[str, object]) -> bytes:
    return json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")


def profile_digest(profile: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_profile(profile)).hexdigest()


def deployment_environment(
    profile: Mapping[str, object], base: Mapping[str, str]
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in base.items()
        if not key.startswith("FOLDARIUM_")
        and key not in {"MODAL_PROFILE", "MODAL_ENVIRONMENT"}
    }
    reviewed = profile["environment"]
    assert isinstance(reviewed, dict)
    environment.update(reviewed)
    environment[DEPLOYMENT_DIGEST_ENV] = profile_digest(profile)
    environment["MODAL_PROFILE"] = str(profile["modal_profile"])
    environment["MODAL_ENVIRONMENT"] = str(profile["modal_environment"])
    return environment


def deploy_command(profile: Mapping[str, object], modal_bin: Path) -> list[str]:
    return [
        str(modal_bin),
        "deploy",
        "--env",
        str(profile["modal_environment"]),
        "--strategy",
        "rolling",
        str(profile["entrypoint"]),
    ]


def _check_clean_worktree(repository_root: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("production deployment requires a clean Git worktree")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _verify_modal_profile(modal_bin: Path, environment: Mapping[str, str], expected: str) -> None:
    current = subprocess.run(
        [str(modal_bin), "profile", "current"],
        cwd=REPOSITORY_ROOT,
        env=dict(environment),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if current != expected:
        raise RuntimeError(f"Modal resolved profile {current!r}, expected {expected!r}")


def _verify_deployed_config(
    profile: Mapping[str, object],
    modal_python: Path,
    environment: Mapping[str, str],
) -> None:
    script = """
import json
import os
import modal
report = modal.Function.from_name(
    os.environ["FOLDARIUM_VERIFY_APP"],
    "deployment_config",
    environment_name=os.environ["FOLDARIUM_VERIFY_ENVIRONMENT"],
).remote()
print(json.dumps(report, sort_keys=True))
"""
    verification_environment = dict(environment)
    verification_environment["FOLDARIUM_VERIFY_APP"] = str(profile["app_name"])
    verification_environment["FOLDARIUM_VERIFY_ENVIRONMENT"] = str(
        profile["modal_environment"]
    )
    completed = subprocess.run(
        [str(modal_python), "-c", script],
        cwd=REPOSITORY_ROOT,
        env=verification_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise RuntimeError("deployed configuration verification returned no output")
    report = json.loads(output_lines[-1])
    expected_digest = profile_digest(profile)
    if not isinstance(report, dict) or report.get("config_sha256") != expected_digest:
        raise RuntimeError("deployed Modal configuration digest does not match reviewed profile")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--modal-bin",
        type=Path,
        default=REPOSITORY_ROOT / "pipeline" / ".venv" / "bin" / "modal",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm",
        help="required with --apply; must equal profile/environment/app",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    profile = load_profile(args.profile.resolve())
    digest = profile_digest(profile)
    summary = {
        "profile_name": profile["profile_name"],
        "modal_profile": profile["modal_profile"],
        "modal_environment": profile["modal_environment"],
        "app_name": profile["app_name"],
        "entrypoint": profile["entrypoint"],
        "config_sha256": digest,
        "environment": profile["environment"],
        "mode": "apply" if args.apply else "validate-only",
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.apply:
        return 0

    expected_confirmation = "/".join(
        str(profile[key]) for key in ("modal_profile", "modal_environment", "app_name")
    )
    if args.confirm != expected_confirmation:
        raise RuntimeError(f"--confirm must exactly equal {expected_confirmation!r}")
    if not args.modal_bin.is_file():
        raise RuntimeError(f"Modal executable not found: {args.modal_bin}")
    modal_python = args.modal_bin.parent / "python"
    if not modal_python.is_file():
        raise RuntimeError(f"Modal Python executable not found: {modal_python}")
    commit_sha = _check_clean_worktree(REPOSITORY_ROOT)
    environment = deployment_environment(profile, os.environ)
    _verify_modal_profile(args.modal_bin, environment, str(profile["modal_profile"]))
    print(f"Deploying reviewed commit {commit_sha} with config {digest}")
    subprocess.run(
        deploy_command(profile, args.modal_bin),
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
    )
    _verify_deployed_config(profile, modal_python, environment)
    print("Post-deploy configuration digest verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
