"""Generate tests/fixtures/private-evaluation-v5.golden.json for JS contract tests.

Run from the pipeline directory:

    .venv/bin/python tests/generate_private_evaluation_v2_golden.py
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_private_evaluation import (
    FakeCoordinator,
    coordinate,
    minimal_reference_mmcif_gz,
    overlay_evaluation_score,
    round_fixture,
)
from foldarium_pipeline.private_evaluation import (
    PRODUCTION_BETA_CATCHUP_ROUND_ID,
    describe_private_evaluation_artifact,
    materialize_private_preclose_evaluation,
)
from foldarium_pipeline.wednesday_reveal import rcsb_reference_url
import gzip


def main() -> None:
    round_record, _private, private_content = round_fixture()
    coordinator = FakeCoordinator(round_record, private_content)
    reference_content = minimal_reference_mmcif_gz()

    def reference(item):
        return coordinate(reference_content, rcsb_reference_url(item["target_id"]))

    def evaluator(*args, **kwargs):
        return overlay_evaluation_score()

    with tempfile.TemporaryDirectory() as temporary:
        outcome = materialize_private_preclose_evaluation(
            PRODUCTION_BETA_CATCHUP_ROUND_ID,
            temporary,
            coordinator=coordinator,
            reference_resolver=reference,
            evaluator=evaluator,
            now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
        )

    artifact_bytes = coordinator.stored_contents[0]
    descriptor = describe_private_evaluation_artifact(artifact_bytes)
    descriptor["artifact_object_uri"] = outcome["artifact"]["object_uri"]
    live_round = deepcopy(round_record)
    live_round.pop("blind_manifest", None)
    live_round["item_count"] = descriptor["item_count"]
    payload = {
        "_generated_by": "pipeline/tests/generate_private_evaluation_v2_golden.py",
        "artifact_base64": base64.b64encode(artifact_bytes).decode("ascii"),
        "descriptor": descriptor,
        "liveRound": live_round,
    }
    output = Path(__file__).resolve().parents[2] / "tests/fixtures/private-evaluation-v5.golden.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    print(descriptor["artifact_sha256"])


if __name__ == "__main__":
    main()
