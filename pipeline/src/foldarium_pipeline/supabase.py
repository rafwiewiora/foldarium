"""Small Supabase control-plane and artifact publication adapter.

The GPU worker produces local, checksummed artifacts.  This module is the only
place that knows how to publish those artifacts to Supabase: it verifies every
file, writes it to a content-addressed object path without upserting, and only
then asks Postgres to finish the run in one transaction.

No Supabase SDK is required.  Keeping the boundary to ordinary HTTP makes the
same worker usable from Modal, GCP, and local integration tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .contracts import canonical_json, stable_id
from .staging import build_run_row

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SupabaseConfigurationError(ValueError):
    """Raised when the explicitly supplied Supabase configuration is unsafe."""


class SupabasePublicationError(RuntimeError):
    """Raised when verification or a sanitized Supabase request fails."""


def _safe_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SupabasePublicationError(f"{field} must be a safe identifier")
    return value


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupabasePublicationError(f"{field} must be an object")
    copied = deepcopy(dict(value))
    try:
        json.dumps(copied, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SupabasePublicationError(f"{field} must contain finite JSON values") from exc
    return copied


class SupabasePublisher:
    """Publish verified worker results using a Supabase service-role credential.

    Credentials are accepted only through the explicit constructor or
    :meth:`from_env`.  They are held in a private slot, excluded from ``repr``,
    and used only in request headers.
    """

    __slots__ = (
        "_base_url",
        "_service_role_key",
        "storage_bucket",
        "_opener",
        "_timeout_seconds",
    )

    def __init__(
        self,
        supabase_url: str,
        service_role_key: str,
        storage_bucket: str,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = self._validate_base_url(supabase_url)
        if not isinstance(service_role_key, str) or not service_role_key.strip():
            raise SupabaseConfigurationError("service_role_key must be a non-empty string")
        if not isinstance(storage_bucket, str) or not _BUCKET.fullmatch(storage_bucket):
            raise SupabaseConfigurationError("storage_bucket must be a safe bucket identifier")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise SupabaseConfigurationError("timeout_seconds must be a positive number")
        if timeout_seconds <= 0:
            raise SupabaseConfigurationError("timeout_seconds must be a positive number")
        self._service_role_key = service_role_key
        self.storage_bucket = storage_bucket
        self._opener = opener or urlopen
        self._timeout_seconds = float(timeout_seconds)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = 60.0,
    ) -> "SupabasePublisher":
        """Construct from the three named deployment secrets/config values."""

        source = os.environ if environ is None else environ
        names = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "FOLDARIUM_STORAGE_BUCKET")
        missing = [name for name in names if not source.get(name)]
        if missing:
            raise SupabaseConfigurationError(
                "missing required environment variables: " + ", ".join(missing)
            )
        return cls(
            source["SUPABASE_URL"],
            source["SUPABASE_SERVICE_ROLE_KEY"],
            source["FOLDARIUM_STORAGE_BUCKET"],
            opener=opener,
            timeout_seconds=timeout_seconds,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(supabase_url={self._base_url!r}, "
            f"storage_bucket={self.storage_bucket!r}, service_role_key=<redacted>)"
        )

    @staticmethod
    def _validate_base_url(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SupabaseConfigurationError("supabase_url must be a non-empty HTTPS origin")
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise SupabaseConfigurationError(
                "supabase_url must be an HTTPS origin without credentials, path, query, or fragment"
            )
        # Rebuild from parsed pieces and discard a cosmetic trailing slash.  All
        # request paths below are fixed by this adapter, never supplied by a run.
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    def claim_run(self, task_id: str, worker_id: str, lease_seconds: int = 900) -> bool:
        """Atomically claim a pending/retryable run for this worker."""

        task_id = _safe_identifier(task_id, "task_id")
        worker_id = _safe_identifier(worker_id, "worker_id")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise SupabasePublicationError("lease_seconds must be a positive integer")
        if not 60 <= lease_seconds <= 86_400:
            raise SupabasePublicationError("lease_seconds must be from 60 to 86400")
        response = self._rpc(
            "claim_prediction_run",
            {
                "p_run_id": task_id,
                "p_worker_id": worker_id,
                "p_lease_seconds": lease_seconds,
            },
        )
        if isinstance(response, bool):
            return response
        # PostgREST can return a scalar function as either the scalar itself or
        # a one-row representation depending on request/return settings.
        if isinstance(response, list) and len(response) == 1 and isinstance(response[0], bool):
            return response[0]
        if isinstance(response, Mapping):
            for key in ("claim_prediction_run", "claimed"):
                if isinstance(response.get(key), bool):
                    return bool(response[key])
            if response.get("run_id") == task_id and response.get("status") == "running":
                return True
        if (
            isinstance(response, list)
            and len(response) == 1
            and isinstance(response[0], Mapping)
            and response[0].get("run_id") == task_id
            and response[0].get("status") == "running"
        ):
            return True
        raise SupabasePublicationError("claim_prediction_run returned an unexpected response")

    def publish_result(
        self,
        result: Mapping[str, Any],
        artifact_root: str | Path,
        worker_id: str,
    ) -> Any:
        """Upload all artifacts, then atomically finish a terminal run.

        ``artifact_root`` is the method adapter's output directory: every
        artifact ``relative_path`` is resolved beneath it and re-hashed before
        any network request.  A failed verification performs no uploads or RPC.
        """

        normalized = _json_object(result, "result")
        run_id = _safe_identifier(normalized.get("task_id"), "result.task_id")
        worker_id = _safe_identifier(worker_id, "worker_id")
        status = normalized.get("status")
        if status == "failed":
            payload = {
                "p_run_id": run_id,
                "p_worker_id": worker_id,
                "p_result": normalized,
                "p_artifacts": [],
            }
            self._encode_json(payload)
            return self._rpc("finish_prediction_run", payload)
        if status != "succeeded":
            raise SupabasePublicationError("result status must be succeeded or failed")
        root = Path(artifact_root).resolve(strict=True)
        if not root.is_dir():
            raise SupabasePublicationError("artifact_root must be a directory")

        samples = normalized.get("samples")
        if not isinstance(samples, list) or not samples:
            raise SupabasePublicationError("result.samples must be a non-empty list")

        prepared: list[tuple[Path, str, str, dict[str, Any]]] = []
        rpc_artifacts: list[dict[str, Any]] = []
        rpc_samples: list[dict[str, Any]] = []
        unique_rows: set[tuple[str, str, str]] = set()

        # Verify the complete result before the first upload.  This prevents a
        # late traversal or digest error from leaving a partial publication.
        for sample_index, raw_sample in enumerate(samples):
            sample = _json_object(raw_sample, f"result.samples[{sample_index}]")
            sample_id = _safe_identifier(
                sample.get("sample_id"), f"result.samples[{sample_index}].sample_id"
            )
            raw_artifacts = sample.pop("artifacts", None)
            if not isinstance(raw_artifacts, list) or not raw_artifacts:
                raise SupabasePublicationError(
                    f"result.samples[{sample_index}].artifacts must be a non-empty list"
                )
            rpc_samples.append(sample)
            for artifact_index, raw_artifact in enumerate(raw_artifacts):
                field = f"result.samples[{sample_index}].artifacts[{artifact_index}]"
                item = _json_object(raw_artifact, field)
                role = _safe_identifier(item.get("role"), f"{field}.role")
                digest = item.get("sha256")
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise SupabasePublicationError(f"{field}.sha256 must be a lowercase SHA-256")
                source, relative_path = self._local_artifact(root, item.get("relative_path"), field)
                actual_digest, size_bytes = self._hash_file(source)
                if actual_digest != digest:
                    raise SupabasePublicationError(f"{field} does not match its declared SHA-256")
                declared_size = item.get("size_bytes")
                if declared_size is not None and (
                    isinstance(declared_size, bool)
                    or not isinstance(declared_size, int)
                    or declared_size != size_bytes
                ):
                    raise SupabasePublicationError(f"{field}.size_bytes does not match the file")
                media_type = item.get("media_type", "application/octet-stream")
                if not isinstance(media_type, str) or not media_type or len(media_type) > 255:
                    raise SupabasePublicationError(f"{field}.media_type must be a short string")
                metadata = _json_object(item.get("metadata", {}), f"{field}.metadata")
                metadata["source_relative_path"] = relative_path
                row_identity = (sample_id, role, digest)
                if row_identity in unique_rows:
                    raise SupabasePublicationError(f"{field} duplicates an artifact row")
                unique_rows.add(row_identity)
                object_path = self._object_path(digest)
                object_uri = f"supabase://{self.storage_bucket}/{object_path}"
                artifact_row = {
                    "artifact_id": stable_id(
                        "artifact",
                        {
                            "run_id": run_id,
                            "sample_id": sample_id,
                            "role": role,
                            "sha256": digest,
                        },
                    ),
                    "sample_id": sample_id,
                    "role": role,
                    "relative_path": relative_path,
                    "object_uri": object_uri,
                    "sha256": digest,
                    "size_bytes": size_bytes,
                    "media_type": media_type,
                    "metadata": metadata,
                }
                prepared.append((source, digest, media_type, artifact_row))
                rpc_artifacts.append(artifact_row)

        rpc_result = {key: value for key, value in normalized.items() if key != "samples"}
        rpc_result["samples"] = rpc_samples

        finish_payload = {
            "p_run_id": run_id,
            "p_worker_id": worker_id,
            "p_result": rpc_result,
            "p_artifacts": rpc_artifacts,
        }
        # Refuse accidental credential serialization before creating even a
        # content-addressed object.
        self._encode_json(finish_payload)

        uploaded: set[str] = set()
        for source, digest, media_type, _artifact_row in prepared:
            if digest in uploaded:
                continue
            self._upload(source, digest, media_type)
            uploaded.add(digest)

        return self._rpc("finish_prediction_run", finish_payload)

    @staticmethod
    def _local_artifact(root: Path, relative: Any, field: str) -> tuple[Path, str]:
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or "\x00" in relative
        ):
            raise SupabasePublicationError(f"{field}.relative_path must be a safe relative path")
        posix = PurePosixPath(relative)
        if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
            raise SupabasePublicationError(f"{field}.relative_path must stay below artifact_root")
        source = (root / Path(*posix.parts)).resolve(strict=True)
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise SupabasePublicationError(
                f"{field}.relative_path must stay below artifact_root"
            ) from exc
        if not source.is_file():
            raise SupabasePublicationError(f"{field}.relative_path must name a regular file")
        return source, posix.as_posix()

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _object_path(digest: str) -> str:
        return f"sha256/{digest[:2]}/{digest}"

    def _upload(self, source: Path, digest: str, media_type: str) -> None:
        object_path = self._object_path(digest)
        endpoint = (
            "/storage/v1/object/"
            + quote(self.storage_bucket, safe="")
            + "/"
            + quote(object_path, safe="/")
        )
        response = self._request(
            endpoint,
            source.read_bytes(),
            operation="artifact upload",
            content_type=media_type,
            extra_headers={"x-upsert": "false"},
            allow_conflict=True,
        )
        if response is None:
            self._verify_existing_object(digest)

    def _verify_existing_object(self, digest: str) -> None:
        object_path = self._object_path(digest)
        endpoint = (
            "/storage/v1/object/authenticated/"
            + quote(self.storage_bucket, safe="")
            + "/"
            + quote(object_path, safe="/")
        )
        body = self._request(
            endpoint,
            None,
            operation="existing artifact verification",
            method="GET",
        )
        if body is None or hashlib.sha256(body).hexdigest() != digest:
            raise SupabasePublicationError(
                "existing content-addressed artifact does not match its object key"
            )

    def _rpc(self, function: str, payload: Mapping[str, Any]) -> Any:
        encoded = self._encode_json(payload)
        body = self._request(
            "/rest/v1/rpc/" + quote(function, safe=""),
            encoded,
            operation=function,
            content_type="application/json",
            extra_headers={"Accept": "application/json", "Prefer": "return=representation"},
        )
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupabasePublicationError(f"{function} returned invalid JSON") from exc

    def _encode_json(self, payload: Mapping[str, Any]) -> bytes:
        try:
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SupabasePublicationError("Supabase RPC payload must be finite JSON") from exc
        secret = self._service_role_key.encode("utf-8")
        if secret and secret in encoded:
            raise SupabasePublicationError("refusing to serialize a credential in an RPC payload")
        return encoded

    def _request(
        self,
        endpoint: str,
        body: bytes | None,
        *,
        operation: str,
        content_type: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        allow_conflict: bool = False,
        method: str = "POST",
    ) -> bytes | None:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": "Bearer " + self._service_role_key,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        headers.update(extra_headers or {})
        request = Request(self._base_url + endpoint, data=body, headers=headers, method=method)
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
        except HTTPError as exc:
            if allow_conflict and exc.code == 409:
                exc.close()
                return None
            status = exc.code
            exc.close()
            raise SupabasePublicationError(f"{operation} failed with HTTP {status}") from None
        except (URLError, TimeoutError, OSError):
            raise SupabasePublicationError(f"{operation} request failed") from None

        try:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            data = response.read()
        except (URLError, TimeoutError, OSError):
            raise SupabasePublicationError(f"{operation} response failed") from None
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        if status is not None and not 200 <= int(status) < 300:
            if allow_conflict and int(status) == 409:
                return None
            raise SupabasePublicationError(f"{operation} failed with HTTP {status}")
        return data


class SupabaseCoordinator(SupabasePublisher):
    """Register immutable weekly inputs before an execution backend may claim them.

    The coordinator uses the same explicit service-role boundary as publication,
    but it never executes a model.  Source snapshots and normalized target
    packages are uploaded first; one Postgres RPC then records the snapshot,
    campaign, targets, and runs transactionally.
    """

    __slots__ = ()

    def _get_json_rows(self, endpoint: str, operation: str) -> list[dict[str, Any]]:
        body = self._request(endpoint, None, operation=operation, method="GET")
        try:
            value = json.loads((body or b"[]").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupabasePublicationError(f"{operation} returned invalid JSON") from exc
        if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
            raise SupabasePublicationError(f"{operation} returned an invalid row set")
        return [deepcopy(dict(row)) for row in value]

    def weekly_campaign_exists(self, campaign_id: str) -> bool:
        """Return whether an immutable weekly campaign is already registered.

        Scheduled intake checks call this before downloading the public source
        bundle.  Once one tick has registered a week, later ticks therefore do
        not re-crawl CAMEO or submit the same GPU work again.
        """

        campaign_id = _safe_identifier(campaign_id, "campaign_id")
        query = urlencode(
            {
                "select": "campaign_id",
                "campaign_id": f"eq.{campaign_id}",
                "limit": "1",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/campaigns?{query}", "weekly campaign preflight"
        )
        if len(rows) > 1:
            raise SupabasePublicationError(
                "weekly campaign preflight returned duplicate campaign rows"
            )
        return bool(rows)

    def record_curation_decisions(
        self, decisions: list[Mapping[str, Any]]
    ) -> Any:
        """Record private, immutable selection or rejection decisions."""

        if not isinstance(decisions, list) or not decisions:
            raise SupabasePublicationError("curation decisions must be a non-empty list")
        normalized = [
            _json_object(decision, f"curation decisions[{index}]")
            for index, decision in enumerate(decisions)
        ]
        for index, decision in enumerate(normalized):
            for field in ("decision_id", "source", "stage", "target_id"):
                _safe_identifier(decision.get(field), f"curation decisions[{index}].{field}")
            outcome = decision.get("decision")
            reason = decision.get("reason")
            if not isinstance(outcome, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", outcome):
                raise SupabasePublicationError("curation decision outcome is invalid")
            if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
                raise SupabasePublicationError("curation decision reason is invalid")
            digest = decision.get("input_sha256")
            if digest is not None and (
                not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            ):
                raise SupabasePublicationError("curation decision input_sha256 is invalid")
            _json_object(decision.get("metrics", {}), "curation decision metrics")
            _json_object(decision.get("provenance", {}), "curation decision provenance")
        return self._rpc("record_curation_decisions", {"p_decisions": normalized})

    def campaign_prediction_outputs(self, campaign_id: str) -> list[dict[str, Any]]:
        """Return succeeded run/sample rows with their private complex artifacts."""

        campaign_id = _safe_identifier(campaign_id, "campaign_id")
        run_query = urlencode(
            {
                "select": "run_id,target_id,method,method_version,task_payload,result,status",
                "campaign_id": f"eq.{campaign_id}",
                "status": "eq.succeeded",
                "order": "target_id.asc,method.asc,run_id.asc",
            }
        )
        runs = self._get_json_rows(
            f"/rest/v1/prediction_runs?{run_query}", "campaign prediction query"
        )
        if not runs:
            return []
        run_ids = [_safe_identifier(row.get("run_id"), "prediction run_id") for row in runs]
        run_filter = "in.(" + ",".join(run_ids) + ")"
        artifact_query = urlencode(
            {
                "select": "run_id,sample_id,role,object_uri,sha256,media_type",
                "run_id": run_filter,
                "role": "eq.predicted_complex",
                "order": "run_id.asc,sample_id.asc",
            }
        )
        artifacts = self._get_json_rows(
            f"/rest/v1/prediction_artifacts?{artifact_query}",
            "campaign artifact query",
        )
        by_sample: dict[tuple[str, str], dict[str, Any]] = {}
        for artifact in artifacts:
            run_id = _safe_identifier(artifact.get("run_id"), "artifact.run_id")
            sample_id = _safe_identifier(artifact.get("sample_id"), "artifact.sample_id")
            digest = artifact.get("sha256")
            if artifact.get("role") != "predicted_complex" or not isinstance(
                digest, str
            ) or not _SHA256.fullmatch(digest):
                raise SupabasePublicationError("campaign artifact metadata is invalid")
            key = (run_id, sample_id)
            if key in by_sample:
                raise SupabasePublicationError("a prediction sample has multiple complex artifacts")
            by_sample[key] = artifact

        normalized: list[dict[str, Any]] = []
        for row in runs:
            run_id = _safe_identifier(row.get("run_id"), "prediction run_id")
            task = _json_object(row.get("task_payload"), "prediction task_payload")
            result = _json_object(row.get("result"), "prediction result")
            samples = result.get("samples")
            if not isinstance(samples, list) or not samples:
                raise SupabasePublicationError("succeeded prediction result has no samples")
            output_samples: list[dict[str, Any]] = []
            for raw_sample in samples:
                sample = _json_object(raw_sample, "prediction sample")
                sample_id = _safe_identifier(sample.get("sample_id"), "prediction sample_id")
                artifact = by_sample.pop((run_id, sample_id), None)
                if artifact is None:
                    raise SupabasePublicationError(
                        "succeeded prediction sample has no complex artifact"
                    )
                sample["predicted_complex"] = artifact
                output_samples.append(sample)
            normalized.append({**row, "task_payload": task, "result": result, "samples": output_samples})
        if by_sample:
            raise SupabasePublicationError("complex artifacts reference unknown result samples")
        return normalized

    def download_content_object(
        self, object_uri: str, *, expected_sha256: str | None = None
    ) -> bytes:
        """Download and verify one private content-addressed object from this bucket."""

        if not isinstance(object_uri, str):
            raise SupabasePublicationError("object_uri must be a Supabase object URI")
        parsed = urlsplit(object_uri)
        object_path = parsed.path.lstrip("/")
        match = re.fullmatch(r"sha256/([0-9a-f]{2})/([0-9a-f]{64})", object_path)
        if (
            parsed.scheme != "supabase"
            or parsed.netloc != self.storage_bucket
            or parsed.query
            or parsed.fragment
            or match is None
            or match.group(1) != match.group(2)[:2]
        ):
            raise SupabasePublicationError("object_uri is not a valid object in this bucket")
        digest = match.group(2)
        if expected_sha256 is not None and expected_sha256 != digest:
            raise SupabasePublicationError("object_uri digest does not match expected_sha256")
        endpoint = (
            "/storage/v1/object/authenticated/"
            + quote(self.storage_bucket, safe="")
            + "/"
            + quote(object_path, safe="/")
        )
        body = self._request(endpoint, None, operation="artifact download", method="GET")
        if body is None or hashlib.sha256(body).hexdigest() != digest:
            raise SupabasePublicationError("downloaded artifact does not match its object digest")
        return body

    def store_bytes(self, content: bytes, media_type: str) -> dict[str, Any]:
        """Store bytes by digest without overwriting an existing object."""

        if not isinstance(content, bytes) or not content:
            raise SupabasePublicationError("stored content must be non-empty bytes")
        if not isinstance(media_type, str) or not media_type or len(media_type) > 255:
            raise SupabasePublicationError("media_type must be a short string")
        digest = hashlib.sha256(content).hexdigest()
        object_path = self._object_path(digest)
        endpoint = (
            "/storage/v1/object/"
            + quote(self.storage_bucket, safe="")
            + "/"
            + quote(object_path, safe="/")
        )
        response = self._request(
            endpoint,
            content,
            operation="source snapshot upload",
            content_type=media_type,
            extra_headers={"x-upsert": "false"},
            allow_conflict=True,
        )
        if response is None:
            self._verify_existing_object(digest)
        return {
            "object_uri": f"supabase://{self.storage_bucket}/{object_path}",
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
        }

    def register_weekly_plan(
        self,
        plan: Mapping[str, Any],
        source_files: Mapping[str, tuple[bytes, str]],
        *,
        adapter_version: str,
        execution_backend: str = "modal",
        max_attempts: int = 2,
    ) -> Any:
        """Upload replay inputs and atomically register a non-empty weekly plan."""

        normalized = _json_object(plan, "plan")
        campaign = _json_object(normalized.get("campaign"), "plan.campaign")
        targets = normalized.get("targets")
        tasks = normalized.get("tasks")
        plan_digest = normalized.get("plan_sha256")
        if not isinstance(targets, list) or not targets or not all(
            isinstance(target, Mapping) for target in targets
        ):
            raise SupabasePublicationError("plan.targets must be a non-empty array")
        if not isinstance(tasks, list) or not tasks or not all(
            isinstance(task, Mapping) for task in tasks
        ):
            raise SupabasePublicationError("plan.tasks must be a non-empty array")
        if not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest):
            raise SupabasePublicationError("plan.plan_sha256 must be a lowercase SHA-256")
        unhashed = {
            key: value
            for key, value in normalized.items()
            if key not in {"plan_sha256", "generated_at"}
        }
        actual_plan_digest = hashlib.sha256(canonical_json(unhashed).encode("utf-8")).hexdigest()
        if actual_plan_digest != plan_digest:
            raise SupabasePublicationError("plan.plan_sha256 does not match the plan")
        if not isinstance(adapter_version, str) or not adapter_version.strip():
            raise SupabasePublicationError("adapter_version must be a non-empty string")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise SupabasePublicationError("max_attempts must be a positive integer")

        stored_sources: dict[str, dict[str, Any]] = {}
        for name, item in sorted(source_files.items()):
            _safe_identifier(name, "source file name")
            if not isinstance(item, tuple) or len(item) != 2:
                raise SupabasePublicationError("source files must map to (bytes, media_type)")
            stored_sources[name] = self.store_bytes(item[0], item[1])

        packages: dict[str, dict[str, Any]] = {}
        target_rows: list[dict[str, Any]] = []
        campaign_id = _safe_identifier(campaign.get("campaign_id"), "campaign_id")
        release_date = campaign.get("release_date")
        for raw_target in targets:
            target = deepcopy(dict(raw_target))
            target_id = _safe_identifier(target.get("target_id"), "target_id")
            encoded = canonical_json(target).encode("utf-8")
            package = self.store_bytes(encoded, "application/json")
            packages[target_id] = package
            target_rows.append(
                {
                    "target_id": target_id,
                    "campaign_id": campaign_id,
                    "source_id": target_id,
                    "source_release_date": release_date,
                    "package_uri": package["object_uri"],
                    "package_sha256": package["sha256"],
                    "package_schema_version": 1,
                    "input_summary": {
                        "schema_version": target.get("schema_version"),
                        "entities": [
                            {
                                "type": entity.get("type"),
                                "chain_ids": entity.get("chain_ids"),
                            }
                            for entity in target.get("entities", [])
                        ],
                    },
                    "metadata": target.get("metadata", {}),
                }
            )

        run_rows: list[dict[str, Any]] = []
        for task in tasks:
            target_id = str(task.get("target", {}).get("target_id", ""))
            package = packages.get(target_id)
            if package is None:
                raise SupabasePublicationError("every task target must be present in plan.targets")
            row = build_run_row(
                task,
                adapter_version=adapter_version,
                execution_backend=execution_backend,
                input_uri=package["object_uri"],
                max_attempts=max_attempts,
            )
            if row["input_sha256"] != package["sha256"]:
                raise SupabasePublicationError("target package digest does not match staged input")
            run_rows.append(row)

        campaign_configuration = _json_object(
            campaign.get("configuration", {}), "campaign.configuration"
        )
        intake_source = _safe_identifier(
            campaign_configuration.get("intake_source", "cameo-prerelease"),
            "campaign.configuration.intake_source",
        )
        campaign_row = {
            "campaign_id": campaign_id,
            "name": campaign.get("name"),
            "source": campaign.get("source"),
            "release_date": release_date,
            "selection_policy_version": campaign.get("selection_policy_version"),
            "configuration": campaign_configuration,
            "status": "predicting",
            "metadata": {
                "weekly_plan_sha256": plan_digest,
                "generated_at": normalized.get("generated_at"),
                "budget": normalized.get("budget", {}),
            },
        }
        decisions: list[dict[str, Any]] = []
        decision_provenance = {
            "plan_sha256": plan_digest,
            "selection_policy_version": campaign.get("selection_policy_version"),
            "adapter_version": adapter_version,
        }
        for target in targets:
            target_id = _safe_identifier(target.get("target_id"), "target_id")
            decisions.append(
                {
                    "decision_id": stable_id(
                        "curation",
                        {
                            "source": intake_source,
                            "stage": "weekly-intake",
                            "target_id": target_id,
                            "plan_sha256": plan_digest,
                        },
                    ),
                    "source": intake_source,
                    "stage": "weekly-intake",
                    "target_id": target_id,
                    "decision": "selected",
                    "reason": "selected-by-bounded-weekly-policy",
                    "input_sha256": plan_digest,
                    "metrics": {
                        "selected_ligand": target.get("metadata", {}).get(
                            "selected_ligand"
                        ),
                        "cameo_label": target.get("metadata", {}).get("cameo_label"),
                    },
                    "provenance": decision_provenance,
                }
            )
        skipped = normalized.get("skipped", [])
        if not isinstance(skipped, list) or not all(
            isinstance(row, Mapping) for row in skipped
        ):
            raise SupabasePublicationError("plan.skipped must be an array of objects")
        for row in skipped:
            target_id = _safe_identifier(row.get("target_id"), "skipped target_id")
            reason = row.get("reason")
            if not isinstance(reason, str) or not reason or len(reason) > 500:
                raise SupabasePublicationError("skipped target reason is invalid")
            decisions.append(
                {
                    "decision_id": stable_id(
                        "curation",
                        {
                            "source": intake_source,
                            "stage": "weekly-intake",
                            "target_id": target_id,
                            "reason": reason,
                            "plan_sha256": plan_digest,
                        },
                    ),
                    "source": intake_source,
                    "stage": "weekly-intake",
                    "target_id": target_id,
                    "decision": "rejected",
                    "reason": reason,
                    "input_sha256": plan_digest,
                    "metrics": {},
                    "provenance": decision_provenance,
                }
            )
        decisions.sort(key=lambda row: (row["target_id"], row["decision"], row["reason"]))
        snapshot = {
            "snapshot_id": stable_id("snapshot", {"plan_sha256": plan_digest}),
            "campaign_id": campaign_id,
            "release_date": release_date,
            "plan_sha256": plan_digest,
            "files": stored_sources,
            "metadata": {
                **_json_object(normalized.get("snapshot", {}), "plan.snapshot"),
                "selection_decisions": decisions,
            },
        }
        payload = {
            "p_snapshot": snapshot,
            "p_campaign": campaign_row,
            "p_targets": target_rows,
            "p_runs": run_rows,
        }
        self._encode_json(payload)
        return self._rpc("register_weekly_prediction_plan", payload)

    def append_weekly_plan(
        self,
        plan: Mapping[str, Any],
        *,
        adapter_version: str,
        execution_backend: str = "modal",
        max_attempts: int = 1,
    ) -> dict[str, Any]:
        """Append previously capped tasks to an immutable registered campaign.

        Saturday's first bounded launch may deliberately register only a small
        pilot.  Once that pilot is approved, this path reuses the exact stored
        campaign and prerelease snapshot while adding only target/run identities
        that are absent. Existing rows are never reset or updated.
        """

        normalized = _json_object(plan, "plan")
        campaign = _json_object(normalized.get("campaign"), "plan.campaign")
        targets = normalized.get("targets")
        tasks = normalized.get("tasks")
        plan_digest = normalized.get("plan_sha256")
        if not isinstance(targets, list) or not targets or not all(
            isinstance(target, Mapping) for target in targets
        ):
            raise SupabasePublicationError("plan.targets must be a non-empty array")
        if not isinstance(tasks, list) or not tasks or not all(
            isinstance(task, Mapping) for task in tasks
        ):
            raise SupabasePublicationError("plan.tasks must be a non-empty array")
        if not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest):
            raise SupabasePublicationError("plan.plan_sha256 must be a lowercase SHA-256")
        unhashed = {
            key: value
            for key, value in normalized.items()
            if key not in {"plan_sha256", "generated_at"}
        }
        actual_digest = hashlib.sha256(canonical_json(unhashed).encode("utf-8")).hexdigest()
        if actual_digest != plan_digest:
            raise SupabasePublicationError("plan.plan_sha256 does not match the plan")
        if not isinstance(adapter_version, str) or not adapter_version.strip():
            raise SupabasePublicationError("adapter_version must be a non-empty string")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise SupabasePublicationError("max_attempts must be a positive integer")

        campaign_id = _safe_identifier(campaign.get("campaign_id"), "campaign_id")
        campaign_query = urlencode(
            {
                "select": (
                    "campaign_id,name,source,release_date,selection_policy_version,"
                    "configuration,status,metadata"
                ),
                "campaign_id": f"eq.{campaign_id}",
                "limit": "1",
            }
        )
        stored_campaigns = self._get_json_rows(
            f"/rest/v1/campaigns?{campaign_query}", "weekly expansion campaign preflight"
        )
        if len(stored_campaigns) != 1:
            raise SupabasePublicationError("weekly expansion requires one existing campaign")
        snapshot_query = urlencode(
            {
                "select": "snapshot_id,campaign_id,release_date,plan_sha256,files,metadata",
                "campaign_id": f"eq.{campaign_id}",
                "order": "created_at.desc",
                "limit": "1",
            }
        )
        stored_snapshots = self._get_json_rows(
            f"/rest/v1/prerelease_snapshots?{snapshot_query}",
            "weekly expansion snapshot preflight",
        )
        if len(stored_snapshots) != 1:
            raise SupabasePublicationError("weekly expansion requires one existing snapshot")

        run_query = urlencode(
            {
                "select": "run_id",
                "target_id": "in.(" + ",".join(
                    sorted(
                        {
                            _safe_identifier(
                                task.get("target", {}).get("target_id"), "task target_id"
                            )
                            for task in tasks
                        }
                    )
                ) + ")",
            }
        )
        existing_runs = {
            _safe_identifier(row.get("run_id"), "stored run_id")
            for row in self._get_json_rows(
                f"/rest/v1/prediction_runs?{run_query}",
                "weekly expansion run preflight",
            )
        }
        new_tasks = [deepcopy(dict(task)) for task in tasks if task.get("task_id") not in existing_runs]
        if not new_tasks:
            return {
                "status": "already-registered",
                "campaign_id": campaign_id,
                "registered_run_ids": [],
            }
        new_target_ids = {
            _safe_identifier(task.get("target", {}).get("target_id"), "task target_id")
            for task in new_tasks
        }
        target_by_id = {
            _safe_identifier(target.get("target_id"), "target_id"): deepcopy(dict(target))
            for target in targets
        }
        if not new_target_ids.issubset(target_by_id):
            raise SupabasePublicationError("every new task target must be present in plan.targets")

        target_rows: list[dict[str, Any]] = []
        packages: dict[str, dict[str, Any]] = {}
        release_date = campaign.get("release_date")
        for target_id in sorted(new_target_ids):
            target = target_by_id[target_id]
            encoded = canonical_json(target).encode("utf-8")
            package = self.store_bytes(encoded, "application/json")
            packages[target_id] = package
            target_rows.append(
                {
                    "target_id": target_id,
                    "campaign_id": campaign_id,
                    "source_id": target_id,
                    "source_release_date": release_date,
                    "package_uri": package["object_uri"],
                    "package_sha256": package["sha256"],
                    "package_schema_version": 1,
                    "input_summary": {
                        "schema_version": target.get("schema_version"),
                        "entities": [
                            {
                                "type": entity.get("type"),
                                "chain_ids": entity.get("chain_ids"),
                            }
                            for entity in target.get("entities", [])
                        ],
                    },
                    "metadata": target.get("metadata", {}),
                }
            )

        run_rows: list[dict[str, Any]] = []
        for task in new_tasks:
            if task.get("campaign_id") != campaign_id:
                raise SupabasePublicationError("new task campaign does not match stored campaign")
            target_id = str(task.get("target", {}).get("target_id", ""))
            package = packages[target_id]
            row = build_run_row(
                task,
                adapter_version=adapter_version,
                execution_backend=execution_backend,
                input_uri=package["object_uri"],
                max_attempts=max_attempts,
            )
            if row["input_sha256"] != package["sha256"]:
                raise SupabasePublicationError("target package digest does not match staged input")
            run_rows.append(row)

        response = self._rpc(
            "register_weekly_prediction_plan",
            {
                "p_snapshot": stored_snapshots[0],
                "p_campaign": stored_campaigns[0],
                "p_targets": target_rows,
                "p_runs": run_rows,
            },
        )
        decisions = [
            {
                "decision_id": stable_id(
                    "curation",
                    {
                        "source": "wwpdb-prerelease",
                        "stage": "prospective-expansion",
                        "target_id": target_id,
                        "plan_sha256": plan_digest,
                    },
                ),
                "source": "wwpdb-prerelease",
                "stage": "prospective-expansion",
                "target_id": target_id,
                "decision": "selected",
                "reason": "selected-for-full-prerelease-blind-round",
                "input_sha256": plan_digest,
                "metrics": {},
                "provenance": {
                    "adapter_version": adapter_version,
                    "plan_sha256": plan_digest,
                },
            }
            for target_id in sorted(new_target_ids)
        ]
        self.record_curation_decisions(decisions)
        return {
            "status": "registered",
            "campaign_id": campaign_id,
            "target_count": len(target_rows),
            "run_count": len(run_rows),
            "registered_run_ids": [row["run_id"] for row in run_rows],
            "registration": response,
        }

    def open_weekly_quiz_round(
        self,
        *,
        round_id: str,
        campaign_id: str,
        opens_at: str,
        closes_at: str,
        blind_manifest: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """Atomically expose a pre-redacted blind manifest for its voting window."""

        from .quiz import manifest_sha256

        payload = {
            "p_round_id": _safe_identifier(round_id, "round_id"),
            "p_campaign_id": _safe_identifier(campaign_id, "campaign_id"),
            "p_opens_at": opens_at,
            "p_closes_at": closes_at,
            "p_blind_manifest": _json_object(blind_manifest, "blind_manifest"),
            "p_blind_manifest_sha256": manifest_sha256(blind_manifest),
            "p_metadata": _json_object(metadata or {}, "metadata"),
        }
        return self._rpc("open_weekly_quiz_round", payload)

    def reveal_weekly_quiz_round(
        self,
        *,
        round_id: str,
        reveal_manifest: Mapping[str, Any],
    ) -> Any:
        """Publish answers only after Postgres verifies that voting is closed."""

        from .quiz import manifest_sha256

        payload = {
            "p_round_id": _safe_identifier(round_id, "round_id"),
            "p_reveal_manifest": _json_object(reveal_manifest, "reveal_manifest"),
            "p_reveal_manifest_sha256": manifest_sha256(reveal_manifest),
        }
        return self._rpc("reveal_weekly_quiz_round", payload)

    def register_external_prediction_set(
        self,
        *,
        target_id: str,
        import_manifest: Mapping[str, Any],
        source_page: bytes,
        artifacts: list[Mapping[str, Any]],
    ) -> Any:
        """Store downloaded CAMEO files and register their public provenance."""

        manifest = _json_object(import_manifest, "import_manifest")
        provider = _safe_identifier(manifest.get("provider"), "provider")
        method = _safe_identifier(manifest.get("method"), "method")
        provider_target = _safe_identifier(
            manifest.get("provider_target_id"), "provider_target_id"
        )
        source_uri = manifest.get("source_page")
        license_name = manifest.get("license")
        if not isinstance(source_uri, str) or not source_uri.startswith("https://cameo3d.org/"):
            raise SupabasePublicationError("external source page must be a CAMEO HTTPS URL")
        if not isinstance(license_name, str) or not license_name:
            raise SupabasePublicationError("external import license is required")
        prepared_artifacts: list[dict[str, Any]] = []
        for index, raw in enumerate(artifacts):
            if not isinstance(raw, Mapping):
                raise SupabasePublicationError(f"artifacts[{index}] must be an object")
            # ``content`` is deliberately bytes and therefore not JSON. Validate
            # every serializable sub-field below, then omit content from the RPC.
            artifact = deepcopy(dict(raw))
            role = artifact.get("role")
            if role not in {"prediction", "reference"}:
                raise SupabasePublicationError("external artifact role must be prediction/reference")
            content = artifact.get("content")
            media_type = artifact.get("media_type", "chemical/x-mmcif")
            source = artifact.get("source_uri")
            if not isinstance(content, bytes) or not content:
                raise SupabasePublicationError("external artifact content must be non-empty bytes")
            if not isinstance(media_type, str) or not media_type or len(media_type) > 255:
                raise SupabasePublicationError("external artifact media_type must be a short string")
            if not isinstance(source, str) or not source.startswith("https://cameo3d.org/"):
                raise SupabasePublicationError("external artifact source must be a CAMEO HTTPS URL")
            model_index = artifact.get("model_index") if role == "prediction" else None
            assembly_id = artifact.get("assembly_id") if role == "reference" else None
            if role == "prediction" and (
                isinstance(model_index, bool)
                or not isinstance(model_index, int)
                or not 1 <= model_index <= 5
            ):
                raise SupabasePublicationError("prediction model_index must be from 1 to 5")
            if role == "reference" and (
                isinstance(assembly_id, bool)
                or not isinstance(assembly_id, int)
                or assembly_id < 1
            ):
                raise SupabasePublicationError("reference assembly_id must be positive")
            prepared_artifacts.append(
                {
                    **artifact,
                    "role": role,
                    "content": content,
                    "media_type": media_type,
                    "source_uri": source,
                    "model_index": model_index,
                    "assembly_id": assembly_id,
                    "metadata": _json_object(artifact.get("metadata", {}), "artifact.metadata"),
                }
            )

        page_object = self.store_bytes(source_page, "text/html")
        set_id = stable_id(
            "external",
            {
                "target_id": target_id,
                "provider": provider,
                "method": method,
                "provider_target_id": provider_target,
                "source_page_sha256": page_object["sha256"],
            },
        )
        rows: list[dict[str, Any]] = [
            {
                "artifact_id": stable_id(
                    "external_artifact", {"external_set_id": set_id, "role": "source_page"}
                ),
                "external_set_id": set_id,
                "role": "source_page",
                "model_index": None,
                "assembly_id": None,
                **page_object,
                "source_uri": source_uri,
                "metadata": {},
            }
        ]
        for artifact in prepared_artifacts:
            role = artifact["role"]
            stored = self.store_bytes(artifact["content"], artifact["media_type"])
            model_index = artifact["model_index"]
            assembly_id = artifact["assembly_id"]
            identity = {
                "external_set_id": set_id,
                "role": role,
                "model_index": model_index,
                "assembly_id": assembly_id,
                "sha256": stored["sha256"],
            }
            rows.append(
                {
                    "artifact_id": stable_id("external_artifact", identity),
                    "external_set_id": set_id,
                    "role": role,
                    "model_index": model_index,
                    "assembly_id": assembly_id,
                    **stored,
                    "source_uri": artifact["source_uri"],
                    "metadata": artifact["metadata"],
                }
            )
        set_row = {
            "external_set_id": set_id,
            "target_id": _safe_identifier(target_id, "target_id"),
            "provider": provider,
            "method": method,
            "provider_server_id": manifest.get("provider_server_id"),
            "provider_target_id": provider_target,
            "source_page_uri": page_object["object_uri"],
            "source_page_sha256": page_object["sha256"],
            "license": license_name,
            "import_manifest": manifest,
            "status": "imported",
        }
        return self._rpc(
            "register_external_prediction_set",
            {"p_set": set_row, "p_artifacts": rows},
        )


__all__ = [
    "SupabaseConfigurationError",
    "SupabaseCoordinator",
    "SupabasePublicationError",
    "SupabasePublisher",
]
