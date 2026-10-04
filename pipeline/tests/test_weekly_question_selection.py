from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from copy import deepcopy
from unittest.mock import Mock, patch

from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.quiz import build_blind_manifest, manifest_sha256
from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublicationError
from foldarium_pipeline.weekly_question_selection import (
    freeze_weekly_featured_questions,
    INTERESTINGNESS_POLICY,
    WeeklyQuestionSelectionError,
    public_featured_questions,
    select_weekly_questions,
)


def population(count=12):
    return build_blind_manifest("preview-weekly-fixture", [{
        "id": f"item-{index:02d}",
        "choices": [{
            "run_id": f"run-{index}-{method}",
            "sample_id": f"sample-{sample}",
            "method": method,
            "cluster_id": f"cluster-{sample if index % 2 else 0}",
            "is_rep": sample == 0,
            "pose_uri": f"supabase://public/item-{index}/{method}/{sample}",
        } for method in ("boltz2", "openfold3") for sample in range(2)],
    } for index in range(count)])


def rebind(blind, private):
    private["blind_manifest_sha256"] = manifest_sha256(blind)


class WeeklyQuestionSelectionTests(unittest.TestCase):
    def test_default_five_is_repeatable_preserves_full_population_and_private_lineage(self):
        blind, private = population()
        original = deepcopy((blind, private))
        selection = select_weekly_questions(blind, private, seed="fixed-weekly-seed")
        self.assertEqual(selection, select_weekly_questions(blind, private, seed="fixed-weekly-seed"))
        self.assertEqual(len(selection["included_item_ids"]), 5)
        self.assertEqual(selection["candidate_count"], 12)
        self.assertEqual(selection["source_item_count"], 12)
        self.assertEqual(selection["source_private_index_sha256"], hashlib.sha256(canonical_json(private).encode()).hexdigest())
        self.assertEqual((blind, private), original)
        selected = set(selection["included_item_ids"])
        self.assertEqual(selection["included_item_ids"], [item["id"] for item in blind["items"] if item["id"] in selected])
        self.assertEqual(sum(row["selected"] for row in selection["candidates"]), 5)
        self.assertTrue(all(row["sampling_weight"] == 1 for row in selection["candidates"]))
        self.assertTrue(all(row["weighted_priority"] is None for row in selection["candidates"]))
        expected = hashlib.sha256(canonical_json({k: v for k, v in selection.items() if k != "selection_sha256"}).encode()).hexdigest()
        self.assertEqual(selection["selection_sha256"], expected)

    def test_draw_does_not_depend_on_source_item_or_choice_order(self):
        blind, private = population()
        first = select_weekly_questions(blind, private, seed="stable")
        blind["items"].reverse()
        private["items"].reverse()
        for item in blind["items"]:
            item["choices"].reverse()
        rebind(blind, private)
        second = select_weekly_questions(blind, private, seed="stable")
        self.assertEqual(first["candidate_population_sha256"], second["candidate_population_sha256"])
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertEqual(first["draw_order_item_ids"], second["draw_order_item_ids"])
        self.assertEqual(first["included_item_ids"], list(reversed(second["included_item_ids"])))

    def test_uniform_is_not_secretly_weighted_by_interestingness_or_answer_data(self):
        blind, private = population()
        first = select_weekly_questions(blind, private, seed="uniform")
        for item in blind["items"]:
            for choice in item["choices"]:
                choice["cluster_id"] = "same-cluster"
                choice["confidence"] = {"value": 99.9}
            item["metadata"] = {"training_similarity": {"status": "unknown"}}
        for item in private["items"]:
            item["clustering"] = {"oracle_correct": True}
            for choice in item["choices"]:
                choice["correct"] = True
                choice["rmsd"] = 0.0
        rebind(blind, private)
        second = select_weekly_questions(blind, private, seed="uniform")
        self.assertEqual(first["included_item_ids"], second["included_item_ids"])
        self.assertEqual(first["candidate_population_sha256"], second["candidate_population_sha256"])
        self.assertNotEqual(first["candidate_evidence_sha256"], second["candidate_evidence_sha256"])
        self.assertTrue(all(row["interestingness"]["score"] == 0 for row in second["candidates"]))

    def test_interestingness_distinguishes_diversity_from_method_disagreement(self):
        blind, private = population(3)
        for index, item in enumerate(blind["items"]):
            for number, choice in enumerate(item["choices"]):
                if index == 0:
                    choice["cluster_id"] = "single"
                elif index == 1:
                    choice["cluster_id"] = choice["method"]
                else:
                    # Each method has one choice in each cluster.
                    choice["cluster_id"] = private["items"][index]["choices"][number]["sample_id"]
        # Use choice ID to avoid depending on the public manifest shuffle.
        sample_by_id = {choice["id"]: choice["sample_id"] for choice in private["items"][2]["choices"]}
        for choice in blind["items"][2]["choices"]:
            choice["cluster_id"] = sample_by_id[choice["id"]]
        rebind(blind, private)
        audit = select_weekly_questions(blind, private, seed="scoring")
        scores = [row["interestingness"] for row in audit["candidates"]]
        self.assertEqual([row["policy"] for row in scores], [INTERESTINGNESS_POLICY] * 3)
        self.assertEqual([row["cluster_diversity"] for row in scores], [0, 0.5, 0.5])
        self.assertEqual([row["cross_method_disagreement"] for row in scores], [0, 1, 0])
        self.assertEqual([row["score"] for row in scores], [0, 0.75, 0.25])

    def test_missing_clusters_or_one_method_are_explicit_and_do_not_block_draw(self):
        blind, private = population(3)
        for choice in blind["items"][0]["choices"]:
            choice.pop("cluster_id")
        for choice in blind["items"][1]["choices"]:
            choice["method"] = "only-method"
        for choice in blind["items"][2]["choices"]:
            choice.pop("method")
        rebind(blind, private)
        audit = select_weekly_questions(blind, private, seed="missing", mode="interestingness_weighted")
        scores = [row["interestingness"] for row in audit["candidates"]]
        self.assertEqual(audit["selected_question_count"], 3)
        self.assertEqual(scores[0]["score"], None)
        self.assertEqual(scores[0]["missing"], ["incomplete_cluster_membership"])
        self.assertEqual(audit["candidates"][0]["sampling_weight"], 1)
        self.assertIsNone(scores[1]["cross_method_disagreement"])
        self.assertEqual(scores[1]["missing"], ["fewer_than_two_methods"])
        self.assertEqual(scores[2]["missing"], ["incomplete_method_identity"])
        self.assertEqual(scores[2]["score"], 0)

    def test_weighted_mode_is_explicit_bounded_repeatable_and_without_replacement(self):
        blind, private = population(20)
        audit = select_weekly_questions(blind, private, seed="weighted", mode="interestingness_weighted")
        self.assertEqual(audit, select_weekly_questions(blind, private, seed="weighted", mode="interestingness_weighted"))
        self.assertEqual(len(set(audit["included_item_ids"])), 5)
        self.assertTrue(all(1 <= row["sampling_weight"] <= 2 for row in audit["candidates"]))
        self.assertTrue(all(row["weighted_priority"] > 0 for row in audit["candidates"]))
        for item in blind["items"]:
            for choice in item["choices"]:
                choice["cluster_id"] = "single"
        rebind(blind, private)
        uniform = select_weekly_questions(blind, private, seed="equal-weights")
        weighted = select_weekly_questions(blind, private, seed="equal-weights", mode="interestingness_weighted")
        self.assertEqual(uniform["draw_order_item_ids"], weighted["draw_order_item_ids"])

    def test_changed_seed_and_candidate_identity_change_draw_reproducibly(self):
        blind, private = population()
        audit = select_weekly_questions(blind, private, seed="one")
        other = select_weekly_questions(blind, private, seed="two")
        self.assertNotEqual(audit["included_item_ids"], other["included_item_ids"])
        counts = Counter()
        for seed in range(200):
            counts.update(select_weekly_questions(blind, private, seed=str(seed))["included_item_ids"])
        # Fixed seeds make this deterministic; broad bounds catch score sorting,
        # fixed first-five selection, and systematic exclusion of easy items.
        self.assertEqual(set(counts), {item["id"] for item in blind["items"]})
        self.assertTrue(all(45 < value < 120 for value in counts.values()))
        smaller = select_weekly_questions(blind, private, seed="one", eligible_item_ids=["item-00", "item-03"])
        self.assertEqual(smaller["included_item_ids"], ["item-00", "item-03"])
        self.assertEqual(smaller["source_item_count"], 12)
        self.assertEqual(smaller["candidate_count"], 2)
        self.assertEqual(len(smaller["ineligible_item_ids"]), 10)
        self.assertNotEqual(audit["candidate_population_sha256"], smaller["candidate_population_sha256"])

    def test_public_marker_is_explicit_whitelist_bound_to_full_manifest(self):
        blind, private = population()
        audit = select_weekly_questions(blind, private, seed="public-marker")
        marker = public_featured_questions(audit)
        self.assertEqual(marker["blind_manifest_sha256"], manifest_sha256(blind))
        self.assertEqual(marker["item_ids"], audit["included_item_ids"])
        self.assertEqual(marker["source_item_count"], len(blind["items"]))
        self.assertEqual(set(marker), {
            "schema_version", "policy", "mode", "seed", "blind_manifest_sha256",
            "candidate_population_sha256", "selection_sha256", "source_item_count",
            "candidate_count", "requested_question_count", "selected_question_count", "item_ids",
        })
        audit["included_item_ids"].reverse()
        with self.assertRaisesRegex(WeeklyQuestionSelectionError, "audit digest"):
            public_featured_questions(audit)

    def test_invalid_inputs_fail_closed_without_dropping_candidates(self):
        blind, private = population()
        for options in ({"question_count": 0}, {"question_count": 6}, {"question_count": True},
                        {"mode": "novelty"}, {"seed": " "}, {"eligible_item_ids": []},
                        {"eligible_item_ids": "item-00"}, {"eligible_item_ids": ["unknown"]},
                        {"eligible_item_ids": ["item-00", "item-00"]}):
            with self.subTest(options=options), self.assertRaises(WeeklyQuestionSelectionError):
                select_weekly_questions(blind, private, **({"seed": "bad"} | options))
        for mutation in ("digest", "missing_choice", "empty_item", "malformed_cluster", "revealed"):
            changed, index = deepcopy((blind, private))
            if mutation == "digest":
                index["blind_manifest_sha256"] = "0" * 64
            elif mutation == "missing_choice":
                index["items"][0]["choices"].pop()
            elif mutation == "empty_item":
                changed["items"][0]["choices"] = []
                rebind(changed, index)
            elif mutation == "malformed_cluster":
                changed["items"][0]["choices"][0]["cluster_id"] = 3
                rebind(changed, index)
            else:
                changed["items"][0]["choices"][0]["correct"] = True
                rebind(changed, index)
            with self.subTest(mutation=mutation), self.assertRaises(WeeklyQuestionSelectionError):
                select_weekly_questions(changed, index, seed="bad")


