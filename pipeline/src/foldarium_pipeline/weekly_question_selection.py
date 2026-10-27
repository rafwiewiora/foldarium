"""Reproducible small Weekly question draws from an immutable blind population.

The default draw is uniform: predicted pose diversity is explanatory, not a
hidden sampling weight. The explicitly requested weighted mode uses at most a
2x weight. Neither mode uses experimental answers, Foldseek novelty, affinity,
confidence, or human/LLM votes. Missing novelty therefore cannot block a draw.

This module selects IDs, not a scientific benchmark population. The caller must
record whether these IDs scope human presentation or an immutable published
round. Metrics over a subset must state that denominator; preserving a full
Preview does not itself publish a full-population benchmark.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from .contracts import canonical_json
from .quiz import QuizManifestError, _assert_no_reveal_fields, manifest_sha256
from .weekly_quiz import WeeklyQuizAssemblyError, clone_weekly_quiz_manifests

DEFAULT_WEEKLY_QUESTION_COUNT = 5
QUESTION_SELECTION_POLICY = "foldarium-weekly-question-draw/v1"
INTERESTINGNESS_POLICY = "pose-cluster-gini-and-cross-method-total-variation/v1"
QUESTION_SELECTION_MODES = frozenset({"uniform", "interestingness_weighted"})


class WeeklyQuestionSelectionError(ValueError):
    """A question draw cannot be bound to a valid blind candidate population."""


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise WeeklyQuestionSelectionError(f"{field} must be a nonempty canonical string")
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    """Project only immutable blind identities and pose-cluster memberships."""

    item_id = _identifier(item.get("id"), "item.id")
    choices = []
    for choice in item["choices"]:
        choice_id = _identifier(choice.get("id"), "choice.id")
        # Missing clustering/method metadata is allowed for legacy blind rounds;
        # malformed supplied values are not treated as missing scientific data.
        method = choice.get("method")
        cluster = choice.get("cluster_id")
        if method is not None:
            method = _identifier(method, "choice.method")
        if cluster is not None:
            cluster = _identifier(cluster, "choice.cluster_id")
        choices.append({"id": choice_id, "method": method, "cluster_id": cluster})
    return {"item_id": item_id, "choices": sorted(choices, key=lambda row: row["id"])}


def _interestingness(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded descriptive difficulty signals, with explicit missingness.

    Cluster diversity is the Gini impurity of all raw choice memberships.
    Cross-method disagreement is the mean total-variation distance between
    methods' cluster-frequency distributions. Identical diffuse distributions
    have diversity but no between-method disagreement. One-method populations
    have no disagreement measurement, rather than an invented zero.
    """

    choices = candidate["choices"]
    missing = []
    diversity = None
    disagreement = None
    cluster_counts: Counter[str] = Counter()
    method_counts: dict[str, Counter[str]] = defaultdict(Counter)
    if any(choice["cluster_id"] is None for choice in choices):
        missing.append("incomplete_cluster_membership")
    else:
        cluster_counts.update(choice["cluster_id"] for choice in choices)
        diversity = 1.0 - sum((count / len(choices)) ** 2 for count in cluster_counts.values())
        if any(choice["method"] is None for choice in choices):
            missing.append("incomplete_method_identity")
        else:
            for choice in choices:
                method_counts[choice["method"]][choice["cluster_id"]] += 1
            methods = sorted(method_counts)
            distances = []
            for left_index, left in enumerate(methods):
                for right in methods[left_index + 1:]:
                    left_total = sum(method_counts[left].values())
                    right_total = sum(method_counts[right].values())
                    distances.append(0.5 * sum(
                        abs(method_counts[left][cluster] / left_total
                            - method_counts[right][cluster] / right_total)
                        for cluster in sorted(cluster_counts)
                    ))
            if distances:
                disagreement = sum(distances) / len(distances)
            else:
                missing.append("fewer_than_two_methods")
    available = [value for value in (diversity, disagreement) if value is not None]
    score = sum(available) / len(available) if available else None
    return {
        "policy": INTERESTINGNESS_POLICY,
        "score": round(score, 12) if score is not None else None,
        "cluster_diversity": round(diversity, 12) if diversity is not None else None,
        "cross_method_disagreement": round(disagreement, 12) if disagreement is not None else None,
        "missing": missing,
        "choice_count": len(choices),
        "cluster_counts": dict(sorted(cluster_counts.items())),
        "method_cluster_counts": {
            method: dict(sorted(counts.items()))
            for method, counts in sorted(method_counts.items())
        },
    }


