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
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .contracts import stable_id

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


__all__ = [
    "SupabaseConfigurationError",
    "SupabasePublicationError",
    "SupabasePublisher",
]
