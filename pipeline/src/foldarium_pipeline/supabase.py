"""Small Supabase control-plane and artifact publication adapter.

The GPU worker produces local, checksummed artifacts.  This module is the only
place that knows how to publish those artifacts to Supabase: it verifies every
file, writes it to a content-addressed object path without upserting, and only
then asks Postgres to finish the run in one transaction.

No Supabase SDK is required.  Keeping the boundary to ordinary HTTP makes the
same worker usable across local and remote execution backends.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .contracts import canonical_json, stable_id
from .staging import build_run_row

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_STORAGE_ERROR_BYTES = 16 * 1024
IMMUTABLE_PUBLIC_CACHE_CONTROL = "public, max-age=31536000, immutable"

# Retrying a prediction is a metered state transition, not ordinary queue
# maintenance. Keep the authorization deliberately narrower than the worker's
# complete error taxonomy: the legacy code is needed for runs completed before
# MSA archive failures received their own classification, while the specific
# code covers newly classified failures. GPU OOM and every other failure mode
# are intentionally absent.
TRANSIENT_BOLTZ_MSA_RETRY_ERROR_CODES = frozenset(
    {"msa_preprocessing_failed", "output_validation_failed"}
)
MAX_TRANSIENT_BOLTZ_MSA_RETRY_RUNS = 10
MAX_PREDICTION_RETRY_RUNS = 80
PREDICTION_RETRY_KINDS = frozenset(
    {
        "gpu_out_of_memory",
        "msa_generation_timeout",
        "msa_preprocessing_failed",
        "repeat_once",
    }
)
WEEKLY_QUIZ_ENVIRONMENTS = frozenset({"production", "preview", "development"})
PRIVATE_WEEKLY_EVALUATION_FIELDS = (
    "evaluation_id",
    "round_id",
    "campaign_id",
    "environment",
    "round_opens_at",
    "round_closes_at",
    "blind_manifest_sha256",
    "private_index_sha256",
    "reveal_manifest_sha256",
    "reference_set_sha256",
    "prediction_set_sha256",
    "format_version",
    "evaluator_versions",
    "reveal_policy_version",
    "acceptance_policy_version",
    "correct_rmsd_threshold_angstrom",
    "item_count",
    "choice_count",
    "artifact_object_uri",
    "artifact_sha256",
    "artifact_size_bytes",
    "artifact_media_type",
)
WEEKLY_RETROSPECTIVE_PUBLICATION_FIELDS = (
    "publication_id",
    "round_id",
    "campaign_id",
    "environment",
    "format_version",
    "evaluation_id",
    "evaluation_format_version",
    "round_opens_at",
    "round_closes_at",
    "round_revealed_at",
    "blind_manifest_sha256",
    "private_index_sha256",
    "reveal_manifest_sha256",
    "reference_set_sha256",
    "prediction_set_sha256",
    "evaluation_artifact_sha256",
    "item_count",
    "choice_count",
    "source_snapshot_object_uri",
    "source_snapshot_sha256",
    "source_snapshot_size_bytes",
    "source_snapshot_media_type",
    "public_artifact_object_uri",
    "public_artifact_sha256",
    "public_artifact_size_bytes",
    "public_artifact_media_type",
    "admin_artifact_object_uri",
    "admin_artifact_sha256",
    "admin_artifact_size_bytes",
    "admin_artifact_media_type",
)


class SupabaseConfigurationError(ValueError):
    """Raised when the explicitly supplied Supabase configuration is unsafe."""


class SupabasePublicationError(RuntimeError):
    """Raised when verification or a sanitized Supabase request fails."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


def _safe_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SupabasePublicationError(f"{field} must be a safe identifier")
    return value


