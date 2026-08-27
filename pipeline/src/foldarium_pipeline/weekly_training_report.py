"""Statistical report generation for Weekly training-similarity audits."""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from .training_similarity import NOVELTY_THRESHOLD, SCORER_VERSION
from .weekly_training_audit import AUDIT_FORMAT

REPORT_FORMAT = "foldarium.weekly-training-similarity-report/v1"
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260827


class WeeklyTrainingReportError(RuntimeError):
    """Raised when audit files cannot support a valid report."""


def _read_audit(path: Path, expected_mode: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WeeklyTrainingReportError(f"invalid audit file: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("format_version") != AUDIT_FORMAT
        or value.get("mode") != expected_mode
        or not isinstance(value.get("records"), list)
    ):
        raise WeeklyTrainingReportError(f"audit contract mismatch: {path}")
    return value


def roc_auc(labels_and_scores: Sequence[tuple[bool, float]]) -> float | None:
    """Compute AUROC as the positive-negative pair concordance probability."""

    positives = [score for label, score in labels_and_scores if label]
    negatives = [score for label, score in labels_and_scores if not label]
    if not positives or not negatives:
        return None
    concordance = 0.0
    for positive in positives:
        for negative in negatives:
            concordance += (
                1.0 if positive > negative else 0.5 if positive == negative else 0.0
            )
    return concordance / (len(positives) * len(negatives))


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise WeeklyTrainingReportError("cannot take a percentile of no values")
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def bootstrap_interval(
    rows: Sequence[Any],
    metric: Callable[[Sequence[Any]], float | None],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> list[float] | None:
    if not rows:
        return None
    rng = random.Random(seed)
    estimates: list[float] = []
    for _sample in range(samples):
        resampled = [rows[rng.randrange(len(rows))] for _index in rows]
        estimate = metric(resampled)
        if estimate is not None:
            estimates.append(float(estimate))
    if not estimates:
        return None
    return [
        round(_percentile(estimates, 0.025), 4),
        round(_percentile(estimates, 0.975), 4),
    ]


def _rate(values: Sequence[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _classification_confusion(
    pairs: Sequence[tuple[str, str]]
) -> dict[str, int]:
    return {
        f"actual_{actual}__predicted_{predicted}": sum(
            left == actual and right == predicted for left, right in pairs
        )
        for actual in ("familiar", "novel")
        for predicted in ("familiar", "novel")
    }


def _method_statistics(
    pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    method: str,
) -> dict[str, Any]:
    classified = [
        (exact, blind[method])
        for exact, blind in pairs
        if exact.get("classification") in {"familiar", "novel"}
        and blind.get(method, {}).get("classification") in {"familiar", "novel"}
    ]
    class_pairs = [
        (exact["classification"], estimate["classification"])
        for exact, estimate in classified
    ]
    scored = [
        (exact, blind[method])
        for exact, blind in pairs
        if exact.get("classification") in {"familiar", "novel"}
        and isinstance(blind.get(method, {}).get("score"), (int, float))
    ]
    auc_rows = [
        (exact["classification"] == "familiar", float(estimate["score"]))
        for exact, estimate in scored
    ]
    pose_rows = [
        (exact, blind[method])
        for exact, blind in pairs
        if isinstance(blind.get(method, {}).get("choice_id"), str)
    ]
    none_rows = [
        (exact, estimate)
        for exact, estimate in pose_rows
        if isinstance(estimate.get("predict_none"), bool)
    ]

    def classification_accuracy(sample: Sequence[Any]) -> float | None:
        return _rate(
            [
                exact["classification"] == estimate["classification"]
                for exact, estimate in sample
            ]
        )

    def auc_metric(sample: Sequence[Any]) -> float | None:
        return roc_auc(
            [
                (exact["classification"] == "familiar", float(estimate["score"]))
                for exact, estimate in sample
            ]
        )

    def pose_accuracy(sample: Sequence[Any]) -> float | None:
        return _rate(
            [
                estimate["choice_id"] in set(exact.get("correct_choice_ids", []))
                for exact, estimate in sample
            ]
        )

    def none_accuracy(sample: Sequence[Any]) -> float | None:
        return _rate(
            [
                estimate["predict_none"] is (not exact.get("has_correct_pose", False))
                for exact, estimate in sample
            ]
        )

    return {
        "paired_count": len(pairs),
        "classification_count": len(classified),
        "confusion_matrix": _classification_confusion(class_pairs),
        "classification_accuracy": _round(classification_accuracy(classified)),
        "classification_accuracy_bootstrap_95ci": bootstrap_interval(
            classified, classification_accuracy
        ),
        "continuous_score_count": len(scored),
        "auroc": _round(roc_auc(auc_rows)),
        "auroc_bootstrap_95ci": bootstrap_interval(scored, auc_metric),
        "selected_pose_count": len(pose_rows),
        "correct_pose_pick_count": sum(
            estimate["choice_id"] in set(exact.get("correct_choice_ids", []))
            for exact, estimate in pose_rows
        ),
        "correct_pose_pick_rate": _round(pose_accuracy(pose_rows)),
        "correct_pose_pick_bootstrap_95ci": bootstrap_interval(
            pose_rows, pose_accuracy
        ),
        "pose_or_none_count": len(none_rows),
        "pose_or_none_correct_count": sum(
            estimate["predict_none"] is (not exact.get("has_correct_pose", False))
            for exact, estimate in none_rows
        ),
        "pose_or_none_accuracy": _round(none_accuracy(none_rows)),
        "pose_or_none_bootstrap_95ci": bootstrap_interval(
            none_rows, none_accuracy
        ),
    }


def build_report(exact: dict[str, Any], blind: dict[str, Any]) -> dict[str, Any]:
    exact_records = [
        row for row in exact["records"] if isinstance(row, dict)
    ]
    blind_records = [
        row for row in blind["records"] if isinstance(row, dict)
    ]
    exact_by_id = {
        row["item_id"]: row
        for row in exact_records
        if isinstance(row.get("item_id"), str)
    }
    blind_by_id = {
        row["item_id"]: row
        for row in blind_records
        if isinstance(row.get("item_id"), str)
    }
    pairs = [
        (exact_by_id[item_id], blind_by_id[item_id])
        for item_id in sorted(exact_by_id.keys() & blind_by_id.keys())
        if exact_by_id[item_id].get("status") == "complete"
        and blind_by_id[item_id].get("status") == "complete"
    ]
    complete_exact = [
        row for row in exact_records if row.get("status") == "complete"
    ]
    classes = Counter(row.get("classification", "unknown") for row in exact_records)
    weeks: dict[str, Any] = {}
    for week in sorted(
        {
            row.get("blind_week")
            for row in exact_records
            if isinstance(row.get("blind_week"), str)
        }
    ):
        rows = [row for row in exact_records if row.get("blind_week") == week]
        weeks[week] = {
            "target_count": len(rows),
            "classification": dict(
                sorted(Counter(row.get("classification", "unknown") for row in rows).items())
            ),
        }
    known_exact = [
        row
        for row in complete_exact
        if row.get("classification") in {"familiar", "novel"}
    ]

    def availability_rate(sample: Sequence[Any]) -> float | None:
        return _rate([bool(item.get("has_correct_pose")) for item in sample])

    outcomes: dict[str, Any] = {}
    for classification in ("familiar", "novel", "all"):
        rows = (
            known_exact
            if classification == "all"
            else [
                row
                for row in known_exact
                if row["classification"] == classification
            ]
        )
        values = [bool(row.get("has_correct_pose")) for row in rows]
        outcomes[classification] = {
            "target_count": len(rows),
            "correct_pose_available_count": sum(values),
            "correct_pose_available_rate": _round(_rate(values)),
            "correct_pose_available_bootstrap_95ci": bootstrap_interval(
                rows, availability_rate
            ),
        }
    participants = sorted(
        {
            name
            for row in known_exact
            for name in row.get("automated_correct", {})
        }
    )
    automated: dict[str, Any] = {}
    for participant in participants:
        automated[participant] = {}

        def participant_rate(
            sample: Sequence[Any], name: str = participant
        ) -> float | None:
            return _rate(
                [bool(item["automated_correct"][name]) for item in sample]
            )

        for classification in ("familiar", "novel", "all"):
            rows = [
                row
                for row in known_exact
                if participant in row.get("automated_correct", {})
                and (
                    classification == "all"
                    or row["classification"] == classification
                )
            ]
            values = [bool(row["automated_correct"][participant]) for row in rows]
            automated[participant][classification] = {
                "target_count": len(rows),
                "correct_count": sum(values),
                "correct_rate": _round(_rate(values)),
                "correct_rate_bootstrap_95ci": bootstrap_interval(
                    rows, participant_rate
                ),
            }
    known_with_neighbor = [
        row
        for row in known_exact
        if isinstance(row.get("train_shape_overlap"), (int, float))
        and isinstance(row.get("train_pdb"), str)
    ]
    representatives = sorted(
        known_with_neighbor,
        key=lambda row: (
            row["classification"] != "familiar",
            -float(row["train_shape_overlap"]),
            row["item_id"],
        ),
    )[:10]
    compact_records = []
    for row in sorted(exact_records, key=lambda value: (value.get("blind_week", ""), value["item_id"])):
        blind_row = blind_by_id.get(row["item_id"], {})
        compact_records.append(
            {
                "week": row.get("blind_week"),
                "item_id": row["item_id"],
                "ligand": row.get("ligand_component_id"),
                "classification": row.get("classification", "unknown"),
                "reason": row.get("reason"),
                "train_pdb": row.get("train_pdb"),
                "train_het": row.get("train_het"),
                "train_identity": row.get("train_identity"),
                "train_align_rmsd": row.get("train_align_rmsd"),
                "train_shape_overlap": row.get("train_shape_overlap"),
                "has_correct_pose": row.get("has_correct_pose"),
                "nearest_score": blind_row.get("nearest_training_system", {}).get("score"),
                "nearest_classification": blind_row.get(
                    "nearest_training_system", {}
                ).get("classification"),
                "nearest_choice_id": blind_row.get("nearest_training_system", {}).get(
                    "choice_id"
                ),
                "pocket_aware_score": blind_row.get("pocket_aware", {}).get("score"),
                "pocket_aware_classification": blind_row.get("pocket_aware", {}).get(
                    "classification"
                ),
                "pocket_aware_choice_id": blind_row.get("pocket_aware", {}).get(
                    "choice_id"
                ),
            }
        )
    database_snapshots = {
        json.dumps(row["foldseek_database_snapshot"], sort_keys=True)
        for row in complete_exact
        if isinstance(row.get("foldseek_database_snapshot"), dict)
    }
    return {
        "format_version": REPORT_FORMAT,
        "scorer_version": SCORER_VERSION,
        "training_cutoff": "2021-09-30",
        "novelty_threshold": NOVELTY_THRESHOLD,
        "bootstrap": {
            "method": "target-level percentile bootstrap",
            "samples": BOOTSTRAP_SAMPLES,
            "seed": BOOTSTRAP_SEED,
        },
        "counts": {
            "exact_record_count": len(exact_records),
            "exact_complete_count": len(complete_exact),
            "blind_record_count": len(blind_records),
            "paired_complete_count": len(pairs),
            "classification": dict(sorted(classes.items())),
        },
        "foldseek_provenance": {
            "backend_counts": dict(
                sorted(
                    Counter(
                        row.get("foldseek_backend", "unknown")
                        for row in complete_exact
                    ).items()
                )
            ),
            "database_snapshots": [
                json.loads(value) for value in sorted(database_snapshots)
            ],
        },
        "by_week": weeks,
        "correct_pose_availability": outcomes,
        "automated_correctness": automated,
        "blind_estimators": {
            method: _method_statistics(pairs, method)
            for method in ("nearest_training_system", "pocket_aware")
        },
        "representative_neighbors": [
            {
                key: row.get(key)
                for key in (
                    "item_id",
                    "blind_week",
                    "classification",
                    "train_pdb",
                    "train_het",
                    "train_identity",
                    "train_align_rmsd",
                    "train_shape_overlap",
                )
            }
            for row in representatives
        ],
        "records": compact_records,
    }


def _format_rate(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{100 * value:.1f}%"


def render_markdown(report: dict[str, Any], exact_digest: str, blind_digest: str) -> str:
    counts = report["counts"]
    classification = counts["classification"]
    nearest = report["blind_estimators"]["nearest_training_system"]
    pocket = report["blind_estimators"]["pocket_aware"]
    snapshots = report["foldseek_provenance"]["database_snapshots"]
    database = snapshots[0] if len(snapshots) == 1 else {}
    lines = [
        "# Weekly training-similarity audit",
        "",
        "## Result",
        "",
        f"The post-reveal audit covers {counts['exact_record_count']} published targets. "
        f"It classified {classification.get('familiar', 0)} as familiar, "
        f"{classification.get('novel', 0)} as novel, and "
        f"{classification.get('unknown', 0)} as unknown.",
        "",
        "| Blind estimator | Classified pairs | Classification accuracy | AUROC | Correct pose pick | Pose/None accuracy |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| Nearest training system | {nearest['classification_count']} | "
            f"{_format_rate(nearest['classification_accuracy'])} | "
            f"{nearest['auroc'] if nearest['auroc'] is not None else 'n/a'} | "
            f"{_format_rate(nearest['correct_pose_pick_rate'])} | "
            f"{_format_rate(nearest['pose_or_none_accuracy'])} |"
        ),
        (
            f"| Top-25 pocket-aware | {pocket['classification_count']} | "
            f"{_format_rate(pocket['classification_accuracy'])} | "
            f"{pocket['auroc'] if pocket['auroc'] is not None else 'n/a'} | "
            f"{_format_rate(pocket['correct_pose_pick_rate'])} | "
            f"{_format_rate(pocket['pose_or_none_accuracy'])} |"
        ),
        "",
        "Percentile confidence intervals in the JSON report use 2,000 deterministic "
        "target-level bootstrap samples. The fixed historical 0.25 overlap threshold "
        "was not calibrated on these Weekly targets.",
        "",
        "## Scientific contract",
        "",
        "- Foldseek `pdb100` / `3diaa`, with release date strictly before 2021-09-30.",
        "- At most the first 25 retained structural neighbors.",
        "- At least four Foldseek-aligned Cα atoms within 8 Å of the query pocket.",
        "- Pocket-local Cα RMSD at most 3 Å.",
        "- Familiar when maximum carried-ligand vdW-volume overlap is at least 0.25; novel below 0.25.",
        "- Search, download, parse, or incomplete-candidate failures are unknown rather than novel.",
        "",
        "The exact label is retrospective: it uses the released RCSB crystal and crystal "
        "ligand. The blind estimates use only the archived predicted receptor, predicted "
        "pocket, and candidate poses. Reveal manifests, crystal structures, answer "
        "overlays, and answer RMSDs are not accepted by the blind scorer.",
        "",
        "## Weekly counts",
        "",
        "| Week | Targets | Familiar | Novel | Unknown |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for week, row in report["by_week"].items():
        classes = row["classification"]
        lines.append(
            f"| {week} | {row['target_count']} | {classes.get('familiar', 0)} | "
            f"{classes.get('novel', 0)} | {classes.get('unknown', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Observed Weekly outcomes",
            "",
            "| Exact class | Targets | Correct pose available |",
            "| --- | ---: | ---: |",
        ]
    )
    for classification in ("familiar", "novel", "all"):
        row = report["correct_pose_availability"][classification]
        lines.append(
            f"| {classification.title()} | {row['target_count']} | "
            f"{row['correct_pose_available_count']} "
            f"({_format_rate(row['correct_pose_available_rate'])}) |"
        )
    lines.extend(
        [
            "",
            "| Automated participant | Overall | Familiar | Novel |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for participant, participant_rows in report["automated_correctness"].items():
        lines.append(
            f"| {participant} | {_format_rate(participant_rows['all']['correct_rate'])} | "
            f"{_format_rate(participant_rows['familiar']['correct_rate'])} | "
            f"{_format_rate(participant_rows['novel']['correct_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## Representative nearest training ligands",
            "",
            "| Target | Week | Class | Training PDB | Ligand | Identity | Local RMSD (Å) | Overlap |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in report["representative_neighbors"]:
        lines.append(
            f"| {row['item_id']} | {row['blind_week']} | {row['classification']} | "
            f"{row['train_pdb']} | {row['train_het']} | {row['train_identity']} | "
            f"{row['train_align_rmsd']} | {row['train_shape_overlap']} |"
        )
    sensitivity = report.get("public_api_sensitivity")
    if isinstance(sensitivity, dict):
        lines.extend(
            [
                "",
                "## Public API sensitivity check",
                "",
                f"The public Foldseek queue completed {sensitivity['api_complete_count']} "
                f"of 100 targets before repeated timeouts. Among "
                f"{sensitivity['comparable_count']} targets with known labels from both "
                f"backends, {sensitivity['agreement_count']} agreed "
                f"({_format_rate(sensitivity['agreement_rate'])}; 95% target-bootstrap CI "
                f"{sensitivity['agreement_bootstrap_95ci']}). The final table uses the "
                "single version-pinned local database snapshot for every target.",
            ]
        )
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- The 2021-09-30 cutoff approximates AlphaFold 3 training availability; it does not describe every model's private or later training corpus.",
            "- Foldseek is a global structural retrieval step. Pocket-local fitting reduces, but does not eliminate, global-vs-pocket similarity mismatch.",
            "- Holo/apo state, missing residues, alternate conformers, and oligomer choice can change a pocket comparison.",
            "- The public Foldseek `pdb100` database is mutable, so cache and result digests are part of the provenance.",
            "- Ligand component filtering is heuristic. Excluded cofactors, additives, modified residues, or unusual ligands can affect the nearest analog.",
            "- The blind proxies are evaluated, not production selectors. Their candidate-pose overlap is not a calibrated probability of pose correctness.",
            "",
            "## Provenance",
            "",
            f"- Scorer: `{report['scorer_version']}`",
            f"- Foldseek backend counts: `{json.dumps(report['foldseek_provenance']['backend_counts'], sort_keys=True)}`",
            f"- Foldseek release: `{database.get('foldseek_release', 'multiple or unavailable')}`",
            f"- Foldseek database downloaded: `{database.get('downloaded_at', 'multiple or unavailable')}`",
            f"- Exact audit SHA-256: `{exact_digest}`",
            f"- Blind audit SHA-256: `{blind_digest}`",
            "- Raw structures, API responses, and resumable caches are intentionally outside Git.",
            "",
            "Per-target results and all confidence intervals are in "
            "[`weekly-training-similarity-results.json`](weekly-training-similarity-results.json) "
            "and [`weekly-training-similarity-results.csv`](weekly-training-similarity-results.csv).",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(
    exact_path: Path,
    blind_path: Path,
    json_path: Path,
    csv_path: Path,
    markdown_path: Path,
    api_sensitivity_path: Path | None = None,
) -> dict[str, Any]:
    exact = _read_audit(exact_path, "exact")
    blind = _read_audit(blind_path, "blind")
    report = build_report(exact, blind)
    if api_sensitivity_path is not None:
        api = _read_audit(api_sensitivity_path, "exact")
        final_by_id = {
            row["item_id"]: row
            for row in exact["records"]
            if row.get("classification") in {"familiar", "novel"}
        }
        api_by_id = {
            row["item_id"]: row
            for row in api["records"]
            if row.get("classification") in {"familiar", "novel"}
        }
        comparable = [
            (final_by_id[item_id], api_by_id[item_id])
            for item_id in sorted(final_by_id.keys() & api_by_id.keys())
        ]

        def agreement(sample: Sequence[Any]) -> float | None:
            return _rate(
                [
                    final["classification"] == public["classification"]
                    for final, public in sample
                ]
            )

        report["public_api_sensitivity"] = {
            "api_audit_sha256": sha256(api_sensitivity_path.read_bytes()).hexdigest(),
            "api_complete_count": sum(
                row.get("status") == "complete" for row in api["records"]
            ),
            "comparable_count": len(comparable),
            "agreement_count": sum(
                final["classification"] == public["classification"]
                for final, public in comparable
            ),
            "agreement_rate": _round(agreement(comparable)),
            "agreement_bootstrap_95ci": bootstrap_interval(
                comparable, agreement
            ),
            "confusion_matrix": _classification_confusion(
                [
                    (final["classification"], public["classification"])
                    for final, public in comparable
                ]
            ),
        }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    records = report["records"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]) if records else [])
        if records:
            writer.writeheader()
            writer.writerows(records)
    exact_digest = sha256(exact_path.read_bytes()).hexdigest()
    blind_digest = sha256(blind_path.read_bytes()).hexdigest()
    markdown_path.write_text(render_markdown(report, exact_digest, blind_digest))
    return report


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exact", required=True, type=Path)
    parser.add_argument("--blind", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--api-sensitivity", type=Path)
    options = parser.parse_args(arguments)
    report = write_artifacts(
        options.exact,
        options.blind,
        options.json,
        options.csv,
        options.markdown,
        options.api_sensitivity,
    )
    print(json.dumps(report["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BOOTSTRAP_SAMPLES",
    "REPORT_FORMAT",
    "WeeklyTrainingReportError",
    "bootstrap_interval",
    "build_report",
    "main",
    "render_markdown",
    "roc_auc",
    "write_artifacts",
]
