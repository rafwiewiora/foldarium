#!/usr/bin/env python3
"""Audited weekly LLM selector scoring runner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from foldarium_pipeline.weekly_llm_providers.claude import (
    ClaudeProvider,
    preflight_claude_auth,
)
from foldarium_pipeline.weekly_llm_providers.cursor import (
    CursorProvider,
    list_cursor_models,
    preflight_cursor_api_key,
)
from foldarium_pipeline.weekly_llm_providers.fake import FakeProvider
from foldarium_pipeline.weekly_llm_runner import RunnerOptions, run_weekly_llm_score
from foldarium_pipeline.weekly_selector import canonical_json


def _build_provider(args: argparse.Namespace):
    if args.provider == "fake":
        fixture_path = args.fake_fixture
        if fixture_path is None:
            raise SystemExit("fake provider requires --fake-fixture")
        return FakeProvider(fixture_path=fixture_path), "fake", args.display_name or "Fake Provider"
    if args.provider == "claude":
        return (
            ClaudeProvider(dry_run=args.dry_run_provider),
            "anthropic",
            args.display_name or "Claude Opus",
        )
    if args.provider == "cursor":
        return (
            CursorProvider(dry_run=args.dry_run_provider),
            "cursor",
            args.display_name or "GPT-5.6 Sol",
        )
    raise SystemExit(f"unsupported provider: {args.provider}")


def _cmd_run(args: argparse.Namespace) -> int:
    provider, provider_name, display_name = _build_provider(args)
    submit_token = os.environ.get("FOLDARIUM_SELECTOR_BENCHMARK_TOKEN")
    submit_url = args.submit_url or os.environ.get("FOLDARIUM_SELECTOR_BENCHMARK_URL")
    dry_run_submit = args.artifact_only or not submit_url or not submit_token
    result = run_weekly_llm_score(
        RunnerOptions(
            kit_path=args.kit,
            output_dir=args.output_dir,
            provider=provider,
            display_name=display_name,
            provider_name=provider_name,
            network_allowlist_path=args.network_allowlist,
            egress_enforcement_asserted=args.assert_provider_egress_enforced,
            execution_id=args.execution_id,
            supersedes_execution_id=args.supersedes_execution_id,
            submit_url=submit_url,
            submit_token=submit_token,
            dry_run_submit=dry_run_submit,
        )
    )
    print(
        canonical_json(
            {
                "ok": True,
                "execution_id": result.execution["execution_id"],
                "execution_digest": result.execution_digest,
                "payload_digest": result.payload_digest,
                "output_sha256": result.output_sha256,
                "benchmark_path": str(result.benchmark_path),
                "submission_path": str(result.submission_path),
                "private_dir": str(result.private_dir),
                "submitted": result.submit_receipt is not None,
            }
        )
    )
    return 0


def _cmd_preflight_claude(_args: argparse.Namespace) -> int:
    status = preflight_claude_auth()
    print(canonical_json({"ok": True, "provider": "claude", **status}))
    return 0


def _cmd_preflight_cursor(_args: argparse.Namespace) -> int:
    preflight_cursor_api_key()
    print(canonical_json({"ok": True, "provider": "cursor", "api_key_present": True}))
    return 0


def _cmd_list_cursor_models(_args: argparse.Namespace) -> int:
    models = list_cursor_models()
    payload = [
        {
            "id": model.id,
            "display_name": model.display_name,
            "parameters": [
                {
                    "id": parameter.id,
                    "display_name": parameter.display_name,
                    "values": [
                        {"value": value.value, "display_name": value.display_name}
                        for value in parameter.values
                    ],
                }
                for parameter in model.parameters
            ],
            "variants": [
                {
                    "display_name": variant.display_name,
                    "params": [{"id": param.id, "value": param.value} for param in variant.params],
                }
                for variant in model.variants
            ],
        }
        for model in models
    ]
    print(canonical_json({"ok": True, "models": payload}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="verify kit, score items, emit benchmark bundle")
    run_parser.add_argument("kit", type=Path, help="path to verified selector kit ZIP")
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument(
        "--provider",
        choices=("fake", "claude", "cursor"),
        default="fake",
        help="scoring provider adapter",
    )
    run_parser.add_argument(
        "--fake-fixture",
        type=Path,
        help="JSON fixture for fake provider responses",
    )
    run_parser.add_argument(
        "--dry-run-provider",
        action="store_true",
        help="block paid provider calls (use with fake provider in tests)",
    )
    run_parser.add_argument("--display-name", default=None)
    run_parser.add_argument("--execution-id", default=None)
    run_parser.add_argument("--supersedes-execution-id", default=None)
    run_parser.add_argument(
        "--network-allowlist",
        type=Path,
        help="reviewed provider-only network allowlist JSON for live providers",
    )
    run_parser.add_argument(
        "--assert-provider-egress-enforced",
        action="store_true",
        help="operator attestation that provider-only egress enforcement is active",
    )
    run_parser.add_argument(
        "--submit-url",
        default=None,
        help="optional benchmark endpoint URL (default: FOLDARIUM_SELECTOR_BENCHMARK_URL env)",
    )
    run_parser.add_argument(
        "--artifact-only",
        action="store_true",
        help="never submit to network (default unless URL and env token are set)",
    )
    run_parser.set_defaults(handler=_cmd_run)

    preflight_claude_parser = subparsers.add_parser(
        "preflight-claude",
        help="verify Claude CLI subscription auth without scoring",
    )
    preflight_claude_parser.set_defaults(handler=_cmd_preflight_claude)

    preflight_cursor_parser = subparsers.add_parser(
        "preflight-cursor",
        help="verify CURSOR_API_KEY is present without scoring",
    )
    preflight_cursor_parser.set_defaults(handler=_cmd_preflight_cursor)

    list_models_parser = subparsers.add_parser(
        "list-cursor-models",
        help="list accessible Cursor models and reasoning parameters",
    )
    list_models_parser.set_defaults(handler=_cmd_list_cursor_models)

    args = parser.parse_args(argv)
    if args.command == "run" and args.execution_id is None:
        args.execution_id = str(uuid.uuid4())
    try:
        return args.handler(args)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