def _weekly_quiz_environment(value: Any) -> str:
    environment = _safe_identifier(value, "weekly quiz environment")
    if environment not in WEEKLY_QUIZ_ENVIRONMENTS:
        raise SupabasePublicationError(
            "weekly quiz environment must be production, preview, or development"
        )
    return environment


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupabasePublicationError(f"{field} must be an object")
    copied = deepcopy(dict(value))
    try:
        json.dumps(copied, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SupabasePublicationError(f"{field} must contain finite JSON values") from exc
    return copied


def _is_storage_duplicate_response(status: int, body: bytes) -> bool:
    """Recognize only Supabase's known HTTP-400 duplicate-object envelope.

    Storage has returned the legacy JSON envelope below with either an HTTP 400
    or 409 status. Ordinary 400 responses must remain failures. HTTP 409 keeps
    its standard conflict meaning; both paths still return to callers only
    after the content-addressed object has been downloaded and re-hashed.
    """

    if status == 409:
        return True
    if status != 400 or not body or len(body) > _MAX_STORAGE_ERROR_BYTES:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, Mapping)
        and str(payload.get("statusCode")) == "409"
        and payload.get("error") == "Duplicate"
        and isinstance(payload.get("message"), str)
        and payload["message"].strip().casefold() == "the resource already exists"
    )


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
            status = exc.code
            error_body = b""
            if allow_conflict and status == 400:
                try:
                    error_body = exc.read(_MAX_STORAGE_ERROR_BYTES + 1)
                except (AttributeError, OSError):
                    error_body = b""
            if allow_conflict and _is_storage_duplicate_response(status, error_body):
                exc.close()
                return None
            exc.close()
            raise SupabasePublicationError(
                f"{operation} failed with HTTP {status}",
                http_status=status,
            ) from None
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
            if allow_conflict and _is_storage_duplicate_response(int(status), data):
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

    def _get_all_json_rows(
        self,
        endpoint: str,
        operation: str,
        *,
        page_size: int = 1000,
        maximum_rows: int = 100_000,
    ) -> list[dict[str, Any]]:
        """Read a bounded complete PostgREST row set using explicit ranges."""

        rows: list[dict[str, Any]] = []
        for offset in range(0, maximum_rows, page_size):
            body = self._request(
                endpoint,
                None,
                operation=operation,
                method="GET",
                extra_headers={"Range": f"{offset}-{offset + page_size - 1}"},
            )
            try:
                value = json.loads((body or b"[]").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SupabasePublicationError(
                    f"{operation} returned invalid JSON"
                ) from exc
            if not isinstance(value, list) or not all(
                isinstance(row, Mapping) for row in value
            ):
                raise SupabasePublicationError(
                    f"{operation} returned an invalid row set"
                )
            page = [deepcopy(dict(row)) for row in value]
            rows.extend(page)
            if len(page) < page_size:
                return rows
        raise SupabasePublicationError(f"{operation} exceeded the bounded row limit")

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

    def latest_prior_prerelease_snapshot(
        self, release_date: str
    ) -> dict[str, Any] | None:
        """Return the authoritative source hashes from the newest earlier week."""

        if not isinstance(release_date, str):
            raise SupabasePublicationError("prerelease release_date must be an ISO date")
        try:
            requested_date = date.fromisoformat(release_date)
        except ValueError as exc:
            raise SupabasePublicationError(
                "prerelease release_date must be an ISO date"
            ) from exc
        query = urlencode(
            {
                "select": (
                    "snapshot_id,campaign_id,release_date,files,metadata,created_at"
                ),
                "release_date": f"lt.{release_date}",
                "order": "release_date.desc,created_at.desc",
                "limit": "1",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/prerelease_snapshots?{query}",
            "prior prerelease snapshot query",
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise SupabasePublicationError(
                "prior prerelease snapshot query returned multiple rows"
            )
        row = rows[0]
        snapshot_id = _safe_identifier(row.get("snapshot_id"), "snapshot_id")
        campaign_id = _safe_identifier(row.get("campaign_id"), "campaign_id")
        stored_release_date = row.get("release_date")
        if not isinstance(stored_release_date, str):
            raise SupabasePublicationError(
                "prior prerelease snapshot has an invalid release_date"
            )
        try:
            prior_date = date.fromisoformat(stored_release_date)
        except ValueError as exc:
            raise SupabasePublicationError(
                "prior prerelease snapshot has an invalid release_date"
            ) from exc
        if prior_date >= requested_date:
            raise SupabasePublicationError(
                "prior prerelease snapshot is not strictly earlier than the requested week"
            )
        files = _json_object(row.get("files"), "prior prerelease snapshot files")
        metadata = _json_object(
            row.get("metadata"), "prior prerelease snapshot metadata"
        )
        result: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "campaign_id": campaign_id,
            "release_date": stored_release_date,
            "created_at": row.get("created_at"),
        }
        for source_name, metadata_prefix in (
            ("wwpdb_sequence", "sequence"),
            ("wwpdb_nonpolymer", "nonpolymer"),
        ):
            descriptor = _json_object(
                files.get(source_name),
                f"prior prerelease snapshot files.{source_name}",
            )
            digest = descriptor.get("sha256")
            metadata_digest = metadata.get(f"{metadata_prefix}_sha256")
            if (
                not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
                or metadata_digest != digest
            ):
                raise SupabasePublicationError(
                    f"prior prerelease snapshot has inconsistent {metadata_prefix} SHA-256"
                )
            rows_value = metadata.get(f"{metadata_prefix}_rows")
            if (
                isinstance(rows_value, bool)
                or not isinstance(rows_value, int)
                or rows_value < 1
            ):
                raise SupabasePublicationError(
                    f"prior prerelease snapshot has invalid {metadata_prefix} row count"
                )
            result[f"{metadata_prefix}_sha256"] = digest
            result[f"{metadata_prefix}_rows"] = rows_value
        return result

    def campaign_prediction_run_statuses(
        self, campaign_id: str
    ) -> list[dict[str, Any]]:
        """Return every prediction-run state for one exact campaign."""

        campaign_id = _safe_identifier(campaign_id, "campaign_id")
        target_query = urlencode(
            {
                "select": "target_id",
                "campaign_id": f"eq.{campaign_id}",
                "order": "target_id.asc",
            }
        )
        campaign_targets = self._get_json_rows(
            f"/rest/v1/targets?{target_query}", "campaign run target query"
        )
        if not campaign_targets:
            return []
        target_ids = [
            _safe_identifier(row.get("target_id"), "campaign run target_id")
            for row in campaign_targets
        ]
        run_query = urlencode(
            {
                "select": (
                    "run_id,target_id,method,status,attempt_count,max_attempts,"
                    "error_code,task_payload,result"
                ),
                "target_id": "in.(" + ",".join(target_ids) + ")",
                "order": "target_id.asc,method.asc,run_id.asc",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/prediction_runs?{run_query}", "campaign run status query"
        )
        seen: set[str] = set()
        for row in rows:
            run_id = _safe_identifier(row.get("run_id"), "campaign run_id")
            if run_id in seen:
                raise SupabasePublicationError(
                    "campaign run status query returned duplicate run rows"
                )
            seen.add(run_id)
            if row.get("target_id") not in target_ids:
                raise SupabasePublicationError(
                    "campaign run status query crossed the campaign boundary"
                )
            if row.get("status") not in {
                "pending",
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
            }:
                raise SupabasePublicationError(
                    f"campaign run {run_id} has an invalid status"
                )
            _json_object(row.get("task_payload"), "campaign run task_payload")
            if row.get("result") is not None:
                _json_object(row.get("result"), "campaign run result")
        return rows

    def weekly_quiz_round_exists(self, round_id: str) -> bool:
        """Return whether one exact immutable weekly round already exists."""

        round_id = _safe_identifier(round_id, "round_id")
        query = urlencode(
            {
                "select": "round_id",
                "round_id": f"eq.{round_id}",
                "limit": "2",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/weekly_quiz_rounds?{query}",
            "weekly quiz round existence query",
        )
        if len(rows) > 1:
            raise SupabasePublicationError(
                "weekly quiz round existence query returned duplicate rows"
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
        target_query = urlencode(
            {
                "select": "target_id",
                "campaign_id": f"eq.{campaign_id}",
                "order": "target_id.asc",
            }
        )
        campaign_targets = self._get_json_rows(
            f"/rest/v1/targets?{target_query}", "campaign target query"
        )
        if not campaign_targets:
            return []
        target_ids = [
            _safe_identifier(row.get("target_id"), "campaign target_id")
            for row in campaign_targets
        ]
        run_query = urlencode(
            {
                "select": (
                    "run_id,target_id,method,method_version,task_payload,result,status,completed_at"
                ),
                "target_id": "in.(" + ",".join(target_ids) + ")",
                "status": "eq.succeeded",
                "order": "target_id.asc,method.asc,completed_at.desc,run_id.desc",
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

    def weekly_quiz_round(self, round_id: str) -> dict[str, Any]:
        """Return one exact private weekly-round row for reveal evaluation.

        This deliberately queries the private table rather than the public view:
        the latter omits the private-index pointer and hides reveal state.  A
        missing or duplicated identity is an error, never an invitation to pick
        a nearby/current row.
        """

        round_id = _safe_identifier(round_id, "round_id")
        query = urlencode(
            {
                "select": (
                    "round_id,campaign_id,status,opens_at,closes_at,blind_manifest,"
                    "blind_manifest_sha256,reveal_manifest,reveal_manifest_sha256,metadata,"
                    "environment,item_count,opened_at,revealed_at"
                ),
                "round_id": f"eq.{round_id}",
                "limit": "2",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/weekly_quiz_rounds?{query}", "weekly quiz round query"
        )
        if len(rows) != 1:
            raise SupabasePublicationError(
                f"weekly quiz round query returned {len(rows)} rows for exact round_id"
            )
        row = rows[0]
        if row.get("round_id") != round_id:
            raise SupabasePublicationError("weekly quiz round query returned the wrong round_id")
        row["metadata"] = _json_object(row.get("metadata"), "weekly round metadata")
        return row

    def current_weekly_quiz_round(
        self, campaign_id: str | None = None, *, environment: str = "production"
    ) -> dict[str, Any]:
        """Return the one current public round, optionally bound to a campaign.

        A Saturday campaign can acquire an immutable replacement round after a
        publication defect is found.  The public RPC already resolves that
        choice by ``opens_at``. Callers resolving a known campaign can retain
        the explicit guard, while scheduled lifecycle jobs should follow the
        environment's actual current round across campaign rollovers.
        """

        if campaign_id is not None:
            campaign_id = _safe_identifier(campaign_id, "campaign_id")
        environment = _weekly_quiz_environment(environment)
        rows = self._rpc(
            "get_current_weekly_quiz_round", {"p_environment": environment}
        )
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            count = len(rows) if isinstance(rows, list) else 0
            raise SupabasePublicationError(
                f"current weekly quiz round query returned {count} rows"
            )
        row = dict(rows[0])
        row["round_id"] = _safe_identifier(row.get("round_id"), "round_id")
        if campaign_id is not None and row.get("campaign_id") != campaign_id:
            raise SupabasePublicationError(
                "current weekly quiz round does not belong to the expected campaign"
            )
        return row

    def weekly_quiz_reveal_inputs(self, round_id: str) -> tuple[dict[str, Any], bytes]:
        """Resolve an exact private round and its digest-bound private index."""

        row = self.weekly_quiz_round(round_id)
        raw_index = row["metadata"].get("private_index")
        private_index = _json_object(raw_index, "weekly round metadata.private_index")
        object_uri = private_index.get("object_uri")
        digest = private_index.get("sha256")
        media_type = private_index.get("media_type")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise SupabasePublicationError("weekly round private index has no valid SHA-256")
        if media_type != "application/json":
            raise SupabasePublicationError("weekly round private index must be application/json")
        content = self.download_content_object(object_uri, expected_sha256=digest)
        size_bytes = private_index.get("size_bytes")
        if size_bytes is not None and (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes != len(content)
        ):
            raise SupabasePublicationError("weekly round private index size does not match")
        return row, content

    def predicted_complex_artifact(
        self, run_id: str, sample_id: str
    ) -> dict[str, Any]:
        """Return exactly one original predicted-complex artifact identity."""

        run_id = _safe_identifier(run_id, "run_id")
        sample_id = _safe_identifier(sample_id, "sample_id")
        query = urlencode(
            {
                "select": "run_id,sample_id,role,object_uri,sha256,media_type",
                "run_id": f"eq.{run_id}",
                "sample_id": f"eq.{sample_id}",
                "role": "eq.predicted_complex",
                "limit": "2",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/prediction_artifacts?{query}",
            "predicted complex artifact query",
        )
        if len(rows) != 1:
            raise SupabasePublicationError(
                "predicted complex artifact query did not return exactly one row"
            )
        artifact = rows[0]
        digest = artifact.get("sha256")
        media_type = artifact.get("media_type")
        if (
            artifact.get("run_id") != run_id
            or artifact.get("sample_id") != sample_id
            or artifact.get("role") != "predicted_complex"
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or not isinstance(media_type, str)
            or not media_type
        ):
            raise SupabasePublicationError("predicted complex artifact metadata is invalid")
        return artifact

    def download_predicted_complex(self, run_id: str, sample_id: str) -> dict[str, Any]:
        """Download an exact original complex and verify its recorded digest."""

        artifact = self.predicted_complex_artifact(run_id, sample_id)
        content = self.download_content_object(
            artifact.get("object_uri"), expected_sha256=artifact["sha256"]
        )
        return {**artifact, "content": content}

    def fetch_campaign_target_packages(
        self,
        campaign_id: str,
        target_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Fetch and verify canonical target packages for one exact campaign subset."""

        campaign_id = _safe_identifier(campaign_id, "campaign_id")
        if isinstance(target_ids, (str, bytes)):
            raise SupabasePublicationError("target_ids must be an iterable of identifiers")
        requested = sorted(
            {
                _safe_identifier(target_id, "target_id").upper()
                for target_id in target_ids
                if isinstance(target_id, str) and target_id.strip()
            }
        )
        if not requested:
            raise SupabasePublicationError("target_ids must be a non-empty set")
        query = urlencode(
            {
                "select": "target_id,campaign_id,package_uri,package_sha256",
                "campaign_id": f"eq.{campaign_id}",
                "target_id": "in.(" + ",".join(requested) + ")",
                "order": "target_id.asc",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/targets?{query}", "campaign target package query"
        )
        by_target: dict[str, dict[str, Any]] = {}
        for row in rows:
            target_id = _safe_identifier(row.get("target_id"), "target_id").upper()
            if target_id in by_target:
                raise SupabasePublicationError(
                    "campaign target package query returned duplicate target rows"
                )
            row_campaign = _safe_identifier(row.get("campaign_id"), "campaign_id")
            if row_campaign != campaign_id:
                raise SupabasePublicationError(
                    "campaign target package query crossed the campaign boundary"
                )
            package_uri = row.get("package_uri")
            package_sha256 = row.get("package_sha256")
            if not isinstance(package_uri, str) or not package_uri:
                raise SupabasePublicationError("target package_uri is invalid")
            if not isinstance(package_sha256, str) or not _SHA256.fullmatch(
                package_sha256
            ):
                raise SupabasePublicationError("target package_sha256 is invalid")
            by_target[target_id] = {
                "target_id": target_id,
                "campaign_id": row_campaign,
                "package_uri": package_uri,
                "package_sha256": package_sha256,
            }
        missing = [target_id for target_id in requested if target_id not in by_target]
        if missing:
            raise SupabasePublicationError(
                "campaign target package query did not return every requested target: "
                + ", ".join(missing)
            )
        if sorted(by_target) != requested:
            raise SupabasePublicationError(
                "campaign target package query returned an unexpected target set"
            )
        packages: dict[str, dict[str, Any]] = {}
        for target_id in requested:
            descriptor = by_target[target_id]
            content = self.download_content_object(
                descriptor["package_uri"],
                expected_sha256=descriptor["package_sha256"],
            )
            try:
                decoded = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SupabasePublicationError(
                    f"target package for {target_id} is not valid UTF-8 JSON"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise SupabasePublicationError(
                    f"target package for {target_id} must be an object"
                )
            package = deepcopy(dict(decoded))
            package_target_id = package.get("target_id")
            if not isinstance(package_target_id, str) or not package_target_id:
                raise SupabasePublicationError(
                    f"target package for {target_id} has no target_id"
                )
            if package_target_id.strip().upper() != target_id:
                raise SupabasePublicationError(
                    f"target package for {target_id} disagrees with its row identity"
                )
            packages[target_id] = {
                **descriptor,
                "package": package,
            }
        return packages

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

    def store_bytes(
        self,
        content: bytes,
        media_type: str,
        *,
        cache_control: str | None = None,
    ) -> dict[str, Any]:
        """Store bytes by digest without overwriting an existing object."""

        if not isinstance(content, bytes) or not content:
            raise SupabasePublicationError("stored content must be non-empty bytes")
        if not isinstance(media_type, str) or not media_type or len(media_type) > 255:
            raise SupabasePublicationError("media_type must be a short string")
        if cache_control is not None and (
            not isinstance(cache_control, str)
            or not cache_control
            or len(cache_control) > 255
        ):
            raise SupabasePublicationError("cache_control must be a short string")
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
            extra_headers={
                "x-upsert": "false",
                **({"Cache-Control": cache_control} if cache_control else {}),
            },
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

    def replace_content_object(
        self,
        object_uri: str,
        content: bytes,
        media_type: str,
        *,
        cache_control: str,
    ) -> dict[str, Any]:
        """Replace exact content-addressed bytes only to update object metadata."""

        if not isinstance(content, bytes) or not content:
            raise SupabasePublicationError("stored content must be non-empty bytes")
        if not isinstance(media_type, str) or not media_type or len(media_type) > 255:
            raise SupabasePublicationError("media_type must be a short string")
        if not isinstance(cache_control, str) or not cache_control or len(cache_control) > 255:
            raise SupabasePublicationError("cache_control must be a short string")
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
        if hashlib.sha256(content).hexdigest() != digest:
            raise SupabasePublicationError(
                "replacement bytes do not match the content-addressed object digest"
            )
        endpoint = (
            "/storage/v1/object/"
            + quote(self.storage_bucket, safe="")
            + "/"
            + quote(object_path, safe="/")
        )
        self._request(
            endpoint,
            content,
            operation="content metadata replacement",
            content_type=media_type,
            extra_headers={"Cache-Control": cache_control},
            method="PUT",
        )
        return {
            "object_uri": object_uri,
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
            "cache_control": cache_control,
        }

    def _storage_bucket_metadata(self, operation: str) -> dict[str, Any]:
        endpoint = "/storage/v1/bucket/" + quote(self.storage_bucket, safe="")
        body = self._request(
            endpoint,
            None,
            operation=operation,
            method="GET",
        )
        try:
            bucket = json.loads((body or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupabasePublicationError(f"{operation} returned invalid JSON") from exc
        if not isinstance(bucket, Mapping) or bucket.get("id") != self.storage_bucket:
            raise SupabasePublicationError(f"{operation} returned the wrong bucket")
        return deepcopy(dict(bucket))

    def require_public_bucket(self) -> None:
        """Fail unless this Storage bucket is browser-readable without a token."""

        bucket = self._storage_bucket_metadata("public storage bucket check")
        if bucket.get("public") is not True:
            raise SupabasePublicationError(
                f"storage bucket {self.storage_bucket!r} must be public for quiz assets"
            )

    def require_private_bucket(self) -> None:
        """Fail unless this Storage bucket requires authenticated object access."""

        bucket = self._storage_bucket_metadata("private storage bucket check")
        if bucket.get("public") is not False:
            raise SupabasePublicationError(
                f"storage bucket {self.storage_bucket!r} must be private for evaluation results"
            )

    def authorize_transient_boltz_msa_retries(
        self,
        run_ids: list[str],
        *,
        confirmed_oom_run_ids: list[str],
        resubmit_already_authorized: bool = False,
    ) -> dict[str, Any]:
        """Authorize one serialized retry for exact transient Boltz MSA runs.

        Initial authorization requires every requested row to be a failed
        Boltz-2 run at attempt 1/1 with an explicitly allowed transient error
        code, and requires the operator to supply the separately diagnosed OOM
        run IDs that must be refused. The conditional PATCH changes only
        ``max_attempts`` to 2; the existing claim RPC performs the actual
        failed-to-running transition.

        A repeated preflight after a completed authorization is read-only and
        reports the rows as already authorized. It never authorizes a third
        attempt and gives the deployment adapter no newly authorized task to
        submit again by default. The explicit ``resubmit_already_authorized``
        recovery switch exposes the verified task only when a prior control
        call authorized the row but failed before the execution backend acknowledged a spawn.
        """

        from .contracts import validate_prediction_task

        if not isinstance(resubmit_already_authorized, bool):
            raise SupabasePublicationError(
                "resubmit_already_authorized must be a boolean"
            )
        if not isinstance(run_ids, list) or not run_ids:
            raise SupabasePublicationError("retry run_ids must be a non-empty list")
        if len(run_ids) > MAX_TRANSIENT_BOLTZ_MSA_RETRY_RUNS:
            raise SupabasePublicationError(
                "retry run_ids exceeds the bounded maintenance batch size"
            )
        normalized_ids = [
            _safe_identifier(run_id, f"retry run_ids[{index}]")
            for index, run_id in enumerate(run_ids)
        ]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise SupabasePublicationError("retry run_ids must be unique")
        if not isinstance(confirmed_oom_run_ids, list):
            raise SupabasePublicationError(
                "confirmed_oom_run_ids must be an operator-reviewed list"
            )
        if len(confirmed_oom_run_ids) > MAX_TRANSIENT_BOLTZ_MSA_RETRY_RUNS:
            raise SupabasePublicationError(
                "confirmed_oom_run_ids exceeds the bounded maintenance batch size"
            )
        normalized_oom_ids = [
            _safe_identifier(run_id, f"confirmed_oom_run_ids[{index}]")
            for index, run_id in enumerate(confirmed_oom_run_ids)
        ]
        if len(set(normalized_oom_ids)) != len(normalized_oom_ids):
            raise SupabasePublicationError("confirmed_oom_run_ids must be unique")
        refused_oom_ids = sorted(set(normalized_ids).intersection(normalized_oom_ids))
        if refused_oom_ids:
            raise SupabasePublicationError(
                "refusing to authorize operator-confirmed OOM run_ids: "
                + ", ".join(refused_oom_ids)
            )

        fields = (
            "run_id,target_id,method,status,attempt_count,max_attempts,"
            "error_code,task_payload"
        )

        def fetch_rows(operation: str) -> list[dict[str, Any]]:
            query = urlencode(
                {
                    "select": fields,
                    "run_id": "in.(" + ",".join(normalized_ids) + ")",
                    "order": "run_id.asc",
                }
            )
            rows = self._get_json_rows(
                f"/rest/v1/prediction_runs?{query}", operation
            )
            by_id = {
                _safe_identifier(row.get("run_id"), "retry row run_id"): row
                for row in rows
            }
            if len(by_id) != len(rows):
                raise SupabasePublicationError("retry preflight returned duplicate run rows")
            missing = [run_id for run_id in normalized_ids if run_id not in by_id]
            extras = sorted(set(by_id).difference(normalized_ids))
            if missing or extras:
                raise SupabasePublicationError(
                    "retry preflight did not return exactly the requested run_ids"
                )
            return [by_id[run_id] for run_id in normalized_ids]

        def validate_rows(
            rows: list[dict[str, Any]], *, allowed_max_attempts: set[int]
        ) -> dict[str, dict[str, Any]]:
            tasks: dict[str, dict[str, Any]] = {}
            for row in rows:
                run_id = row["run_id"]
                if row.get("method") != "boltz2":
                    raise SupabasePublicationError(
                        f"retry run {run_id} is not a Boltz-2 run"
                    )
                if row.get("status") != "failed" or row.get("attempt_count") != 1:
                    raise SupabasePublicationError(
                        f"retry run {run_id} must be failed at attempt_count 1"
                    )
                if row.get("max_attempts") not in allowed_max_attempts:
                    raise SupabasePublicationError(
                        f"retry run {run_id} has an unauthorized max_attempts value"
                    )
                if row.get("error_code") not in TRANSIENT_BOLTZ_MSA_RETRY_ERROR_CODES:
                    raise SupabasePublicationError(
                        f"retry run {run_id} does not have an allowed transient MSA error"
                    )
                try:
                    task = validate_prediction_task(row.get("task_payload"))
                except (TypeError, ValueError) as exc:
                    raise SupabasePublicationError(
                        f"retry run {run_id} has an invalid task payload"
                    ) from exc
                if (
                    task.get("task_id") != run_id
                    or task.get("method") != "boltz2"
                    or task.get("target", {}).get("target_id") != row.get("target_id")
                ):
                    raise SupabasePublicationError(
                        f"retry run {run_id} task payload does not match the stored run"
                    )
                tasks[run_id] = task
            return tasks

        before = fetch_rows("transient Boltz MSA retry preflight")
        tasks = validate_rows(before, allowed_max_attempts={1, 2})
        newly_authorized = [
            row["run_id"] for row in before if row["max_attempts"] == 1
        ]
        already_authorized = [
            row["run_id"] for row in before if row["max_attempts"] == 2
        ]
        recovery_submissions = (
            already_authorized if resubmit_already_authorized else []
        )

        if newly_authorized:
            allowed_codes = sorted(TRANSIENT_BOLTZ_MSA_RETRY_ERROR_CODES)
            query = urlencode(
                {
                    "run_id": "in.(" + ",".join(newly_authorized) + ")",
                    "method": "eq.boltz2",
                    "status": "eq.failed",
                    "attempt_count": "eq.1",
                    "max_attempts": "eq.1",
                    "error_code": "in.(" + ",".join(allowed_codes) + ")",
                }
            )
            body = self._request(
                f"/rest/v1/prediction_runs?{query}",
                self._encode_json({"max_attempts": 2}),
                operation="transient Boltz MSA retry authorization",
                method="PATCH",
                content_type="application/json",
                extra_headers={
                    "Accept": "application/json",
                    "Prefer": "return=representation",
                },
            )
            try:
                updated = json.loads((body or b"[]").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SupabasePublicationError(
                    "transient Boltz MSA retry authorization returned invalid JSON"
                ) from exc
            if not isinstance(updated, list) or not all(
                isinstance(row, Mapping) for row in updated
            ):
                raise SupabasePublicationError(
                    "transient Boltz MSA retry authorization returned an invalid row set"
                )
            updated_ids = {
                _safe_identifier(row.get("run_id"), "authorized retry run_id")
                for row in updated
            }
            if updated_ids != set(newly_authorized) or len(updated) != len(
                newly_authorized
            ):
                raise SupabasePublicationError(
                    "conditional retry authorization did not update every requested run"
                )

        verified = fetch_rows("transient Boltz MSA retry verification")
        verified_tasks = validate_rows(verified, allowed_max_attempts={2})
        if verified_tasks != tasks:
            raise SupabasePublicationError(
                "retry task payload changed during authorization"
            )
        approved_submission_ids = newly_authorized + recovery_submissions
        return {
            "status": (
                "authorized"
                if newly_authorized
                else (
                    "resubmission-authorized"
                    if recovery_submissions
                    else "already-authorized"
                )
            ),
            "requested_run_ids": normalized_ids,
            "authorized_run_ids": newly_authorized,
            "already_authorized_run_ids": already_authorized,
            "resubmission_authorized_run_ids": recovery_submissions,
            "approved_submission_run_ids": approved_submission_ids,
            "resubmit_already_authorized": resubmit_already_authorized,
            "authorization_rows": [
                {
                    "run_id": row["run_id"],
                    "target_id": row["target_id"],
                    "error_code": row["error_code"],
                    "attempt_count": row["attempt_count"],
                    "previous_max_attempts": row["max_attempts"],
                    "max_attempts": 2,
                    "action": (
                        "authorized"
                        if row["run_id"] in newly_authorized
                        else (
                            "approved-for-resubmission"
                            if resubmit_already_authorized
                            else "already-authorized"
                        )
                    ),
                }
                for row in before
            ],
            "confirmed_oom_run_ids": normalized_oom_ids,
            "allowed_error_codes": sorted(TRANSIENT_BOLTZ_MSA_RETRY_ERROR_CODES),
            "task_payloads": {
                run_id: verified_tasks[run_id] for run_id in approved_submission_ids
            },
        }

    def authorize_prediction_retries(
        self,
        retry_requests: list[dict[str, Any]],
        *,
        resubmit_already_authorized: bool = False,
    ) -> dict[str, Any]:
        """Authorize at most one exact, resource-bounded retry per failed run."""

        from .contracts import validate_prediction_task

        if not isinstance(resubmit_already_authorized, bool):
            raise SupabasePublicationError(
                "resubmit_already_authorized must be a boolean"
            )
        if not isinstance(retry_requests, list) or not retry_requests:
            raise SupabasePublicationError(
                "retry_requests must be a non-empty list"
            )
        if len(retry_requests) > MAX_PREDICTION_RETRY_RUNS:
            raise SupabasePublicationError(
                "retry_requests exceeds the one-campaign retry bound"
            )

        normalized_requests: list[dict[str, Any]] = []
        for index, raw in enumerate(retry_requests):
            if not isinstance(raw, Mapping):
                raise SupabasePublicationError(
                    f"retry_requests[{index}] must be an object"
                )
            request = {
                "run_id": _safe_identifier(
                    raw.get("run_id"), f"retry_requests[{index}].run_id"
                ),
                "target_id": _safe_identifier(
                    raw.get("target_id"), f"retry_requests[{index}].target_id"
                ),
                "method": _safe_identifier(
                    raw.get("method"), f"retry_requests[{index}].method"
                ),
                "source_error_code": _safe_identifier(
                    raw.get("source_error_code"),
                    f"retry_requests[{index}].source_error_code",
                ),
                "retry_kind": _safe_identifier(
                    raw.get("retry_kind"), f"retry_requests[{index}].retry_kind"
                ),
                "retry_gpu_class": _safe_identifier(
                    raw.get("retry_gpu_class"),
                    f"retry_requests[{index}].retry_gpu_class",
                ),
                "retry_timeout_seconds": raw.get("retry_timeout_seconds"),
                "reviewed_legacy": raw.get("reviewed_legacy"),
            }
            if request["method"] not in {"boltz2", "openfold3"}:
                raise SupabasePublicationError(
                    f"retry_requests[{index}].method is unsupported"
                )
            if request["retry_kind"] not in PREDICTION_RETRY_KINDS:
                raise SupabasePublicationError(
                    f"retry_requests[{index}].retry_kind is unsupported"
                )
            if request["retry_gpu_class"] not in {"l4", "a100-40gb"}:
                raise SupabasePublicationError(
                    f"retry_requests[{index}].retry_gpu_class is unsupported"
                )
            retry_timeout = request["retry_timeout_seconds"]
            if (
                isinstance(retry_timeout, bool)
                or not isinstance(retry_timeout, int)
                or not 1 <= retry_timeout <= 4_500
            ):
                raise SupabasePublicationError(
                    f"retry_requests[{index}].retry_timeout_seconds is invalid"
                )
            if not isinstance(request["reviewed_legacy"], bool):
                raise SupabasePublicationError(
                    f"retry_requests[{index}].reviewed_legacy must be a boolean"
                )
            normalized_requests.append(request)

        request_by_id = {
            request["run_id"]: request for request in normalized_requests
        }
        if len(request_by_id) != len(normalized_requests):
            raise SupabasePublicationError("retry_requests run_ids must be unique")
        normalized_ids = sorted(request_by_id)
        fields = (
            "run_id,target_id,method,status,attempt_count,max_attempts,"
            "error_code,task_payload"
        )

        def fetch_rows(operation: str) -> list[dict[str, Any]]:
            query = urlencode(
                {
                    "select": fields,
                    "run_id": "in.(" + ",".join(normalized_ids) + ")",
                    "order": "run_id.asc",
                }
            )
            rows = self._get_json_rows(
                f"/rest/v1/prediction_runs?{query}", operation
            )
            by_id = {
                _safe_identifier(row.get("run_id"), "retry row run_id"): row
                for row in rows
            }
            if len(by_id) != len(rows) or set(by_id) != set(normalized_ids):
                raise SupabasePublicationError(
                    "retry preflight did not return exactly the requested run_ids"
                )
            return [by_id[run_id] for run_id in normalized_ids]

        def validate_rows(
            rows: list[dict[str, Any]], *, allowed_max_attempts: set[int]
        ) -> dict[str, dict[str, Any]]:
            tasks: dict[str, dict[str, Any]] = {}
            for row in rows:
                run_id = row["run_id"]
                request = request_by_id[run_id]
                if row.get("status") != "failed" or row.get("attempt_count") != 1:
                    raise SupabasePublicationError(
                        f"retry run {run_id} must be failed at attempt_count 1"
                    )
                if row.get("max_attempts") not in allowed_max_attempts:
                    raise SupabasePublicationError(
                        f"retry run {run_id} has an unauthorized max_attempts value"
                    )
                if (
                    row.get("target_id") != request["target_id"]
                    or row.get("method") != request["method"]
                    or row.get("error_code") != request["source_error_code"]
                ):
                    raise SupabasePublicationError(
                        f"retry request identity does not match stored run {run_id}"
                    )
                try:
                    task = validate_prediction_task(row.get("task_payload"))
                except (TypeError, ValueError) as exc:
                    raise SupabasePublicationError(
                        f"retry run {run_id} has an invalid task payload"
                    ) from exc
                if (
                    task.get("task_id") != run_id
                    or task.get("method") != row.get("method")
                    or task.get("target", {}).get("target_id")
                    != row.get("target_id")
                ):
                    raise SupabasePublicationError(
                        f"retry run {run_id} task payload does not match the stored run"
                    )
                resources = task.get("resources")
                original_gpu = (
                    resources.get("gpu_class")
                    if isinstance(resources, Mapping)
                    else None
                )
                original_timeout = (
                    resources.get("timeout_seconds")
                    if isinstance(resources, Mapping)
                    else None
                )
                expected_resources = {
                    "gpu_out_of_memory": ("a100-40gb", 1_800),
                    "msa_generation_timeout": ("l4", 4_500),
                    "msa_preprocessing_failed": ("l4", 1_800),
                    "repeat_once": (original_gpu, original_timeout),
                }[request["retry_kind"]]
                if (
                    original_gpu != "l4"
                    or original_timeout != 1_800
                    or request["retry_gpu_class"] != expected_resources[0]
                    or request["retry_timeout_seconds"] != expected_resources[1]
                ):
                    raise SupabasePublicationError(
                        f"retry request resources do not match policy for {run_id}"
                    )
                tasks[run_id] = task
            return tasks

        before = fetch_rows("prediction retry preflight")
        tasks = validate_rows(before, allowed_max_attempts={1, 2})
        newly_authorized = [
            row["run_id"] for row in before if row["max_attempts"] == 1
        ]
        already_authorized = [
            row["run_id"] for row in before if row["max_attempts"] == 2
        ]
        recovery_submissions = (
            already_authorized if resubmit_already_authorized else []
        )

        if newly_authorized:
            query = urlencode(
                {
                    "run_id": "in.(" + ",".join(newly_authorized) + ")",
                    "status": "eq.failed",
                    "attempt_count": "eq.1",
                    "max_attempts": "eq.1",
                }
            )
            body = self._request(
                f"/rest/v1/prediction_runs?{query}",
                self._encode_json({"max_attempts": 2}),
                operation="prediction retry authorization",
                method="PATCH",
                content_type="application/json",
                extra_headers={
                    "Accept": "application/json",
                    "Prefer": "return=representation",
                },
            )
            try:
                updated = json.loads((body or b"[]").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SupabasePublicationError(
                    "prediction retry authorization returned invalid JSON"
                ) from exc
            if not isinstance(updated, list) or not all(
                isinstance(row, Mapping) for row in updated
            ):
                raise SupabasePublicationError(
                    "prediction retry authorization returned an invalid row set"
                )
            updated_ids = {
                _safe_identifier(row.get("run_id"), "authorized retry run_id")
                for row in updated
            }
            if updated_ids != set(newly_authorized) or len(updated) != len(
                newly_authorized
            ):
                raise SupabasePublicationError(
                    "conditional retry authorization did not update every requested run"
                )

        verified = fetch_rows("prediction retry verification")
        verified_tasks = validate_rows(verified, allowed_max_attempts={2})
        if verified_tasks != tasks:
            raise SupabasePublicationError(
                "retry task payload changed during authorization"
            )
        approved_submission_ids = newly_authorized + recovery_submissions
        return {
            "status": (
                "authorized"
                if newly_authorized
                else (
                    "resubmission-authorized"
                    if recovery_submissions
                    else "already-authorized"
                )
            ),
            "retry_requests": normalized_requests,
            "requested_run_ids": normalized_ids,
            "authorized_run_ids": newly_authorized,
            "already_authorized_run_ids": already_authorized,
            "resubmission_authorized_run_ids": recovery_submissions,
            "approved_submission_run_ids": approved_submission_ids,
            "resubmit_already_authorized": resubmit_already_authorized,
            "authorization_rows": [
                {
                    "run_id": row["run_id"],
                    "target_id": row["target_id"],
                    "method": row["method"],
                    "error_code": row["error_code"],
                    "attempt_count": row["attempt_count"],
                    "previous_max_attempts": row["max_attempts"],
                    "max_attempts": 2,
                    "action": (
                        "authorized"
                        if row["run_id"] in newly_authorized
                        else (
                            "approved-for-resubmission"
                            if resubmit_already_authorized
                            else "already-authorized"
                        )
                    ),
                }
                for row in before
            ],
            "task_payloads": {
                run_id: verified_tasks[run_id]
                for run_id in approved_submission_ids
            },
        }

    def register_weekly_plan(
        self,
        plan: Mapping[str, Any],
        source_files: Mapping[str, tuple[bytes, str]],
        *,
        adapter_version: str,
        execution_backend: str = "local",
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
        execution_backend: str = "local",
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
        environment: str = "production",
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
            "p_environment": _weekly_quiz_environment(environment),
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

    def private_weekly_evaluation(
        self, round_id: str
    ) -> dict[str, Any] | None:
        """Return the immutable private evaluation descriptor for one round."""

        round_id = _safe_identifier(round_id, "round_id")
        query = urlencode(
            {
                "select": ",".join(PRIVATE_WEEKLY_EVALUATION_FIELDS)
                + ",created_at",
                "round_id": f"eq.{round_id}",
                "limit": "2",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/weekly_quiz_evaluations?{query}",
            "private weekly evaluation lookup",
        )
        if len(rows) > 1:
            raise SupabasePublicationError(
                "private weekly evaluation lookup returned duplicate rows"
            )
        return deepcopy(dict(rows[0])) if rows else None

    def register_private_weekly_evaluation(
        self, descriptor: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Insert or verify one immutable service-role-only evaluation descriptor.

        The table has no browser grants or public read RPC.  Its database trigger
        locks the exact weekly round and rechecks production/open/unrevealed and
        post-close state plus the blind/private-index digests in the same
        transaction as this insert.
        """

        from .private_evaluation import (
            PRIVATE_EVALUATION_FORMAT_VERSION,
            PRIVATE_EVALUATION_MEDIA_TYPE,
        )
        from .wednesday_reveal import (
            ACCEPTANCE_POLICY_VERSION,
            CORRECT_RMSD_ANGSTROM,
            REVEAL_POLICY_VERSION,
        )

        payload = _json_object(descriptor, "private weekly evaluation descriptor")
        if set(payload) != set(PRIVATE_WEEKLY_EVALUATION_FIELDS):
            raise SupabasePublicationError(
                "private weekly evaluation descriptor fields are not exact"
            )
        for field in ("evaluation_id", "round_id", "campaign_id"):
            _safe_identifier(payload.get(field), f"evaluation descriptor {field}")
        if payload.get("environment") != "production":
            raise SupabasePublicationError(
                "private weekly evaluation must bind the production environment"
            )
        if payload.get("format_version") != PRIVATE_EVALUATION_FORMAT_VERSION:
            raise SupabasePublicationError("private evaluation format_version is invalid")
        if payload.get("artifact_media_type") != PRIVATE_EVALUATION_MEDIA_TYPE:
            raise SupabasePublicationError("private evaluation artifact media type is invalid")
        if payload.get("reveal_policy_version") != REVEAL_POLICY_VERSION:
            raise SupabasePublicationError("private evaluation reveal policy is invalid")
        if payload.get("acceptance_policy_version") != ACCEPTANCE_POLICY_VERSION:
            raise SupabasePublicationError("private evaluation acceptance policy is invalid")
        if payload.get("correct_rmsd_threshold_angstrom") != CORRECT_RMSD_ANGSTROM:
            raise SupabasePublicationError("private evaluation RMSD threshold is invalid")
        for field in (
            "blind_manifest_sha256",
            "private_index_sha256",
            "reveal_manifest_sha256",
            "reference_set_sha256",
            "prediction_set_sha256",
            "artifact_sha256",
        ):
            digest = payload.get(field)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise SupabasePublicationError(
                    f"private evaluation {field} must be a lowercase SHA-256"
                )
        for field in ("item_count", "choice_count", "artifact_size_bytes"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SupabasePublicationError(
                    f"private evaluation {field} must be a positive integer"
                )
        versions = payload.get("evaluator_versions")
        if (
            not isinstance(versions, list)
            or not versions
            or any(not isinstance(value, str) or not value for value in versions)
        ):
            raise SupabasePublicationError(
                "private evaluation evaluator_versions must be a sorted unique list"
            )
        if versions != sorted(set(versions)):
            raise SupabasePublicationError(
                "private evaluation evaluator_versions must be a sorted unique list"
            )
        timestamps: dict[str, datetime] = {}
        for field in ("round_opens_at", "round_closes_at"):
            value = payload.get(field)
            if not isinstance(value, str):
                raise SupabasePublicationError(f"private evaluation {field} is invalid")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SupabasePublicationError(
                    f"private evaluation {field} is invalid"
                ) from exc
            if parsed.tzinfo is None:
                raise SupabasePublicationError(
                    f"private evaluation {field} must include a timezone"
                )
            timestamps[field] = parsed
        if timestamps["round_closes_at"] <= timestamps["round_opens_at"]:
            raise SupabasePublicationError("private evaluation voting window is invalid")

        artifact_sha256 = payload["artifact_sha256"]
        object_uri = payload.get("artifact_object_uri")
        parsed_uri = urlsplit(object_uri) if isinstance(object_uri, str) else None
        expected_path = f"/sha256/{artifact_sha256[:2]}/{artifact_sha256}"
        if (
            parsed_uri is None
            or parsed_uri.scheme != "supabase"
            or parsed_uri.netloc != self.storage_bucket
            or parsed_uri.path != expected_path
            or parsed_uri.query
            or parsed_uri.fragment
        ):
            raise SupabasePublicationError(
                "private evaluation artifact URI is not the exact content-addressed object"
            )
        expected_id = stable_id(
            "weekly_eval",
            {
                "format_version": payload["format_version"],
                "round_id": payload["round_id"],
                "blind_manifest_sha256": payload["blind_manifest_sha256"],
                "private_index_sha256": payload["private_index_sha256"],
                "artifact_sha256": artifact_sha256,
            },
            length=32,
        )
        if payload["evaluation_id"] != expected_id:
            raise SupabasePublicationError("private evaluation_id is not deterministic")

        endpoint = "/rest/v1/weekly_quiz_evaluations?on_conflict=evaluation_id"
        body = self._request(
            endpoint,
            self._encode_json(payload),
            operation="private weekly evaluation catalog insert",
            method="POST",
            content_type="application/json",
            extra_headers={
                "Accept": "application/json",
                "Prefer": "resolution=ignore-duplicates,return=representation",
            },
        )
        try:
            rows = json.loads((body or b"[]").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupabasePublicationError(
                "private weekly evaluation catalog insert returned invalid JSON"
            ) from exc
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise SupabasePublicationError(
                "private weekly evaluation catalog insert returned an invalid row set"
            )
        if not rows:
            query = urlencode(
                {
                    "select": ",".join(PRIVATE_WEEKLY_EVALUATION_FIELDS)
                    + ",created_at",
                    "evaluation_id": f"eq.{payload['evaluation_id']}",
                    "limit": "2",
                }
            )
            rows = self._get_json_rows(
                f"/rest/v1/weekly_quiz_evaluations?{query}",
                "private weekly evaluation idempotence query",
            )
        if len(rows) != 1:
            raise SupabasePublicationError(
                "private weekly evaluation catalog did not return one exact row"
            )
        row = deepcopy(dict(rows[0]))
        for field, expected in payload.items():
            if row.get(field) != expected:
                raise SupabasePublicationError(
                    f"private weekly evaluation catalog differs at {field}"
                )
        return row

    def weekly_retrospective_source_rows(
        self, round_id: str, *, environment: str = "production"
    ) -> dict[str, list[dict[str, Any]]]:
        """Snapshot the bounded rows used by retrospective aggregation.

        Raw application state is fetched only to extract the approved
        ``selection_kind`` field in the deterministic core helper. It is never
        copied into either publication artifact or the normalized source object.
        Post-close benchmark rows are fetched separately from ballots via the
        reveal-gated selector benchmark RPC and reduced to ``display_name`` plus
        ``payload`` before leaving this coordinator boundary.
        """

        round_id = _safe_identifier(round_id, "round_id")
        environment = _safe_identifier(environment, "environment")
        round_filter = f"eq.{round_id}"
        votes_query = urlencode(
            {
                "select": "round_id,user_id,item_id,choice_id,picked_none",
                "round_id": round_filter,
                "order": "user_id.asc,item_id.asc",
            }
        )
        attempts_query = urlencode(
            {
                "select": (
                    "vote_attempt_id,round_id,user_id,item_id,choice_id,"
                    "picked_none,app_state,submitted_at"
                ),
                "round_id": round_filter,
                "order": (
                    "user_id.asc,item_id.asc,submitted_at.asc,"
                    "vote_attempt_id.asc"
                ),
            }
        )
        sessions_query = urlencode(
            {
                "select": "round_id,user_id,display_name",
                "round_id": round_filter,
                "order": "user_id.asc,started_at.asc,session_id.asc",
            }
        )
        automated_identities_query = urlencode(
            {
                "select": "user_id,display_name,participant_kind",
                "order": "user_id.asc",
            }
        )
        benchmark_rows = self._rpc(
            "get_weekly_selector_benchmarks_v1",
            {"p_environment": environment, "p_round_id": round_id},
        )
        if benchmark_rows is None:
            benchmark_rows = []
        if not isinstance(benchmark_rows, list) or not all(
            isinstance(row, Mapping) for row in benchmark_rows
        ):
            raise SupabasePublicationError(
                "weekly retrospective benchmark snapshot returned an invalid row set"
            )
        post_close_benchmarks: list[dict[str, Any]] = []
        for row in benchmark_rows:
            run_class = row.get("run_class")
            display_name = row.get("display_name")
            payload = row.get("payload")
            if (
                not isinstance(run_class, str)
                or not run_class
                or not isinstance(display_name, str)
                or not display_name
                or not isinstance(payload, Mapping)
            ):
                raise SupabasePublicationError(
                    "weekly retrospective benchmark snapshot row is malformed"
                )
            post_close_benchmarks.append(
                {
                    "run_class": run_class,
                    "display_name": display_name,
                    "payload": deepcopy(dict(payload)),
                }
            )
        return {
            "votes": self._get_all_json_rows(
                f"/rest/v1/weekly_quiz_votes?{votes_query}",
                "weekly retrospective vote snapshot",
            ),
            "vote_attempts": self._get_all_json_rows(
                f"/rest/v1/weekly_quiz_vote_attempts?{attempts_query}",
                "weekly retrospective vote-attempt snapshot",
            ),
            "current_sessions": self._get_all_json_rows(
                f"/rest/v1/weekly_quiz_sessions?{sessions_query}",
                "weekly retrospective session snapshot",
            ),
            "automated_identities": self._get_all_json_rows(
                (
                    "/rest/v1/weekly_retrospective_automated_identities?"
                    f"{automated_identities_query}"
                ),
                "weekly retrospective automated-identity registry snapshot",
            ),
            "post_close_benchmarks": post_close_benchmarks,
        }

    def weekly_retrospective_publication(
        self, round_id: str
    ) -> dict[str, Any] | None:
        """Return one exact immutable retrospective publication descriptor."""

        round_id = _safe_identifier(round_id, "round_id")
        query = urlencode(
            {
                "select": ",".join(WEEKLY_RETROSPECTIVE_PUBLICATION_FIELDS)
                + ",created_at",
                "round_id": f"eq.{round_id}",
                "limit": "2",
            }
        )
        rows = self._get_json_rows(
            f"/rest/v1/weekly_retrospective_publications?{query}",
            "weekly retrospective publication lookup",
        )
        if len(rows) > 1:
            raise SupabasePublicationError(
                "weekly retrospective publication lookup returned duplicate rows"
            )
        return deepcopy(dict(rows[0])) if rows else None

    def missing_weekly_retrospective_round_ids(self) -> list[str]:
        """List every revealed production round whose publication is absent."""

        response = self._rpc(
            "list_missing_weekly_retrospective_publications", {}
        )
        if not isinstance(response, list) or not all(
            isinstance(row, Mapping) for row in response
        ):
            raise SupabasePublicationError(
                "missing retrospective publication scan returned an invalid row set"
            )
        round_ids = [
            _safe_identifier(row.get("round_id"), "missing retrospective round_id")
            for row in response
        ]
        if len(round_ids) != len(set(round_ids)):
            raise SupabasePublicationError(
                "missing retrospective publication scan returned duplicate rounds"
            )
        return round_ids

    def register_weekly_retrospective_publication(
        self,
        descriptor: Mapping[str, Any],
        *,
        source_snapshot_canonical: str,
    ) -> dict[str, Any]:
        """Validate and register one exact source-bound archive publication."""

        from .retrospective_archive import (
            RETROSPECTIVE_MEDIA_TYPE,
            RETROSPECTIVE_PUBLICATION_FORMAT_VERSION,
            RETROSPECTIVE_SOURCE_FORMAT_VERSION,
        )

        payload = _json_object(
            descriptor, "weekly retrospective publication descriptor"
        )
        if set(payload) != set(WEEKLY_RETROSPECTIVE_PUBLICATION_FIELDS):
            raise SupabasePublicationError(
                "weekly retrospective publication descriptor fields are not exact"
            )
        for field in (
            "publication_id",
            "round_id",
            "campaign_id",
            "evaluation_id",
        ):
            _safe_identifier(payload.get(field), f"retrospective descriptor {field}")
        if payload.get("environment") != "production":
            raise SupabasePublicationError(
                "retrospective publication must bind production"
            )
        if payload.get("format_version") != RETROSPECTIVE_PUBLICATION_FORMAT_VERSION:
            raise SupabasePublicationError(
                "retrospective publication format_version is invalid"
            )
        if payload.get("evaluation_format_version") != (
            "foldarium.weekly-private-evaluation/v5"
        ):
            raise SupabasePublicationError(
                "retrospective evaluation format_version is invalid"
            )
        digest_fields = (
            "blind_manifest_sha256",
            "private_index_sha256",
            "reveal_manifest_sha256",
            "reference_set_sha256",
            "prediction_set_sha256",
            "evaluation_artifact_sha256",
            "source_snapshot_sha256",
            "public_artifact_sha256",
            "admin_artifact_sha256",
        )
        for field in digest_fields:
            value = payload.get(field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise SupabasePublicationError(
                    f"retrospective publication {field} must be a lowercase SHA-256"
                )
        for field in (
            "item_count",
            "choice_count",
            "source_snapshot_size_bytes",
            "public_artifact_size_bytes",
            "admin_artifact_size_bytes",
        ):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SupabasePublicationError(
                    f"retrospective publication {field} must be positive"
                )
        for prefix in ("source_snapshot", "public_artifact", "admin_artifact"):
            if payload.get(f"{prefix}_media_type") != RETROSPECTIVE_MEDIA_TYPE:
                raise SupabasePublicationError(
                    f"retrospective publication {prefix} media type is invalid"
                )
            digest = payload[f"{prefix}_sha256"]
            object_uri = payload.get(f"{prefix}_object_uri")
            parsed = urlsplit(object_uri) if isinstance(object_uri, str) else None
            if (
                parsed is None
                or parsed.scheme != "supabase"
                or parsed.netloc != self.storage_bucket
                or parsed.path != f"/sha256/{digest[:2]}/{digest}"
                or parsed.query
                or parsed.fragment
            ):
                raise SupabasePublicationError(
                    f"retrospective publication {prefix} URI is invalid"
                )
        timestamps: dict[str, datetime] = {}
        for field in (
            "round_opens_at",
            "round_closes_at",
            "round_revealed_at",
        ):
            value = payload.get(field)
            if not isinstance(value, str):
                raise SupabasePublicationError(
                    f"retrospective publication {field} is invalid"
                )
            try:
                timestamps[field] = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise SupabasePublicationError(
                    f"retrospective publication {field} is invalid"
                ) from exc
            if timestamps[field].tzinfo is None:
                raise SupabasePublicationError(
                    f"retrospective publication {field} must include a timezone"
                )
        if not (
            timestamps["round_opens_at"]
            < timestamps["round_closes_at"]
            <= timestamps["round_revealed_at"]
        ):
            raise SupabasePublicationError(
                "retrospective publication round timestamps are inconsistent"
            )

        if not isinstance(source_snapshot_canonical, str) or not source_snapshot_canonical:
            raise SupabasePublicationError(
                "retrospective source snapshot canonical JSON is missing"
            )
        try:
            source_snapshot = json.loads(source_snapshot_canonical)
        except json.JSONDecodeError as exc:
            raise SupabasePublicationError(
                "retrospective source snapshot is not valid JSON"
            ) from exc
        if (
            not isinstance(source_snapshot, Mapping)
            or source_snapshot.get("format_version")
            != RETROSPECTIVE_SOURCE_FORMAT_VERSION
            or source_snapshot.get("round_id") != payload["round_id"]
            or canonical_json(source_snapshot) != source_snapshot_canonical
        ):
            raise SupabasePublicationError(
                "retrospective source snapshot canonical JSON is inconsistent"
            )
        source_digest = hashlib.sha256(
            source_snapshot_canonical.encode("utf-8")
        ).hexdigest()
        if (
            source_digest != payload["source_snapshot_sha256"]
            or len(source_snapshot_canonical.encode("utf-8"))
            != payload["source_snapshot_size_bytes"]
        ):
            raise SupabasePublicationError(
                "retrospective source snapshot descriptor is inconsistent"
            )
        expected_id = stable_id(
            "weekly_archive",
            {
                "format_version": payload["format_version"],
                "round_id": payload["round_id"],
                "evaluation_id": payload["evaluation_id"],
                "evaluation_artifact_sha256": payload[
                    "evaluation_artifact_sha256"
                ],
                "source_snapshot_sha256": payload["source_snapshot_sha256"],
                "public_artifact_sha256": payload["public_artifact_sha256"],
                "admin_artifact_sha256": payload["admin_artifact_sha256"],
            },
            length=32,
        )
        if payload["publication_id"] != expected_id:
            raise SupabasePublicationError(
                "retrospective publication_id is not deterministic"
            )
        response = self._rpc(
            "register_weekly_retrospective_publication",
            {
                "p_publication": payload,
                "p_source_snapshot_canonical": source_snapshot_canonical,
            },
        )
        if isinstance(response, list) and len(response) == 1:
            response = response[0]
        if not isinstance(response, Mapping):
            raise SupabasePublicationError(
                "retrospective publication registration returned no descriptor"
            )
        row = deepcopy(dict(response))
        for field, expected in payload.items():
            observed = row.get(field)
            if field in {
                "round_opens_at",
                "round_closes_at",
                "round_revealed_at",
            }:
                try:
                    observed_time = datetime.fromisoformat(
                        str(observed).replace("Z", "+00:00")
                    )
                    expected_time = datetime.fromisoformat(
                        str(expected).replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise SupabasePublicationError(
                        f"retrospective publication catalog differs at {field}"
                    ) from exc
                differs = observed_time != expected_time
            else:
                differs = observed != expected
            if differs:
                raise SupabasePublicationError(
                    f"retrospective publication catalog differs at {field}"
                )
        return row

    def register_weekly_selector_kit(
        self,
        *,
        round_id: str,
        kit_sha256: str,
        item_count: int,
        byte_size: int,
        storage_path: str,
        descriptor: Mapping[str, Any],
        blind_manifest_sha256: str,
    ) -> Any:
        """Persist one immutable selector kit descriptor for an exact weekly round.

        Requires a deployed ``register_weekly_selector_kit`` RPC that verifies
        the round exists, binds ``blind_manifest_sha256``, and inserts into
        ``private.weekly_selector_kit_catalog``.
        """

        if not re.fullmatch(r"[0-9a-f]{64}", kit_sha256):
            raise SupabasePublicationError("kit_sha256 must be a SHA-256 hex string")
        if not re.fullmatch(r"[0-9a-f]{64}", blind_manifest_sha256):
            raise SupabasePublicationError(
                "blind_manifest_sha256 must be a SHA-256 hex string"
            )
        if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 1:
            raise SupabasePublicationError("item_count must be a positive integer")
        if (
            isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size < 1
            or byte_size > 536_870_912
        ):
            raise SupabasePublicationError("byte_size must be between 1 and 536870912")
        if not isinstance(storage_path, str) or not storage_path.strip():
            raise SupabasePublicationError("storage_path is required")
        if len(storage_path) > 1024 or re.search(r"[\x00-\x1f\x7f]", storage_path):
            raise SupabasePublicationError("storage_path is invalid")
        payload = {
            "p_round_id": _safe_identifier(round_id, "round_id"),
            "p_kit_sha256": kit_sha256,
            "p_item_count": item_count,
            "p_byte_size": byte_size,
            "p_storage_path": storage_path.strip(),
            "p_descriptor": _json_object(descriptor, "descriptor"),
            "p_blind_manifest_sha256": blind_manifest_sha256,
        }
        return self._rpc("register_weekly_selector_kit", payload)

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
