"""Local validation and dry-run interface; GPU dependencies are optional."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .contracts import make_prediction_task, validate_prediction_task, validate_target
from .sizing import GPU_CLASS_NAMES, resolve_gpu_class
from .staging import (
    DEFAULT_EXECUTION_BACKEND,
    DEFAULT_SELECTION_POLICY_VERSION,
    build_staging_plan,
    render_staging_sql,
)
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
    make.add_argument(
        "--resources-json",
        help="execution budget (e.g. timeout_seconds); part of the task, not its identity",
    )
    make.add_argument(
        "--gpu-class",
        choices=sorted(GPU_CLASS_NAMES),
        help="pin the accelerator class; omit to size it from the target",
    )
    make.add_argument(
        "--no-gpu-class",
        action="store_true",
        help="omit gpu_class entirely and let the execution backend decide",
    )

    stage = commands.add_parser(
        "stage-sql", help="render idempotent control-plane rows for planned tasks"
    )
    stage.add_argument("task_json", nargs="+")
    stage.add_argument("--adapter-version", required=True)
    stage.add_argument("--campaign-name")
    stage.add_argument("--campaign-source", default="synthetic-smoke-test")
    stage.add_argument(
        "--selection-policy-version", default=DEFAULT_SELECTION_POLICY_VERSION
    )
    stage.add_argument("--execution-backend", default=DEFAULT_EXECUTION_BACKEND)
    stage.add_argument("--max-attempts", type=int, default=1)
    stage.add_argument("--status", default="pending", choices=("pending", "queued"))

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
        target = _read(args.target_json)
        config = _read(args.config_json)
        resources = _read(args.resources_json) if args.resources_json else {}
        if not args.no_gpu_class:
            # Sizing is recorded in resources, which the identity hash excludes,
            # so the run ID stays the same whatever hardware is chosen.
            resources = {
                **resources,
                "gpu_class": resolve_gpu_class(target, config, args.gpu_class),
            }
        _print(
            make_prediction_task(
                campaign_id=args.campaign,
                target=target,
                method=args.method,
                method_version=args.method_version,
                container_image=args.image,
                config=config,
                output_uri_prefix=args.output_prefix,
                resources=resources,
            )
        )
    elif args.command == "stage-sql":
        print(
            render_staging_sql(
                build_staging_plan(
                    [_read(path) for path in args.task_json],
                    adapter_version=args.adapter_version,
                    campaign_name=args.campaign_name,
                    campaign_source=args.campaign_source,
                    selection_policy_version=args.selection_policy_version,
                    execution_backend=args.execution_backend,
                    max_attempts=args.max_attempts,
                    status=args.status,
                )
            ),
            end="",
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
