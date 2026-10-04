"""Private archive authorization derived from a frozen job and verified receipt.

The proof is an integrity descriptor, not a bearer credential. The database's
publication registration compares the complete source snapshot against its own
joins; a caller cannot authorize a label by constructing this object locally.
"""
from collections.abc import Mapping
import re

LEGACY_APPROVED_IDENTITIES = frozenset({"Claude Opus", "Codex GPT-5.6", "GPT-5.6 Sol"})
BENCHMARK_IDENTITY_POLICY = "foldarium.frozen-benchmark-identity/v1"
_FIELDS = frozenset({"policy", "round_id", "environment", "execution_id", "provider", "model_id", "config_sha256", "blind_manifest_sha256", "execution_sha256", "payload_digest", "artifact_sha256"})
_SHA = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def validate_benchmark_authorization(proof, *, round_id, execution_id, display_name):
    if not isinstance(proof, Mapping) or set(proof) != _FIELDS:
        raise ValueError("benchmark authorization fields are invalid")
    if (proof["policy"] != BENCHMARK_IDENTITY_POLICY
        or proof["round_id"] != round_id
        or proof["environment"] != "production"
        or proof["provider"] != "anthropic-api"
        or not isinstance(execution_id, str) or not _UUID.fullmatch(execution_id)
        or proof["execution_id"] != execution_id
        or not isinstance(display_name, str) or not display_name.strip()
        or len(display_name) > 200 or display_name != display_name.strip()
        or proof["model_id"] != display_name):
        raise ValueError("benchmark authorization identity binding is invalid")
    for field in ("config_sha256", "blind_manifest_sha256", "execution_sha256", "payload_digest", "artifact_sha256"):
        if not isinstance(proof[field], str) or not _SHA.fullmatch(proof[field]):
            raise ValueError("benchmark authorization digest is invalid")
    return dict(proof)
