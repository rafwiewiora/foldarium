"""Foldarium-owned adapter for the independent OpenFold3 CLI."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from ..contracts import ContractError, validate_int_config
from .base import CommandPlan, MethodAdapter, artifact, confidence_summary, write_json

OPENFOLD3_VERSION = "0.4.4"
OPENFOLD3_IMAGE = (
    "openfoldconsortium/openfold3:0.4-pixi@"
    "sha256:9bc891b799285f0edae94f9f3f05ffcb88f29dc8e758248ce384c64f80e16eec"
)
OPENFOLD3_CHECKPOINT = "openfold3-p2-155k"
OPENFOLD3_ACTIVATE = "/opt/activate.sh"
_MODEL_RE = re.compile(r"_seed_(?P<seed>\d+)_sample_(?P<sample>\d+)_model\.(?:cif|pdb)$")


class OpenFold3Adapter(MethodAdapter):
    name = "openfold3"

    def _query(self, task: Mapping[str, Any]) -> dict[str, Any]:
        chains: list[dict[str, Any]] = []
        for entity in task["target"]["entities"]:
            chain: dict[str, Any] = {
                "molecule_type": entity["type"],
                "chain_ids": entity["chain_ids"],
            }
            if entity["type"] == "ligand":
                if "smiles" in entity:
                    chain["smiles"] = entity["smiles"]
                else:
                    chain["ccd_codes"] = entity["ccd_codes"]
            else:
                chain["sequence"] = entity["sequence"]
            chains.append(chain)
        return {"queries": {task["target"]["target_id"]: {"chains": chains}}}

    def plan(self, task: Mapping[str, Any], work_dir: Path) -> CommandPlan:
        config = task["config"]
        allowed = {"checkpoint", "diffusion_samples", "model_seeds", "msa_mode", "runner_yaml"}
        unknown = set(config) - allowed
        if unknown:
            raise ContractError(f"unsupported OpenFold3 config keys: {sorted(unknown)}")
        samples = validate_int_config(config, "diffusion_samples", 5, 100)
        seeds = validate_int_config(config, "model_seeds", 1, 100)
        msa_mode = config.get("msa_mode", "server")
        if msa_mode not in {"server", "precomputed", "none"}:
            raise ContractError("OpenFold3 msa_mode must be server, precomputed, or none")
        if msa_mode == "precomputed":
            raise ContractError(
                "precomputed OpenFold3 MSA localization is not implemented; use server only for smoke tests"
            )
        checkpoint = str(config.get("checkpoint", OPENFOLD3_CHECKPOINT))
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", checkpoint):
            raise ContractError("OpenFold3 checkpoint must be a safe registry name")

        input_path = work_dir / "input" / "query.json"
        output_dir = work_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(input_path, self._query(task))
        argv = [
            "/bin/bash",
            "-lc",
            f"source {OPENFOLD3_ACTIVATE} && exec \"$@\"",
            "foldarium-openfold3",
            "run_openfold",
            "predict",
            "--query-json",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--inference-ckpt-name",
            checkpoint,
            "--num-model-seeds",
            str(seeds),
            "--num-diffusion-samples",
            str(samples),
        ]
        if msa_mode == "server":
            argv.append("--use-msa-server")
        runner_yaml = config.get("runner_yaml")
        if runner_yaml:
            if not isinstance(runner_yaml, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,256}", runner_yaml):
                raise ContractError("runner_yaml must be a safe path supplied by the runtime image")
            argv.extend(["--runner-yaml", runner_yaml])
        return CommandPlan(tuple(argv), input_path, output_dir)

    def collect(self, task: Mapping[str, Any], output_dir: Path) -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        for model in sorted((*output_dir.rglob("*_model.cif"), *output_dir.rglob("*_model.pdb"))):
            match = _MODEL_RE.search(model.name)
            if not match:
                continue
            stem = model.name.rsplit("_model.", 1)[0]
            aggregate = model.with_name(f"{stem}_confidences_aggregated.json")
            full = model.with_name(f"{stem}_confidences.json")
            artifacts = [artifact(model, output_dir, "predicted_complex")]
            summary: dict[str, float] = {}
            if aggregate.exists():
                artifacts.append(artifact(aggregate, output_dir, "confidence_summary"))
                summary = confidence_summary(aggregate)
            if full.exists():
                artifacts.append(artifact(full, output_dir, "confidence_full"))
            samples.append(
                {
                    "sample_id": f"seed-{match.group('seed')}-sample-{match.group('sample')}",
                    "seed": int(match.group("seed")),
                    "sample_index": int(match.group("sample")),
                    "confidence": summary,
                    "artifacts": artifacts,
                }
            )
        if not samples:
            raise FileNotFoundError(f"OpenFold3 produced no recognized model files in {output_dir}")
        return samples
