"""Foldarium-owned adapter for the upstream Boltz-2 CLI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from ..contracts import ContractError, validate_int_config
from .base import CommandPlan, MethodAdapter, artifact, confidence_summary

BOLTZ2_VERSION = "2.2.1"
_MODEL_RE = re.compile(r"_model_(?P<rank>\d+)\.(?:cif|pdb)$")


class Boltz2Adapter(MethodAdapter):
    name = "boltz2"

    def _input(self, task: Mapping[str, Any], msa_mode: str) -> dict[str, Any]:
        sequences: list[dict[str, Any]] = []
        for entity in task["target"]["entities"]:
            body: dict[str, Any] = {"id": entity["chain_ids"]}
            if len(body["id"]) == 1:
                body["id"] = body["id"][0]
            if entity["type"] == "ligand":
                if "smiles" in entity:
                    body["smiles"] = entity["smiles"]
                else:
                    codes = entity["ccd_codes"]
                    if len(codes) != 1:
                        raise ContractError("Boltz-2 currently requires one CCD code per ligand entity")
                    body["ccd"] = codes[0]
            else:
                body["sequence"] = entity["sequence"]
                if msa_mode == "empty" and entity["type"] == "protein":
                    body["msa"] = "empty"
                if msa_mode == "artifact" and entity["type"] == "protein":
                    msa = entity.get("msa", {})
                    if msa.get("mode") != "artifact" or "local_path" not in msa:
                        raise ContractError(
                            "artifact MSAs must be localized by the worker before Boltz-2 planning"
                        )
                    body["msa"] = msa["local_path"]
            sequences.append({entity["type"]: body})
        return {"version": 1, "sequences": sequences}

    def plan(self, task: Mapping[str, Any], work_dir: Path) -> CommandPlan:
        config = task["config"]
        allowed = {
            "diffusion_samples",
            "max_parallel_samples",
            "msa_mode",
            "recycling_steps",
            "sampling_steps",
            "seed",
            "step_scale",
        }
        unknown = set(config) - allowed
        if unknown:
            raise ContractError(f"unsupported Boltz-2 config keys: {sorted(unknown)}")
        diffusion_samples = validate_int_config(config, "diffusion_samples", 5, 100)
        max_parallel = validate_int_config(config, "max_parallel_samples", 1, 100)
        recycling_steps = validate_int_config(config, "recycling_steps", 3, 100)
        sampling_steps = validate_int_config(config, "sampling_steps", 200, 1000)
        seed = config.get("seed", 0)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
            raise ContractError("config.seed must be an integer from 0 to 2147483647")
        step_scale = config.get("step_scale", 1.5)
        if isinstance(step_scale, bool) or not isinstance(step_scale, (int, float)) or not 0 < step_scale <= 10:
            raise ContractError("config.step_scale must be a number from 0 to 10")
        msa_mode = config.get("msa_mode", "server")
        if msa_mode not in {"server", "empty", "artifact"}:
            raise ContractError("Boltz-2 msa_mode must be server, empty, or artifact")

        input_path = work_dir / "input" / "target.yaml"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        # JSON is valid YAML 1.2 and avoids another runtime dependency in the core package.
        input_path.write_text(json.dumps(self._input(task, msa_mode), indent=2) + "\n", encoding="utf-8")
        output_dir = work_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            "boltz",
            "predict",
            str(input_path),
            "--model",
            "boltz2",
            "--out_dir",
            str(output_dir),
            "--output_format",
            "mmcif",
            "--seed",
            str(seed),
            "--recycling_steps",
            str(recycling_steps),
            "--sampling_steps",
            str(sampling_steps),
            "--diffusion_samples",
            str(diffusion_samples),
            "--max_parallel_samples",
            str(max_parallel),
            "--step_scale",
            str(float(step_scale)),
        ]
        if msa_mode == "server":
            argv.append("--use_msa_server")
        return CommandPlan(tuple(argv), input_path, output_dir, {"BOLTZ_CACHE": "/cache/boltz"})

    def collect(self, task: Mapping[str, Any], output_dir: Path) -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        seed = int(task["config"].get("seed", 0))
        for model in sorted((*output_dir.rglob("*_model_*.cif"), *output_dir.rglob("*_model_*.pdb"))):
            match = _MODEL_RE.search(model.name)
            if not match:
                continue
            rank = int(match.group("rank"))
            base = model.name.rsplit("_model_", 1)[0]
            confidence = model.with_name(f"confidence_{base}_model_{rank}.json")
            artifacts = [artifact(model, output_dir, "predicted_complex")]
            summary: dict[str, float] = {}
            if confidence.exists():
                artifacts.append(artifact(confidence, output_dir, "confidence_summary"))
                summary = confidence_summary(confidence)
            for prefix, role in (("pae_", "pae"), ("pde_", "pde"), ("plddt_", "plddt")):
                array = model.with_name(f"{prefix}{base}_model_{rank}.npz")
                if array.exists():
                    artifacts.append(artifact(array, output_dir, role))
            samples.append(
                {
                    "sample_id": f"seed-{seed}-rank-{rank}",
                    "seed": seed,
                    "sample_index": rank,
                    "confidence": summary,
                    "artifacts": artifacts,
                }
            )
        if not samples:
            raise FileNotFoundError(f"Boltz-2 produced no recognized model files in {output_dir}")
        return samples
