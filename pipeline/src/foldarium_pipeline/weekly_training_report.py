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

from .rnp_similarity import RNP_NOVELTY_THRESHOLD, RNP_STYLE_VERSION
from .training_similarity import NOVELTY_THRESHOLD, SCORER_VERSION
from .weekly_training_audit import AUDIT_FORMAT
from .weekly_training_overlays import (
    OVERLAY_MANIFEST_FORMAT,
    load_overlay_manifest,
)

REPORT_FORMAT = "foldarium.weekly-training-similarity-report/v2"
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


def pearson_correlation(
    pairs: Sequence[tuple[float, float]],
) -> float | None:
    if len(pairs) < 2:
        return None
    left_mean = sum(left for left, _right in pairs) / len(pairs)
    right_mean = sum(right for _left, right in pairs) / len(pairs)
    numerator = sum(
        (left - left_mean) * (right - right_mean)
        for left, right in pairs
    )
    left_scale = sum((left - left_mean) ** 2 for left, _right in pairs)
    right_scale = sum((right - right_mean) ** 2 for _left, right in pairs)
    denominator = (left_scale * right_scale) ** 0.5
    return numerator / denominator if denominator else None


def _average_ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda row: row[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(indexed):
        stop = start + 1
        while stop < len(indexed) and indexed[stop][1] == indexed[start][1]:
            stop += 1
        average = ((start + 1) + stop) / 2.0
        for original_index, _value in indexed[start:stop]:
            ranks[original_index] = average
        start = stop
    return ranks


def spearman_correlation(
    pairs: Sequence[tuple[float, float]],
) -> float | None:
    if len(pairs) < 2:
        return None
    left_ranks = _average_ranks([left for left, _right in pairs])
    right_ranks = _average_ranks([right for _left, right in pairs])
    return pearson_correlation(list(zip(left_ranks, right_ranks)))


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
    *,
    exact_method: str | None = None,
) -> dict[str, Any]:
    def exact_classification(exact: dict[str, Any]) -> Any:
        if exact_method is None:
            return exact.get("classification")
        result = exact.get(exact_method)
        return result.get("classification") if isinstance(result, dict) else None

    classified = [
        (exact, blind[method])
        for exact, blind in pairs
        if exact_classification(exact) in {"familiar", "novel"}
        and blind.get(method, {}).get("classification") in {"familiar", "novel"}
    ]
    class_pairs = [
        (exact_classification(exact), estimate["classification"])
        for exact, estimate in classified
    ]
    scored = [
        (exact, blind[method])
        for exact, blind in pairs
        if exact_classification(exact) in {"familiar", "novel"}
        and isinstance(blind.get(method, {}).get("score"), (int, float))
    ]
    auc_rows = [
        (exact_classification(exact) == "familiar", float(estimate["score"]))
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
                exact_classification(exact) == estimate["classification"]
                for exact, estimate in sample
            ]
        )

    def auc_metric(sample: Sequence[Any]) -> float | None:
        return roc_auc(
            [
                (
                    exact_classification(exact) == "familiar",
                    float(estimate["score"]),
                )
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


def _exact_metric(row: dict[str, Any], method: str) -> dict[str, Any]:
    if method == "pocket_aware":
        return {
            "score": row.get("train_shape_overlap"),
            "classification": row.get("classification"),
            "train_pdb": row.get("train_pdb"),
            "train_het": row.get("train_het"),
        }
    result = row.get("rnp_style_top25")
    if not isinstance(result, dict):
        return {}
    return {
        "score": result.get("sucos_shape_pocket_qcov"),
        "classification": result.get("classification"),
        "train_pdb": result.get("train_pdb"),
        "train_het": result.get("train_het"),
    }


def _selected_blind_metric(
    row: dict[str, Any], method: str
) -> dict[str, Any]:
    aggregate = row.get(method)
    if not isinstance(aggregate, dict):
        return {}
    choice_id = aggregate.get("choice_id")
    choices = row.get("choices")
    if not isinstance(choice_id, str) or not isinstance(choices, list):
        return {}
    for choice in choices:
        if (
            isinstance(choice, dict)
            and choice.get("choice_id") == choice_id
            and isinstance(choice.get(method), dict)
        ):
            return choice[method]
    return {}


def _blind_metric(row: dict[str, Any], method: str) -> dict[str, Any]:
    result = row.get(method)
    return result if isinstance(result, dict) else {}


def _metric_comparison(
    pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    methods = ("pocket_aware", "rnp_style_top25")
    availability: dict[str, dict[str, int]] = {}
    for method in methods:
        exact_available = [
            (exact, blind)
            for exact, blind in pairs
            if isinstance(_exact_metric(exact, method).get("score"), (int, float))
            and _exact_metric(exact, method).get("classification")
            in {"familiar", "novel"}
        ]
        blind_available = [
            (exact, blind)
            for exact, blind in pairs
            if isinstance(_blind_metric(blind, method).get("score"), (int, float))
            and _blind_metric(blind, method).get("classification")
            in {"familiar", "novel"}
        ]
        paired_available = [
            (exact, blind)
            for exact, blind in exact_available
            if isinstance(_blind_metric(blind, method).get("score"), (int, float))
            and _blind_metric(blind, method).get("classification")
            in {"familiar", "novel"}
        ]
        availability[method] = {
            "exact_score_count": len(exact_available),
            "blind_score_count": len(blind_available),
            "paired_score_count": len(paired_available),
        }
    comparable: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for exact, blind in pairs:
        if all(
            isinstance(_exact_metric(exact, method).get("score"), (int, float))
            and _exact_metric(exact, method).get("classification")
            in {"familiar", "novel"}
            and isinstance(
                _blind_metric(blind, method).get("score"), (int, float)
            )
            and _blind_metric(blind, method).get("classification")
            in {"familiar", "novel"}
            for method in methods
        ):
            comparable.append((exact, blind))

    exact_scores = [
        (
            float(_exact_metric(exact, "pocket_aware")["score"]),
            float(_exact_metric(exact, "rnp_style_top25")["score"]),
        )
        for exact, _blind in comparable
    ]
    exact_classes = [
        (
            _exact_metric(exact, "pocket_aware")["classification"],
            _exact_metric(exact, "rnp_style_top25")["classification"],
        )
        for exact, _blind in comparable
    ]
    metric_rows: dict[str, Any] = {}
    for method in methods:
        class_matches = [
            _exact_metric(exact, method)["classification"]
            == _blind_metric(blind, method)["classification"]
            for exact, blind in comparable
        ]
        auc_rows = [
            (
                _exact_metric(exact, method)["classification"] == "familiar",
                float(_blind_metric(blind, method)["score"]),
            )
            for exact, blind in comparable
        ]
        recovery_rows = []
        for exact, blind in comparable:
            exact_result = _exact_metric(exact, method)
            selected_result = _selected_blind_metric(blind, method)
            if isinstance(exact_result.get("train_pdb"), str) and isinstance(
                selected_result.get("train_pdb"), str
            ):
                recovery_rows.append((exact_result, selected_result))
        pdb_matches = [
            exact_result["train_pdb"] == selected_result["train_pdb"]
            for exact_result, selected_result in recovery_rows
        ]
        pdb_ligand_rows = [
            (exact_result, selected_result)
            for exact_result, selected_result in recovery_rows
            if isinstance(exact_result.get("train_het"), str)
            and isinstance(selected_result.get("train_het"), str)
        ]
        pdb_ligand_matches = [
            exact_result["train_pdb"] == selected_result["train_pdb"]
            and exact_result["train_het"] == selected_result["train_het"]
            for exact_result, selected_result in pdb_ligand_rows
        ]
        metric_rows[method] = {
            "classification_count": len(comparable),
            "blind_classification_accuracy": _round(_rate(class_matches)),
            "auroc": _round(roc_auc(auc_rows)),
            "closest_training_system_recovery": {
                "pdb_only_count": len(recovery_rows),
                "pdb_only_match_count": sum(pdb_matches),
                "pdb_only_match_rate": _round(_rate(pdb_matches)),
                "pdb_and_ligand_count": len(pdb_ligand_rows),
                "pdb_and_ligand_match_count": sum(pdb_ligand_matches),
                "pdb_and_ligand_match_rate": _round(
                    _rate(pdb_ligand_matches)
                ),
            },
        }
    agreement = [left == right for left, right in exact_classes]
    return {
        "target_pair_count": len(pairs),
        "paired_complete_count": len(comparable),
        "availability": availability,
        "thresholds": {
            "pocket_aware": NOVELTY_THRESHOLD,
            "rnp_style_top25": RNP_NOVELTY_THRESHOLD,
        },
        "threshold_provenance": {
            "pocket_aware": "fixed historical Foldarium overlap cutoff",
            "rnp_style_top25": "Runs N' Poses published 25/100 cutoff",
        },
        "exact_score_correlation": {
            "count": len(exact_scores),
            "pearson": _round(pearson_correlation(exact_scores)),
            "spearman": _round(spearman_correlation(exact_scores)),
        },
        "exact_classification_agreement": {
            "count": len(exact_classes),
            "agreement_count": sum(agreement),
            "agreement_rate": _round(_rate(agreement)),
        },
        "metrics": metric_rows,
    }


def build_report(
    exact: dict[str, Any],
    blind: dict[str, Any],
    *,
    overlay_records: dict[tuple[str, str], dict[str, Any]] | None = None,
    overlay_manifest_sha256: str | None = None,
) -> dict[str, Any]:
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
        exact_rnp = (
            row.get("rnp_style_top25")
            if isinstance(row.get("rnp_style_top25"), dict)
            else {}
        )
        blind_rnp = (
            blind_row.get("rnp_style_top25")
            if isinstance(blind_row.get("rnp_style_top25"), dict)
            else {}
        )
        selected_rnp = _selected_blind_metric(
            blind_row, "rnp_style_top25"
        )
        overlay_row = (overlay_records or {}).get(
            (row.get("blind_week"), row["item_id"])
        )
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
                "rnp_exact_score": exact_rnp.get(
                    "sucos_shape_pocket_qcov"
                ),
                "rnp_exact_classification": exact_rnp.get("classification"),
                "rnp_exact_train_pdb": exact_rnp.get("train_pdb"),
                "rnp_exact_train_het": exact_rnp.get("train_het"),
                "rnp_blind_score": blind_rnp.get("score"),
                "rnp_blind_classification": blind_rnp.get("classification"),
                "rnp_blind_choice_id": blind_rnp.get("choice_id"),
                "rnp_blind_train_pdb": selected_rnp.get("train_pdb"),
                "rnp_blind_train_het": selected_rnp.get("train_het"),
                "training_system_overlay_status": row.get(
                    "training_system_overlay_status"
                ),
                "training_system_overlay_unavailable_reason": row.get(
                    "training_system_overlay_unavailable_reason"
                ),
                "training_system_overlay": (
                    {
                        key: overlay_row[key]
                        for key in (
                            "object_uri",
                            "sha256",
                            "size_bytes",
                            "media_type",
                        )
                    }
                    if overlay_row is not None
                    else None
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
        "rnp_style_version": RNP_STYLE_VERSION,
        "training_cutoff": "2021-09-30",
        "novelty_threshold": NOVELTY_THRESHOLD,
        "rnp_style_contract": {
            "normalized_novelty_threshold": RNP_NOVELTY_THRESHOLD,
            "threshold_source": "Runs N' Poses published 25/100 cutoff",
            "candidate_universe": (
                "drug-like ligands in the same retained top-25 Foldseek PDB hits "
                "used by the Foldarium audit"
            ),
            "ligand_alignment": "Crippen O3A then RDKit rdShapeAlign",
            "ligand_similarity": (
                "0.5 * pharmacophore feature-map score + "
                "0.5 * (1 - shape protrude distance)"
            ),
            "pocket_qcov": (
                "fraction of query 6A pocket residues whose Foldseek-aligned "
                "target residue lies in the training ligand's 6A pocket"
            ),
            "combined_score": "ligand SuCOS * pocket_qcov",
            "paper_identical": False,
            "paper_difference": (
                "Runs N' Poses searches PLINDER systems across the PDB; this "
                "controlled comparison reuses Foldarium's top-25 PDB candidate set"
            ),
            "omitted_paper_components": [
                "PLINDER holo-system and proper-ligand-instance enumeration",
                "Foldseek sensitivity-11 search with up to 5000 candidates",
                "maximum of Foldseek and MMseqs directional pocket coverage",
                "PLIP interacting-residue augmentation of 6A geometric pockets",
                "multi-chain greedy matching and multi-ligand pocket denominator",
                "the recorded RDKit 2024.9.6 environment",
            ],
        },
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
        "training_system_overlays": (
            {
                "format_version": OVERLAY_MANIFEST_FORMAT,
                "manifest_sha256": overlay_manifest_sha256,
                "record_count": len(overlay_records or {}),
                "status_counts": dict(
                    sorted(
                        Counter(
                            row.get(
                                "training_system_overlay_status",
                                "unspecified",
                            )
                            for row in exact_records
                        ).items()
                    )
                ),
            }
            if overlay_records is not None
            else None
        ),
        "by_week": weeks,
        "correct_pose_availability": outcomes,
        "automated_correctness": automated,
        "blind_estimators": {
            "nearest_training_system": _method_statistics(
                pairs, "nearest_training_system"
            ),
            "pocket_aware": _method_statistics(pairs, "pocket_aware"),
            "rnp_style_top25": _method_statistics(
                pairs,
                "rnp_style_top25",
                exact_method="rnp_style_top25",
            ),
        },
        "metric_comparison": _metric_comparison(pairs),
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
    rnp_estimator = report["blind_estimators"]["rnp_style_top25"]
    comparison = report["metric_comparison"]
    pocket_comparison = comparison["metrics"]["pocket_aware"]
    rnp_comparison = comparison["metrics"]["rnp_style_top25"]
    correlations = comparison["exact_score_correlation"]
    agreement = comparison["exact_classification_agreement"]
    snapshots = report["foldseek_provenance"]["database_snapshots"]
    database = snapshots[0] if len(snapshots) == 1 else {}
    overlays = report.get("training_system_overlays")
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
        (
            f"| RnP-style top 25 | {rnp_estimator['classification_count']} | "
            f"{_format_rate(rnp_estimator['classification_accuracy'])} | "
            f"{rnp_estimator['auroc'] if rnp_estimator['auroc'] is not None else 'n/a'} | "
            f"{_format_rate(rnp_estimator['correct_pose_pick_rate'])} | "
            f"{_format_rate(rnp_estimator['pose_or_none_accuracy'])} |"
        ),
        "",
        (
            "Percentile confidence intervals in the JSON report use 2,000 "
            "deterministic target-level bootstrap samples. Thresholds were fixed "
            "before this comparison: Foldarium's historical 0.25 overlap cutoff "
            "and Runs N' Poses' published 25/100 cutoff."
        ),
        "",
        "## Parallel metric comparison",
        "",
        "| Metric | Threshold | Blind class accuracy | AUROC | Closest PDB | Closest PDB + ligand |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| Pocket-aware overlap | {comparison['thresholds']['pocket_aware']} | "
            f"{_format_rate(pocket_comparison['blind_classification_accuracy'])} | "
            f"{pocket_comparison['auroc'] if pocket_comparison['auroc'] is not None else 'n/a'} | "
            f"{_format_rate(pocket_comparison['closest_training_system_recovery']['pdb_only_match_rate'])} | "
            f"{_format_rate(pocket_comparison['closest_training_system_recovery']['pdb_and_ligand_match_rate'])} |"
        ),
        (
            f"| RnP-style top 25 | {comparison['thresholds']['rnp_style_top25']} | "
            f"{_format_rate(rnp_comparison['blind_classification_accuracy'])} | "
            f"{rnp_comparison['auroc'] if rnp_comparison['auroc'] is not None else 'n/a'} | "
            f"{_format_rate(rnp_comparison['closest_training_system_recovery']['pdb_only_match_rate'])} | "
            f"{_format_rate(rnp_comparison['closest_training_system_recovery']['pdb_and_ligand_match_rate'])} |"
        ),
        "",
        (
            f"Across {comparison['paired_complete_count']} targets scored completely by "
            f"both metrics, exact-score Pearson correlation was "
            f"{correlations['pearson'] if correlations['pearson'] is not None else 'n/a'} "
            f"and Spearman correlation was "
            f"{correlations['spearman'] if correlations['spearman'] is not None else 'n/a'}. "
            f"Exact classifications agreed for {agreement['agreement_count']} of "
            f"{agreement['count']} targets ({_format_rate(agreement['agreement_rate'])})."
        ),
        (
            f"Before restricting to that common cohort, pocket-aware overlap had "
            f"{comparison['availability']['pocket_aware']['paired_score_count']} "
            f"scored exact/blind pairs and RnP-style had "
            f"{comparison['availability']['rnp_style_top25']['paired_score_count']} "
            f"of {comparison['target_pair_count']} complete audit pairs."
        ),
        "",
        "## Scientific contract",
        "",
        "- Foldseek `pdb100` / `3diaa`, with release date strictly before 2021-09-30.",
        "- At most the first 25 retained structural neighbors.",
        "- At least four Foldseek-aligned Cα atoms within 8 Å of the query pocket.",
        "- Pocket-local Cα RMSD at most 3 Å.",
        "- Familiar when maximum carried-ligand vdW-volume overlap is at least 0.25; novel below 0.25.",
        "- The parallel RnP-style metric is familiar at or above its separately defined published 25/100 threshold and novel below it.",
        "- Canonical search, download, parse, or incomplete-candidate failures are unknown rather than novel.",
        "- RnP-style invalid ligand candidates are logged and skipped, matching its per-ligand exception isolation; query failures or zero valid candidates after failures are unknown.",
        "- This is an RnP-style controlled approximation, not the published RnP metric or a paper-identical PLINDER rerun.",
        "- It reuses Foldarium's retained top-25 PDB candidates and one Foldseek pocket correspondence; the paper uses PLINDER holo systems, up to 5,000 Foldseek hits, MMseqs coverage, PLIP-augmented pockets, multi-chain matching, and RDKit 2024.9.6.",
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
            f"- RnP-style scorer: `{report['rnp_style_version']}`",
            f"- Foldseek backend counts: `{json.dumps(report['foldseek_provenance']['backend_counts'], sort_keys=True)}`",
            f"- Foldseek release: `{database.get('foldseek_release', 'multiple or unavailable')}`",
            f"- Foldseek database downloaded: `{database.get('downloaded_at', 'multiple or unavailable')}`",
            f"- Exact audit SHA-256: `{exact_digest}`",
            f"- Blind audit SHA-256: `{blind_digest}`",
            *(
                [
                    f"- Training-system overlay manifest: `{overlays['format_version']}`",
                    f"- Training-system overlay manifest SHA-256: `{overlays['manifest_sha256']}`",
                ]
                if isinstance(overlays, dict)
                else []
            ),
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
    overlay_manifest_path: Path | None = None,
) -> dict[str, Any]:
    exact = _read_audit(exact_path, "exact")
    blind = _read_audit(blind_path, "blind")
    exact_digest = sha256(exact_path.read_bytes()).hexdigest()
    overlay_records = None
    overlay_manifest_digest = None
    if overlay_manifest_path is not None:
        try:
            overlay_records, overlay_manifest_digest = load_overlay_manifest(
                overlay_manifest_path,
                audit=exact,
                audit_digest=exact_digest,
                require_complete=True,
            )
        except Exception as exc:
            raise WeeklyTrainingReportError(
                "training-system overlay manifest is invalid"
            ) from exc
    report = build_report(
        exact,
        blind,
        overlay_records=overlay_records,
        overlay_manifest_sha256=overlay_manifest_digest,
    )
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
            "public_queue_audit_sha256": sha256(
                api_sensitivity_path.read_bytes()
            ).hexdigest(),
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
        writer = csv.DictWriter(
            handle,
            fieldnames=list(records[0]) if records else [],
            lineterminator="\n",
        )
        if records:
            writer.writeheader()
            writer.writerows(records)
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
    parser.add_argument("--overlay-manifest", type=Path)
    options = parser.parse_args(arguments)
    report = write_artifacts(
        options.exact,
        options.blind,
        options.json,
        options.csv,
        options.markdown,
        options.api_sensitivity,
        options.overlay_manifest,
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
    "pearson_correlation",
    "render_markdown",
    "roc_auc",
    "spearman_correlation",
    "write_artifacts",
]