def select_weekly_questions(
    blind_manifest: Mapping[str, Any],
    private_index: Mapping[str, Any],
    *,
    seed: str,
    question_count: int = DEFAULT_WEEKLY_QUESTION_COUNT,
    mode: str = "uniform",
    eligible_item_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a private, digest-bound audit and exact ``included_item_ids``.

    All complete items are eligible by default, including single-cluster items.
    An optional explicit eligibility subset supports already-audited upstream
    restrictions; this function never silently filters candidates by score.
    Fewer than ``question_count`` candidates returns every candidate. The source
    manifests are never modified, and selected IDs retain source display order.

    Persist the returned audit with the source's private provenance. It includes
    the full candidate projection needed to reproduce every draw key and score.
    ``source_blind_manifest_sha256`` also binds the exact source assets and
    ``source_private_index_sha256`` binds their original run/sample lineage.
    """

    seed = _identifier(seed, "seed")
    if (
        isinstance(question_count, bool)
        or not isinstance(question_count, int)
        or not 1 <= question_count <= DEFAULT_WEEKLY_QUESTION_COUNT
    ):
        raise WeeklyQuestionSelectionError("question_count must be an integer from 1 to 5")
    if not isinstance(mode, str) or mode not in QUESTION_SELECTION_MODES:
        raise WeeklyQuestionSelectionError("unknown weekly question selection mode")
    if not isinstance(blind_manifest, Mapping) or not isinstance(private_index, Mapping):
        raise WeeklyQuestionSelectionError("source manifests must be objects")
    source_round_id = _identifier(blind_manifest.get("round_id"), "source round_id")
    if blind_manifest.get("schema_version") != 1:
        raise WeeklyQuestionSelectionError("unsupported blind manifest schema")
    try:
        _assert_no_reveal_fields(blind_manifest)
        # Reuse the publication contract's complete ID/digest validation. This
        # does not bind or publish a replacement round, and never mutates input.
        clone_weekly_quiz_manifests(blind_manifest, private_index, round_id=source_round_id)
    except (QuizManifestError, WeeklyQuizAssemblyError) as exc:
        raise WeeklyQuestionSelectionError(str(exc)) from exc
    source_items = blind_manifest["items"]
    source_ids = {_identifier(item["id"], "item.id") for item in source_items}
    if eligible_item_ids is None:
        eligible = source_ids
    else:
        if isinstance(eligible_item_ids, (str, bytes)):
            raise WeeklyQuestionSelectionError("eligible_item_ids must be an iterable of IDs")
        try:
            values = list(eligible_item_ids)
        except TypeError as exc:
            raise WeeklyQuestionSelectionError("eligible_item_ids must be an iterable of IDs") from exc
        eligible = {_identifier(value, "eligible item ID") for value in values}
        if len(eligible) != len(values):
            raise WeeklyQuestionSelectionError("eligible item IDs must be unique")
        if not eligible or not eligible.issubset(source_ids):
            raise WeeklyQuestionSelectionError("eligible items must be a nonempty subset of the source")
    candidates = sorted(
        (_candidate(item) for item in source_items if item["id"] in eligible),
        key=lambda row: row["item_id"],
    )
    # The random baseline depends on candidate identities, not interestingness.
    # Changing cluster/method evidence can change weights only in weighted mode.
    population_digest = _digest([
        {"item_id": row["item_id"], "choice_ids": [choice["id"] for choice in row["choices"]]}
        for row in candidates
    ])
    rows = []
    for candidate in candidates:
        interestingness = _interestingness(candidate)
        key = _digest({
            "policy": QUESTION_SELECTION_POLICY,
            "seed": seed,
            "candidate_population_sha256": population_digest,
            "item_id": candidate["item_id"],
        })
        weight = 1.0
        if mode == "interestingness_weighted" and interestingness["score"] is not None:
            weight += interestingness["score"]
        # Exponential-race weighted sampling without replacement. The midpoint
        # is avoided: these 52-bit variates are strictly between zero and one.
        uniform = (int(key[:13], 16) + 1) / (2 ** 52 + 1)
        priority = -math.log1p(-uniform) / weight if mode != "uniform" else None
        rows.append({
            **candidate,
            "interestingness": interestingness,
            "draw_sha256": key,
            "sampling_weight": weight,
            "weighted_priority": priority,
        })
    ordered = sorted(
        rows,
        key=(lambda row: (row["draw_sha256"], row["item_id"])) if mode == "uniform"
        else (lambda row: (row["weighted_priority"], row["draw_sha256"], row["item_id"])),
    )
    selected_count = min(question_count, len(ordered))
    draw_order_ids = [row["item_id"] for row in ordered[:selected_count]]
    selected = set(draw_order_ids)
    ranks = {row["item_id"]: index for index, row in enumerate(ordered, start=1)}
    for row in rows:
        row["draw_rank"] = ranks[row["item_id"]]
        row["selected"] = row["item_id"] in selected
    audit = {
        "schema_version": 1,
        "policy": QUESTION_SELECTION_POLICY,
        "mode": mode,
        "seed": seed,
        "source_round_id": source_round_id,
        "source_blind_manifest_sha256": manifest_sha256(blind_manifest),
        "source_private_index_sha256": _digest(private_index),
        "candidate_population_sha256": population_digest,
        "candidate_evidence_sha256": _digest(candidates),
        "source_item_count": len(source_items),
        "candidate_count": len(candidates),
        "requested_question_count": question_count,
        "selected_question_count": selected_count,
        "ineligible_item_ids": sorted(source_ids - eligible),
        "included_item_ids": [item["id"] for item in source_items if item["id"] in selected],
        "draw_order_item_ids": draw_order_ids,
        "candidates": rows,
        "benchmark_scope": "selection-only; caller must declare metric population",
    }
    audit["selection_sha256"] = _digest(audit)
    return audit


def public_featured_questions(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the small, blind-safe browser marker; keep all scores private."""

    if (
        audit.get("policy") != QUESTION_SELECTION_POLICY
        or audit.get("schema_version") != 1
        or audit.get("selection_sha256")
        != _digest({key: value for key, value in audit.items() if key != "selection_sha256"})
    ):
        raise WeeklyQuestionSelectionError("question selection audit digest is invalid")
    return {
        "schema_version": 1,
        "policy": audit["policy"],
        "mode": audit["mode"],
        "seed": audit["seed"],
        "blind_manifest_sha256": audit["source_blind_manifest_sha256"],
        "candidate_population_sha256": audit["candidate_population_sha256"],
        "selection_sha256": audit["selection_sha256"],
        "source_item_count": audit["source_item_count"],
        "candidate_count": audit["candidate_count"],
        "requested_question_count": audit["requested_question_count"],
        "selected_question_count": audit["selected_question_count"],
        "item_ids": list(audit["included_item_ids"]),
    }


def _preserved_featured_marker(row: Mapping[str, Any], round_id: str) -> dict[str, Any]:
    """Validate the immutable database marker before acknowledging an old draw."""
    marker = row.get("featured_questions")
    blind = row.get("blind_manifest")
    if (row.get("round_id") != round_id or not isinstance(marker, Mapping) or not isinstance(blind, Mapping)
        or blind.get("round_id") != round_id or blind.get("schema_version") != 1):
        raise WeeklyQuestionSelectionError("existing featured selection source is invalid")
    items = blind.get("items")
    if (not isinstance(items, list) or not items or any(not isinstance(item, Mapping) for item in items)
        or isinstance(row.get("item_count"), bool) or row.get("item_count") != len(items)):
        raise WeeklyQuestionSelectionError("existing featured population is invalid")
    ids = {_identifier(item.get("id"), "item.id") for item in items}
    chosen = marker.get("item_ids")
    candidates, requested, selected = (marker.get(key) for key in
        ("candidate_count", "requested_question_count", "selected_question_count"))
    digests = (marker.get("blind_manifest_sha256"), marker.get("candidate_population_sha256"), marker.get("selection_sha256"))
    if (marker.get("schema_version") != 1 or marker.get("policy") != QUESTION_SELECTION_POLICY
        or not isinstance(marker.get("mode"), str) or marker["mode"] not in QUESTION_SELECTION_MODES
        or not isinstance(marker.get("seed"), str) or not marker["seed"].strip() or marker["seed"] != marker["seed"].strip()
        or any(not isinstance(d, str) or len(d) != 64 or any(c not in "0123456789abcdef" for c in d) for d in digests)
        or marker["blind_manifest_sha256"] != row.get("blind_manifest_sha256")
        or marker["blind_manifest_sha256"] != manifest_sha256(blind)
        or isinstance(marker.get("source_item_count"), bool) or marker.get("source_item_count") != len(items) or len(ids) != len(items)
        or any(isinstance(n, bool) or not isinstance(n, int) for n in (candidates, requested, selected))
        or not 1 <= candidates <= len(items) or not 1 <= requested <= 5 or selected != min(candidates, requested)
        or not isinstance(chosen, list) or len(chosen) != selected
        or any(not isinstance(item_id, str) or item_id not in ids for item_id in chosen)
        or len(set(chosen)) != len(chosen)):
        raise WeeklyQuestionSelectionError("existing featured selection binding is invalid")
    return dict(marker)


def freeze_weekly_featured_questions(
    coordinator: Any,
    round_id: str,
    *,
    seed: str | None = None,
    question_count: int = DEFAULT_WEEKLY_QUESTION_COUNT,
    mode: str = "uniform",
    dry_run: bool = False,
    preference_policy: str | None = None,
    expected_blind_manifest_sha256: str | None = None,
    expected_environment: str | None = None,
) -> dict[str, Any]:
    """Resolve one full published round and idempotently register its human draw.

    The artifact's exact bytes are the unsigned canonical audit, whose hash is
    ``selection_sha256``. Both its descriptor and the exact final round/private
    digests are checked by the service-only RPC. No manifests, selector kits,
    votes, or benchmark denominators are changed. A dry run performs no writes.
    A versioned ligand preference applies only to a new draw; an existing frozen
    marker is returned unchanged before chemistry or private-index work. The
    legacy default remains available for exact historical algorithm replay.
    """

    import json

    round_id = _identifier(round_id, "round_id")
    if expected_blind_manifest_sha256 is not None and (
        not isinstance(expected_blind_manifest_sha256, str) or len(expected_blind_manifest_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_blind_manifest_sha256)
    ):
        raise WeeklyQuestionSelectionError("expected blind manifest digest is invalid")
    if expected_environment is not None and expected_environment not in ("production", "preview"):
        raise WeeklyQuestionSelectionError("expected round environment is invalid")

    def require_expected_binding(row):
        if ((expected_blind_manifest_sha256 is not None and row.get("blind_manifest_sha256") != expected_blind_manifest_sha256)
            or (expected_environment is not None and row.get("environment") != expected_environment)):
            raise WeeklyQuestionSelectionError("featured source binding changed")

    if preference_policy is not None:
        from .druglike_selection import DRUGLIKE_PREFERENCE_POLICY
        if preference_policy != DRUGLIKE_PREFERENCE_POLICY:
            raise WeeklyQuestionSelectionError("unsupported featured ligand preference")
        existing = coordinator.weekly_featured_question_selection(round_id)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise WeeklyQuestionSelectionError("existing featured selection row is invalid")
            require_expected_binding(existing)
            marker = _preserved_featured_marker(existing, round_id)
            return {"status": "already-registered", "round_id": round_id,
                    "featured_questions": marker, "preserved_existing": True}
    source, private_content = coordinator.weekly_quiz_reveal_inputs(round_id)
    require_expected_binding(source)
    if source.get("round_id") != round_id or source.get("status") != "open" or source.get("reveal_manifest") is not None:
        raise WeeklyQuestionSelectionError("featured questions require the exact unrevealed weekly round")
    try:
        private_index = json.loads(private_content)
    except (TypeError, ValueError) as exc:
        raise WeeklyQuestionSelectionError("source private index is invalid JSON") from exc
    preference = None
    if preference_policy is not None:
        from .druglike_selection import assess_druglike_candidates
        preference = assess_druglike_candidates(source["blind_manifest"], private_index)
    selection = select_weekly_questions(
        source["blind_manifest"], private_index,
        seed=f"weekly-featured:{round_id}" if seed is None else seed,
        question_count=question_count, mode=mode,
        eligible_item_ids=preference["eligible_item_ids"] if preference is not None else None,
    )
    if preference is not None:
        if (preference.get("policy") != preference_policy
            or preference.get("source_blind_manifest_sha256") != selection["source_blind_manifest_sha256"]
            or preference.get("source_private_index_sha256") != selection["source_private_index_sha256"]):
            raise WeeklyQuestionSelectionError("ligand preference differs from the exact source")
        selection["ligand_preference"] = preference
        selection["selection_sha256"] = _digest({key: value for key, value in selection.items() if key != "selection_sha256"})
    private_object = source.get("metadata", {}).get("private_index", {})
    if (
        selection["source_blind_manifest_sha256"] != source.get("blind_manifest_sha256")
        or selection["source_private_index_sha256"] != private_object.get("sha256")
        or not str(private_object.get("object_uri", "")).startswith(
            f"supabase://{coordinator.storage_bucket}/sha256/"
        )
    ):
        raise WeeklyQuestionSelectionError("featured source artifact digests or private bucket differ")
    marker = public_featured_questions(selection)
    if dry_run:
        return {"status": "dry-run", "round_id": round_id, "featured_questions": marker, "audit": selection}
    canonical = canonical_json({key: value for key, value in selection.items() if key != "selection_sha256"})
    content = canonical.encode("utf-8")
    digest = selection["selection_sha256"]
    artifact = coordinator.store_bytes(content, "application/json")
    expected = {
        "object_uri": f"supabase://{coordinator.storage_bucket}/sha256/{digest[:2]}/{digest}",
        "sha256": digest,
        "size_bytes": len(content),
        "media_type": "application/json",
    }
    if not isinstance(artifact, Mapping) or any(artifact.get(key) != value for key, value in expected.items()):
        raise WeeklyQuestionSelectionError("featured audit storage result differs from its exact digest")
    receipt = coordinator.register_weekly_featured_questions(
        round_id=round_id,
        featured_questions=marker,
        selection_canonical=canonical,
        selection_artifact=expected,
    )
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("status") not in {"registered", "already-registered"}
        or receipt.get("round_id") != round_id
        or receipt.get("selection_sha256") != digest
    ):
        raise WeeklyQuestionSelectionError("featured registration receipt differs from its exact draw")
    preference_summary = ({"ligand_preference_status": preference["status"],
                           "preferred_candidate_count": preference["preferred_count"],
                           "preference_fallback_reason": preference["fallback_reason"]}
                          if preference is not None else {})
    return {"status": receipt["status"], "round_id": round_id, "featured_questions": marker,
            "selection_artifact": expected, **preference_summary}


__all__ = [
    "DEFAULT_WEEKLY_QUESTION_COUNT",
    "INTERESTINGNESS_POLICY",
    "QUESTION_SELECTION_POLICY",
    "WeeklyQuestionSelectionError",
    "select_weekly_questions",
    "public_featured_questions",
    "freeze_weekly_featured_questions",
]
