"""Local validation and dry-run interface; GPU dependencies are optional."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Sequence

from .contracts import make_prediction_task, validate_prediction_task, validate_target
from .backfill import build_backfill_plan
from .sizing import GPU_CLASS_NAMES, resolve_gpu_class
from .staging import (
    DEFAULT_EXECUTION_BACKEND,
    DEFAULT_SELECTION_POLICY_VERSION,
    build_staging_plan,
    render_staging_sql,
)
from .worker import execute_task_json
from .intake import WeeklyPolicy
from .weekly import build_public_weekly_plan
from .weekly_quiz import publish_staged_weekly_quiz, stage_weekly_quiz
from .supabase import SupabaseConfigurationError, SupabaseCoordinator


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

    weekly = commands.add_parser(
        "weekly-plan",
        help="download public prerelease inputs and write a no-submit weekly plan",
    )
    weekly.add_argument("--release-date", required=True, help="Saturday date in YYYY-MM-DD")
    weekly.add_argument("--output", required=True, help="destination JSON file")
    weekly.add_argument("--max-targets", type=int, default=8)
    weekly.add_argument("--heavy-atom-minimum", type=int, default=15)
    weekly.add_argument("--diffusion-samples", type=int, default=5)
    weekly.add_argument("--timeout-seconds", type=int, default=30 * 60)
    weekly.add_argument("--msa-mode", choices=("server", "none", "empty"), default="server")
    weekly.add_argument(
        "--gpu-class",
        choices=sorted(GPU_CLASS_NAMES),
        help="pin all weekly tasks to one accelerator class",
    )
    weekly.add_argument(
        "--output-prefix", default="supabase://foldarium-predictions/runs"
    )

    backfill = commands.add_parser(
        "backfill-plan",
        help="write a bounded no-submit OF3/Boltz plan from a CAMEO scan report",
    )
    backfill.add_argument("--input", required=True, help="public_catchup --scan-only report")
    backfill.add_argument("--output", required=True)
    backfill.add_argument("--start-week", required=True)
    backfill.add_argument("--end-week", required=True)
    backfill.add_argument("--max-targets-per-week", type=int, default=2)
    backfill.add_argument(
        "--methods", nargs="+", choices=("openfold3", "boltz2"), default=["openfold3", "boltz2"]
    )
    backfill.add_argument("--diffusion-samples", type=int, default=5)
    backfill.add_argument("--timeout-seconds", type=int, default=20 * 60)
    backfill.add_argument("--msa-mode", choices=("server", "none", "empty"), default="server")
    backfill.add_argument(
        "--output-prefix", default="supabase://foldarium-predictions/runs"
    )

    quiz_stage = commands.add_parser(
        "weekly-quiz-stage",
        help="download completed private runs and build an aligned local blind-quiz stage",
    )
    quiz_stage.add_argument("--campaign", required=True)
    quiz_stage.add_argument("--round-id", required=True)
    quiz_stage.add_argument("--output-dir", required=True)

    quiz_publish = commands.add_parser(
        "weekly-quiz-publish",
        help="upload sanitized staged assets and optionally open the voting round",
    )
    quiz_publish.add_argument("--stage-dir", required=True)
    quiz_publish.add_argument("--opens-at", required=True)
    quiz_publish.add_argument("--closes-at", required=True)
    quiz_publish.add_argument(
        "--open-round",
        action="store_true",
        help="after upload, invoke the privileged RPC that makes the blind round visible",
    )
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
    elif args.command == "weekly-plan":
        release = date.fromisoformat(args.release_date)
        plan, inputs = build_public_weekly_plan(
            release,
            policy=WeeklyPolicy(
                heavy_atom_minimum=args.heavy_atom_minimum,
                max_targets=args.max_targets,
                diffusion_samples=args.diffusion_samples,
                timeout_seconds=args.timeout_seconds,
                msa_mode=args.msa_mode,
                gpu_class=args.gpu_class,
            ),
            output_prefix=args.output_prefix,
        )
        destination = Path(args.output)
        destination.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _print(
            {
                "status": "planned-not-submitted",
                "output": str(destination),
                "plan_sha256": plan["plan_sha256"],
                "budget": plan["budget"],
                "availability": inputs["availability"],
            }
        )
    elif args.command == "backfill-plan":
        source = _read(args.input)
        snapshot = source.get("snapshot")
        candidates = source.get("candidate_manifest")
        if not isinstance(snapshot, dict) or not isinstance(candidates, list):
            raise ValueError("backfill input must be a public_catchup --scan-only report")
        plan = build_backfill_plan(
            candidates,
            start_week=date.fromisoformat(args.start_week),
            end_week=date.fromisoformat(args.end_week),
            source_snapshot_sha256=snapshot["sitemap_sha256"],
            output_prefix=args.output_prefix,
            max_targets_per_week=args.max_targets_per_week,
            methods=args.methods,
            diffusion_samples=args.diffusion_samples,
            timeout_seconds=args.timeout_seconds,
            msa_mode=args.msa_mode,
        )
        destination = Path(args.output)
        destination.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _print(
            {
                "status": "planned-not-submitted",
                "output": str(destination),
                "plan_sha256": plan["plan_sha256"],
                "budget": plan["budget"],
            }
        )
    elif args.command == "weekly-quiz-stage":
        coordinator = SupabaseCoordinator.from_env()
        outputs = coordinator.campaign_prediction_outputs(args.campaign)
        stage = stage_weekly_quiz(
            outputs,
            args.output_dir,
            round_id=args.round_id,
            campaign_id=args.campaign,
            downloader=coordinator.download_content_object,
        )
        _print(
            {
                "status": "staged-not-published",
                "stage": str(Path(args.output_dir).resolve() / "stage.json"),
                "stage_sha256": stage["stage_sha256"],
                "items": len(stage["items"]),
                "choices": sum(len(item["choices"]) for item in stage["items"]),
            }
        )
    elif args.command == "weekly-quiz-publish":
        private = SupabaseCoordinator.from_env()
        public_bucket = os.environ.get("FOLDARIUM_PUBLIC_QUIZ_BUCKET")
        if not public_bucket:
            raise SupabaseConfigurationError(
                "missing required environment variable: FOLDARIUM_PUBLIC_QUIZ_BUCKET"
            )
        public_environment = dict(os.environ)
        public_environment["FOLDARIUM_STORAGE_BUCKET"] = public_bucket
        public = SupabaseCoordinator.from_env(public_environment)
        if public.storage_bucket == private.storage_bucket:
            raise SupabaseConfigurationError(
                "FOLDARIUM_PUBLIC_QUIZ_BUCKET must differ from the private prediction bucket"
            )
        _print(
            publish_staged_weekly_quiz(
                args.stage_dir,
                private_coordinator=private,
                public_coordinator=public,
                opens_at=args.opens_at,
                closes_at=args.closes_at,
                open_round=args.open_round,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
