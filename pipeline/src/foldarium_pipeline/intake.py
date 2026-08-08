"""Deterministic Saturday prerelease intake and prediction planning.

Intake is deliberately pure: callers supply the bytes fetched from wwPDB/CAMEO
and receive a replayable plan.  Network access, Supabase registration, and Modal
submission are separate seams, which lets tomorrow's prerelease be rehearsed
without creating a database row or spending a GPU credit.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from .contracts import canonical_json, make_prediction_task, validate_target
from .methods.boltz2 import BOLTZ2_VERSION
from .methods.openfold3 import (
    OPENFOLD3_CHECKPOINT,
    OPENFOLD3_IMAGE,
    OPENFOLD3_VERSION,
)
from .selection import HEAVY_ATOM_MINIMUM, SELECTION_POLICY_VERSION, select_ligand
from .sizing import SizingError, count_tokens, resolve_gpu_class, validate_gpu_class

WWPDB_SEQUENCE_URL = "https://www.wwpdb.org/files/new_release_structure_sequence_canonical.tsv"
WWPDB_NONPOLYMER_URL = "https://www.wwpdb.org/files/new_release_structure_nonpolymer.tsv"
INTAKE_SCHEMA_VERSION = "foldarium.weekly-intake/v1"
ADAPTER_VERSION = "foldarium-pipeline/0.2"

POLYMER_TYPES = {"protein": "protein", "peptide": "protein", "dna": "dna", "rna": "rna"}
NUCLEIC_ACID_CANONICAL_ALPHABET = frozenset("ACGTUIN")


class IntakeError(ValueError):
    """Raised when a prerelease snapshot or CAMEO target cannot be planned safely."""


@dataclass(frozen=True)
class WeeklyPolicy:
    """Cost-bounded, versioned policy for one blind weekly round."""

    heavy_atom_minimum: int = HEAVY_ATOM_MINIMUM
    max_targets: int = 8
    diffusion_samples: int = 5
    timeout_seconds: int = 20 * 60
    msa_mode: str = "server"
    protein_only: bool = True
    gpu_class: str | None = None

    def validate(self) -> "WeeklyPolicy":
        for name in ("heavy_atom_minimum", "max_targets", "diffusion_samples", "timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise IntakeError(f"{name} must be a positive integer")
        if self.diffusion_samples > 100:
            raise IntakeError("diffusion_samples cannot exceed 100")
        if self.msa_mode not in {"server", "none", "empty"}:
            raise IntakeError("msa_mode must be server, none, or empty")
        if not isinstance(self.protein_only, bool):
            raise IntakeError("protein_only must be boolean")
        if self.gpu_class is not None:
            validate_gpu_class(self.gpu_class)
        return self


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_tsv(data: bytes, required: tuple[str, ...], label: str) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IntakeError(f"{label} is not UTF-8") from exc
    first_line = text.splitlines()[0] if text.splitlines() else ""
    first_fields = [field.strip() for field in first_line.split("\t")]
    # wwPDB's canonical sequence file is intentionally headerless (three fixed
    # columns), while the non-polymer file carries names. Accept both official
    # forms but reject ambiguous headerless widths.
    if set(required).issubset(first_fields):
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    else:
        if len(first_fields) != len(required):
            raise IntakeError(f"{label} is missing required columns {required}")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t", fieldnames=required)
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {key: (value or "").strip() for key, value in raw.items() if key is not None}
        if not row.get("PDB_ID"):
            continue
        row["PDB_ID"] = row["PDB_ID"].upper()
        rows.append(row)
    return rows


def parse_wwpdb_snapshot(sequence_tsv: bytes, nonpolymer_tsv: bytes) -> dict[str, Any]:
    """Parse and summarize the two official Saturday prerelease files."""

    sequences = _decode_tsv(
        sequence_tsv, ("PDB_ID", "Sequence_Count", "Sequence"), "sequence prerelease"
    )
    ligands = _decode_tsv(
        nonpolymer_tsv,
        ("PDB_ID", "Component_ID", "InChI", "SMILES string"),
        "non-polymer prerelease",
    )
    entries: dict[str, dict[str, Any]] = {}
    for row in sequences:
        entry = entries.setdefault(row["PDB_ID"], {"sequences": [], "ligands": []})
        sequence = "".join(row["Sequence"].split()).upper()
        if not sequence or not sequence.isalpha():
            raise IntakeError(f"canonical sequence for {row['PDB_ID']} contains non-letters")
        entry["sequences"].append(sequence)
    for row in ligands:
        entry = entries.setdefault(row["PDB_ID"], {"sequences": [], "ligands": []})
        entry["ligands"].append(
            {
                "component_id": row["Component_ID"].upper(),
                "inchi": row["InChI"],
                "smiles": row["SMILES string"],
            }
        )
    return {
        "sequence_url": WWPDB_SEQUENCE_URL,
        "sequence_sha256": _sha256(sequence_tsv),
        "sequence_rows": len(sequences),
        "nonpolymer_url": WWPDB_NONPOLYMER_URL,
        "nonpolymer_sha256": _sha256(nonpolymer_tsv),
        "nonpolymer_rows": len(ligands),
        "entry_count": len(entries),
        "entries": entries,
    }


def _chain_id(index: int) -> str:
    if index < 0:
        raise IntakeError("chain index must be non-negative")
    letter = chr(ord("A") + index % 26)
    generation = index // 26
    return letter if generation == 0 else f"{letter}{generation}"


def target_from_cameo(payload: Mapping[str, Any], policy: WeeklyPolicy) -> dict[str, Any] | None:
    """Normalize one selected CAMEO target, choosing one quiz ligand."""

    policy.validate()
    target_row = payload.get("target")
    raw_entities = payload.get("entities")
    if not isinstance(target_row, Mapping) or not isinstance(raw_entities, list):
        raise IntakeError("decoded CAMEO payload is missing target/entities")
    source_id = target_row.get("id")
    week = target_row.get("week_id")
    if not isinstance(source_id, str) or not isinstance(week, str):
        raise IntakeError("decoded CAMEO target is missing id/week_id")
    try:
        date.fromisoformat(week)
    except ValueError as exc:
        raise IntakeError("decoded CAMEO target has an invalid week_id") from exc

    polymer_rows = [
        row
        for row in raw_entities
        if isinstance(row, Mapping) and str(row.get("entity_type", "")).lower() in POLYMER_TYPES
    ]
    ligand_rows = [
        {
            "component_id": row.get("component_id"),
            "smiles": row.get("smiles"),
            "inchi": row.get("inchi"),
            "source_entity_id": row.get("id"),
        }
        for row in raw_entities
        if isinstance(row, Mapping) and row.get("entity_type") == "non_polymer"
    ]
    selected = select_ligand(ligand_rows, heavy_atom_minimum=policy.heavy_atom_minimum)
    if not polymer_rows or selected is None:
        return None

    entities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in polymer_rows:
        kind = POLYMER_TYPES[str(raw["entity_type"]).lower()]
        sequence = "".join(str(raw.get("canonical_sequence") or "").split()).upper()
        identity = (kind, sequence)
        if not sequence or not sequence.isalpha():
            raise IntakeError(f"CAMEO target {source_id} has an invalid canonical sequence")
        # CAMEO explicitly does not know prerelease stoichiometry. One copy of
        # each distinct entity is a recorded Foldarium input policy, not a claim
        # about the biological assembly.
        if identity in seen:
            continue
        seen.add(identity)
        entities.append(
            {"type": kind, "chain_ids": [_chain_id(len(entities))], "sequence": sequence}
        )
    entities.append(
        {
            "type": "ligand",
            "chain_ids": [_chain_id(len(entities))],
            "smiles": selected["smiles"],
        }
    )
    target = {
        "target_id": source_id,
        "entities": entities,
        "source": {
            "kind": "cameo-prerelease",
            "cameo_target_id": source_id,
            "week": week,
            "pdb_id": target_row.get("pdbid"),
        },
        "metadata": {
            "selected_ligand": {
                "component_id": selected["component_id"],
                "heavy_atoms": selected["heavy_atoms"],
                "inchi": selected.get("inchi"),
            },
            "cameo_label": target_row.get("labels_submission_3d"),
            "stoichiometry_policy": "one-copy-per-distinct-prerelease-entity/v1",
            "selection_policy_version": SELECTION_POLICY_VERSION,
        },
    }
    return validate_target(target)


def target_from_wwpdb(
    pdb_id: str,
    entry: Mapping[str, Any],
    release_date: date,
    policy: WeeklyPolicy,
) -> dict[str, Any] | None:
    """Build a conservative protein/ligand target from the Saturday files alone.

    The canonical sequence prerelease intentionally omits polymer type and
    stoichiometry. For the initial protein-only campaign, sequences made entirely
    from the nucleic-acid alphabet are rejected rather than guessed. One copy of
    each distinct remaining sequence is the same explicit unknown-stoichiometry
    policy used for CAMEO intake.
    """

    policy.validate()
    if not policy.protein_only:
        raise IntakeError("wwPDB-only intake currently requires protein_only=true")
    if not isinstance(pdb_id, str) or not re.fullmatch(r"[0-9A-Z]{4}", pdb_id):
        raise IntakeError("wwPDB entry has an invalid PDB ID")
    sequences = entry.get("sequences")
    ligands = entry.get("ligands")
    if not isinstance(sequences, list) or not isinstance(ligands, list):
        raise IntakeError(f"wwPDB entry {pdb_id} has invalid sequence/ligand rows")
    selected = select_ligand(ligands, heavy_atom_minimum=policy.heavy_atom_minimum)
    if selected is None:
        return None

    distinct_sequences: list[str] = []
    seen: set[str] = set()
    for raw in sequences:
        sequence = "".join(str(raw).split()).upper()
        if not sequence or not sequence.isalpha():
            raise IntakeError(f"wwPDB entry {pdb_id} has an invalid canonical sequence")
        if set(sequence) <= NUCLEIC_ACID_CANONICAL_ALPHABET:
            raise IntakeError("ambiguous-or-nucleic-acid-polymer")
        if sequence not in seen:
            seen.add(sequence)
            distinct_sequences.append(sequence)
    if not distinct_sequences:
        raise IntakeError("no-protein-like-polymer-sequence")

    entities: list[dict[str, Any]] = [
        {"type": "protein", "chain_ids": [_chain_id(index)], "sequence": sequence}
        for index, sequence in enumerate(distinct_sequences)
    ]
    entities.append(
        {
            "type": "ligand",
            "chain_ids": [_chain_id(len(entities))],
            "smiles": selected["smiles"],
        }
    )
    return validate_target(
        {
            "target_id": pdb_id,
            "entities": entities,
            "source": {
                "kind": "wwpdb-prerelease",
                "week": release_date.isoformat(),
                "pdb_id": pdb_id,
            },
            "metadata": {
                "selected_ligand": {
                    "component_id": selected["component_id"],
                    "heavy_atoms": selected["heavy_atoms"],
                    "inchi": selected.get("inchi"),
                },
                "stoichiometry_policy": "one-copy-per-distinct-prerelease-sequence/v1",
                "polymer_type_policy": "reject-nucleic-alphabet-otherwise-protein/v1",
                "selection_policy_version": SELECTION_POLICY_VERSION,
            },
        }
    )


def _priority(target: Mapping[str, Any], release_date: date) -> tuple[int, str]:
    label = str(target.get("metadata", {}).get("cameo_label") or "").lower()
    label_priority = {"ligand": 0, "hard": 1, "medium": 2, "easy": 3}.get(label, 4)
    digest = hashlib.sha256(
        f"{release_date.isoformat()}:{target['target_id']}".encode("ascii")
    ).hexdigest()
    return label_priority, digest


def _polymer_signature(target: Mapping[str, Any]) -> str:
    polymers = sorted(
        (entity["type"], entity["sequence"])
        for entity in target["entities"]
        if entity["type"] != "ligand"
    )
    return hashlib.sha256(canonical_json(polymers).encode("utf-8")).hexdigest()


def build_method_tasks(
    target: Mapping[str, Any],
    campaign_id: str,
    output_prefix: str,
    policy: WeeklyPolicy,
    methods: Iterable[str] = ("openfold3", "boltz2"),
) -> list[dict[str, Any]]:
    of3_msa = "none" if policy.msa_mode in {"none", "empty"} else "server"
    boltz_msa = "empty" if policy.msa_mode in {"none", "empty"} else "server"
    definitions = (
        (
            "openfold3",
            OPENFOLD3_VERSION,
            "docker.io/" + OPENFOLD3_IMAGE,
            {
                "checkpoint": OPENFOLD3_CHECKPOINT,
                "diffusion_samples": policy.diffusion_samples,
                "model_seeds": 1,
                "msa_mode": of3_msa,
            },
        ),
        (
            "boltz2",
            BOLTZ2_VERSION,
            f"modal-build://boltz[cuda]=={BOLTZ2_VERSION}",
            {
                "diffusion_samples": policy.diffusion_samples,
                "max_parallel_samples": 1,
                "msa_mode": boltz_msa,
                "recycling_steps": 3,
                "sampling_steps": 200,
                "seed": 0,
                "step_scale": 1.5,
            },
        ),
    )
    selected_methods = tuple(methods)
    if not selected_methods or len(set(selected_methods)) != len(selected_methods):
        raise IntakeError("methods must contain unique supported methods")
    available = {row[0]: row for row in definitions}
    if set(selected_methods) - set(available):
        raise IntakeError("methods contains an unsupported prediction method")
    tasks: list[dict[str, Any]] = []
    for method in selected_methods:
        _, version, image, config = available[method]
        gpu_class = resolve_gpu_class(target, config, explicit=policy.gpu_class)
        tasks.append(
            make_prediction_task(
                campaign_id=campaign_id,
                target=target,
                method=method,
                method_version=version,
                container_image=image,
                config=config,
                output_uri_prefix=output_prefix,
                resources={
                    "gpu_class": gpu_class,
                    "timeout_seconds": policy.timeout_seconds,
                },
            )
        )
    return tasks


def build_weekly_plan(
    *,
    release_date: date,
    ww_pdb_snapshot: Mapping[str, Any],
    cameo_payloads: Iterable[Mapping[str, Any]] | None = None,
    output_prefix: str,
    policy: WeeklyPolicy | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded, replayable plan; never submits or registers work."""

    policy = (policy or WeeklyPolicy()).validate()
    if not isinstance(release_date, date):
        raise IntakeError("release_date must be a date")
    if not isinstance(ww_pdb_snapshot, Mapping) or not ww_pdb_snapshot.get("sequence_sha256"):
        raise IntakeError("ww_pdb_snapshot must come from parse_wwpdb_snapshot")
    campaign_id = f"wwpdb-{release_date.isoformat()}"
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    intake_source = "cameo-prerelease" if cameo_payloads is not None else "wwpdb-prerelease"
    if cameo_payloads is None:
        entries = ww_pdb_snapshot.get("entries")
        if not isinstance(entries, Mapping):
            raise IntakeError("ww_pdb_snapshot entries are missing")
        source_rows: Iterable[tuple[str, Any]] = sorted(entries.items())
        for source_id, entry in source_rows:
            try:
                if not isinstance(entry, Mapping):
                    raise IntakeError("invalid wwPDB prerelease entry")
                target = target_from_wwpdb(source_id, entry, release_date, policy)
                if target is None:
                    skipped.append(
                        {"target_id": source_id, "reason": "no-eligible-drug-like-ligand"}
                    )
                    continue
                resolve_gpu_class(target, {"msa_mode": policy.msa_mode})
                accepted.append(target)
            except (IntakeError, SizingError, ValueError) as exc:
                skipped.append({"target_id": source_id, "reason": str(exc)})
    else:
        for payload in cameo_payloads:
            source = payload.get("target") if isinstance(payload, Mapping) else None
            source_id = (
                str(source.get("id", "unknown")) if isinstance(source, Mapping) else "unknown"
            )
            try:
                target = target_from_cameo(payload, policy)
                if target is None:
                    skipped.append(
                        {"target_id": source_id, "reason": "no-eligible-drug-like-ligand"}
                    )
                    continue
                if target["source"]["week"] != release_date.isoformat():
                    skipped.append({"target_id": source_id, "reason": "wrong-release-week"})
                    continue
                polymer_types = {
                    entity["type"]
                    for entity in target["entities"]
                    if entity["type"] != "ligand"
                }
                if policy.protein_only and polymer_types != {"protein"}:
                    skipped.append(
                        {"target_id": source_id, "reason": "unsupported-nonprotein-polymer"}
                    )
                    continue
                resolve_gpu_class(target, {"msa_mode": policy.msa_mode})
                accepted.append(target)
            except (IntakeError, SizingError, ValueError) as exc:
                skipped.append({"target_id": source_id, "reason": str(exc)})

    accepted.sort(key=lambda target: _priority(target, release_date))
    diversified: list[dict[str, Any]] = []
    seen_polymer_sets: set[str] = set()
    for target in accepted:
        signature = _polymer_signature(target)
        if signature in seen_polymer_sets:
            skipped.append(
                {"target_id": target["target_id"], "reason": "duplicate-polymer-complex"}
            )
            continue
        seen_polymer_sets.add(signature)
        diversified.append(target)
    overflow = diversified[policy.max_targets :]
    accepted = diversified[: policy.max_targets]
    skipped.extend(
        {"target_id": target["target_id"], "reason": "weekly-target-cap"}
        for target in overflow
    )
    tasks = [
        task
        for target in accepted
        for task in build_method_tasks(target, campaign_id, output_prefix, policy)
    ]
    created = generated_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        raise IntakeError("generated_at must be timezone-aware")
    plan = {
        "schema_version": INTAKE_SCHEMA_VERSION,
        "campaign": {
            "campaign_id": campaign_id,
            "name": f"Foldarium blind week {release_date.isoformat()}",
            "source": (
                "wwPDB Saturday prerelease"
                if intake_source == "wwpdb-prerelease"
                else "wwPDB prerelease + CAMEO selected targets"
            ),
            "release_date": release_date.isoformat(),
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "status": "intake",
            "configuration": {
                "heavy_atom_minimum": policy.heavy_atom_minimum,
                "max_targets": policy.max_targets,
                "diffusion_samples": policy.diffusion_samples,
                "msa_mode": policy.msa_mode,
                "protein_only": policy.protein_only,
                "gpu_class_override": policy.gpu_class,
                "methods": ["openfold3", "boltz2"],
                "intake_source": intake_source,
            },
        },
        "snapshot": {key: value for key, value in ww_pdb_snapshot.items() if key != "entries"},
        "targets": accepted,
        "skipped": sorted(skipped, key=lambda row: (row["target_id"], row["reason"])),
        "tasks": tasks,
        "budget": {
            "selected_targets": len(accepted),
            "gpu_tasks": len(tasks),
            "maximum_gpu_seconds": len(tasks) * policy.timeout_seconds,
            "gpu_classes": {
                name: sum(task["resources"]["gpu_class"] == name for task in tasks)
                for name in sorted({task["resources"]["gpu_class"] for task in tasks})
            },
        },
        "generated_at": created.astimezone(timezone.utc).isoformat(),
    }
    # ``generated_at`` is operational provenance, not scientific identity. A
    # retry minutes later must address the same snapshot/plan and must not create
    # a second campaign merely because the clock advanced.
    digest_input = {key: value for key, value in plan.items() if key != "generated_at"}
    plan["plan_sha256"] = hashlib.sha256(
        canonical_json(digest_input).encode("utf-8")
    ).hexdigest()
    return plan


__all__ = [
    "ADAPTER_VERSION",
    "INTAKE_SCHEMA_VERSION",
    "IntakeError",
    "WWPDB_NONPOLYMER_URL",
    "WWPDB_SEQUENCE_URL",
    "WeeklyPolicy",
    "build_weekly_plan",
    "build_method_tasks",
    "parse_wwpdb_snapshot",
    "target_from_cameo",
    "target_from_wwpdb",
]
