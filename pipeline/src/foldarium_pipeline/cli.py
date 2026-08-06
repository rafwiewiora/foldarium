"""Local validation and dry-run interface; GPU dependencies are optional."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .contracts import make_prediction_task, validate_prediction_task, validate_target
from .worker import execute_task_json


def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Foldarium prediction pipeline")
    commands = parser.add_subparsers(dest="command", required=True)

    target = commands.add_parser("validate-target", help="validate a target package")
    target.add_argument("target_json")

    task = commands.add_parser("validate-task", help="validate a prediction task")
    task.add_argument("task_json")

    make = commands.add_parser("make-task", help="create a deterministic task")
    make.add_argument("target_json")
    make.add_argument("--campaign", required=True)
    make.add_argument("--method", required=True, choices=("openfold3", "boltz2"))
    make.add_argument("--method-version", required=True)
    make.add_argument("--image", required=True)
    make.add_argument("--config-json", required=True)
    make.add_argument("--output-prefix", required=True)

    plan = commands.add_parser("plan", help="materialize inputs without launching a model")
    plan.add_argument("task_json")
    plan.add_argument("--work-root", default=".foldarium-work")

    run = commands.add_parser("run", help="run one task in the current environment")
    run.add_argument("task_json")
    run.add_argument("--work-root", default=".foldarium-work")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-target":
        _print(validate_target(_read(args.target_json)))
    elif args.command == "validate-task":
        _print(validate_prediction_task(_read(args.task_json)))
    elif args.command == "make-task":
        _print(
            make_prediction_task(
                campaign_id=args.campaign,
                target=_read(args.target_json),
                method=args.method,
                method_version=args.method_version,
                container_image=args.image,
                config=_read(args.config_json),
                output_uri_prefix=args.output_prefix,
            )
        )
    elif args.command in {"plan", "run"}:
        _print(
            execute_task_json(
                _read(args.task_json),
                args.work_root,
                dry_run=args.command == "plan",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