class FeaturedFreezeAdapterTests(unittest.TestCase):
    def coordinator(self):
        blind, private = population()
        content = canonical_json(private).encode()
        digest = hashlib.sha256(content).hexdigest()
        source = {
            "round_id": blind["round_id"], "status": "open", "reveal_manifest": None,
            "blind_manifest": blind, "blind_manifest_sha256": manifest_sha256(blind),
            "metadata": {"private_index": {
                "sha256": digest,
                "object_uri": f"supabase://private/sha256/{digest[:2]}/{digest}",
            }},
        }
        coordinator = Mock(storage_bucket="private")
        coordinator.weekly_quiz_reveal_inputs.return_value = (source, content)
        def store(content, media_type):
            digest = hashlib.sha256(content).hexdigest()
            return {"sha256": digest, "object_uri": f"supabase://private/sha256/{digest[:2]}/{digest}",
                    "size_bytes": len(content), "media_type": media_type}
        coordinator.store_bytes.side_effect = store
        coordinator.register_weekly_featured_questions.side_effect = lambda **kw: {
            "status": "registered", "round_id": kw["round_id"],
            "selection_sha256": kw["featured_questions"]["selection_sha256"],
        }
        return coordinator, source

    def test_dry_run_no_writes_and_exact_private_artifact_on_freeze(self):
        coordinator, source = self.coordinator()
        before = deepcopy(source)
        preview = freeze_weekly_featured_questions(coordinator, source["round_id"], dry_run=True)
        coordinator.store_bytes.assert_not_called()
        coordinator.register_weekly_featured_questions.assert_not_called()
        result = freeze_weekly_featured_questions(coordinator, source["round_id"])
        self.assertEqual(result["featured_questions"], preview["featured_questions"])
        self.assertEqual(source, before)
        content, media = coordinator.store_bytes.call_args.args
        self.assertEqual(media, "application/json")
        self.assertEqual(hashlib.sha256(content).hexdigest(), result["featured_questions"]["selection_sha256"])
        self.assertEqual(json.loads(content), {key: value for key, value in preview["audit"].items() if key != "selection_sha256"})
        payload = coordinator.register_weekly_featured_questions.call_args.kwargs
        self.assertEqual(payload["selection_canonical"].encode(), content)
        self.assertEqual(payload["selection_artifact"], result["selection_artifact"])
        coordinator.register_weekly_featured_questions.side_effect = lambda **kw: {
            "status": "already-registered", "round_id": kw["round_id"],
            "selection_sha256": kw["featured_questions"]["selection_sha256"],
        }
        self.assertEqual(freeze_weekly_featured_questions(coordinator, source["round_id"])["status"], "already-registered")

    def test_wrong_round_source_or_storage_digests_and_receipts_fail_closed(self):
        for broken in ("revealed", "blind_digest", "private_digest", "public_bucket", "storage", "receipt"):
            coordinator, source = self.coordinator()
            if broken == "revealed": source["reveal_manifest"] = {}
            if broken == "blind_digest": source["blind_manifest_sha256"] = "0" * 64
            if broken == "private_digest": source["metadata"]["private_index"]["sha256"] = "0" * 64
            if broken == "public_bucket": coordinator.storage_bucket = "public"
            if broken == "storage": coordinator.store_bytes.side_effect = lambda *args: {"sha256": "0" * 64}
            if broken == "receipt": coordinator.register_weekly_featured_questions.side_effect = lambda **kwargs: {"status": "registered"}
            with self.subTest(broken=broken), self.assertRaises(WeeklyQuestionSelectionError):
                freeze_weekly_featured_questions(coordinator, source["round_id"])
            if broken not in ("storage", "receipt"):
                coordinator.store_bytes.assert_not_called()
            if broken != "receipt":
                coordinator.register_weekly_featured_questions.assert_not_called()

    def test_supabase_adapter_binds_exact_canonical_bytes_before_rpc(self):
        fake, source = self.coordinator()
        frozen = freeze_weekly_featured_questions(fake, source["round_id"])
        payload = fake.register_weekly_featured_questions.call_args.kwargs
        coordinator = SupabaseCoordinator("https://project.supabase.co", "fixture-key", "private")
        with patch.object(SupabaseCoordinator, "_rpc", return_value={"status": "registered"}) as rpc:
            coordinator.register_weekly_featured_questions(**payload)
            rpc_name, arguments = rpc.call_args.args
            self.assertEqual(rpc_name, "register_weekly_featured_questions")
            self.assertEqual(arguments["p_selection_canonical"], payload["selection_canonical"])
            self.assertEqual(arguments["p_featured_questions"], frozen["featured_questions"])
            rpc.reset_mock()
            payload["selection_canonical"] += " "
            with self.assertRaisesRegex(SupabasePublicationError, "artifact digest"):
                coordinator.register_weekly_featured_questions(**payload)
            rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
