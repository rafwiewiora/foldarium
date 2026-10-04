"""Bounded private evidence for failed predictions, never quiz artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping, Sequence

VERSION = "prediction-failure-diagnostics/v1"
MAX_NATIVE_FILES = 32
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_INVENTORY = 256
MAX_DIRECTORIES = 128
_REDACTED = "[REDACTED]"
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_AUTH_TOKEN = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;\"']+")
_AUTH_HEADER = re.compile(r"(?im)(\bauthorization[\"']?\s*[:=]\s*)[^\r\n]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
# Once a credential value opens a quote, remove the remainder of that log line.
# Interrupted writers can leave dangling quotes/backslashes; permissive fallback
# must never retain a multiword credential tail.
_ASSIGNMENT = re.compile(r"""(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|authorization|credential|signature)["']?\s*[:=]\s*)(?:["'][^\r\n]*|[^\s,;]+)""")


def _check_deadline(deadline: float | None):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("private evidence collection deadline")


def redact(text: str, secrets: Sequence[str] = (), *, deadline: float | None = None) -> str:
    _check_deadline(deadline)
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        _check_deadline(deadline)
        variants = {secret, json.dumps(secret, ensure_ascii=True)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}
        variants.update(v.replace("/", "\\/") for v in tuple(variants))
        for variant in sorted(variants, key=len, reverse=True):
            _check_deadline(deadline)
            text = text.replace(variant, _REDACTED)
    # Normalize JSON-style escapes for detection. Native files are not rewritten:
    # any normalization/redaction means the whole native file is omitted.
    text = text.replace("\\/", "/").replace('\\"', '"').replace("\\'", "'")
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    for secret in secrets:
        _check_deadline(deadline)
        if isinstance(secret, str) and secret:
            text = text.replace(secret, _REDACTED)
    for pattern, replacement in (
        (_URL, "[REDACTED_URL]"),
        (_AUTH_HEADER, lambda m: m.group(1) + _REDACTED),
        (_AUTH_TOKEN, lambda m: m.group(1) + " " + _REDACTED),
        (_JWT, _REDACTED),
        (_ASSIGNMENT, lambda m: m.group(1) + _REDACTED),
    ):
        _check_deadline(deadline)
        text = pattern.sub(replacement, text)
    _check_deadline(deadline)
    return text


def credential_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Only credential-shaped names; TOKENIZERS_PARALLELISM is not a secret."""
    names = re.compile(r"(?:^|_)(?:API_KEY|PRIVATE_KEY|SECRET(?:_KEY)?|PASSWORD|TOKEN(?:_ID|_SECRET)?|ACCESS_KEY(?:_ID)?|CREDENTIALS?|PAT)$", re.I)
    return tuple(value for key, value in environ.items()
                 if value and (key == "SUPABASE_SERVICE_ROLE_KEY" or names.search(key)))


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _open_directory(root: Path, directory: Path) -> int:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in directory.relative_to(root).parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_regular(path: Path, limit: int, *, root: Path, head_tail: bool = False, deadline: float | None = None) -> tuple[bytes, int]:
    # Walk directory descriptors, not path resolution: an output process cannot
    # race an intermediate directory into a symlink escaping the task root.
    _check_deadline(deadline)
    parts = path.relative_to(root).parts
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        import stat
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("not an exclusively linked regular file")
        if not head_tail and info.st_size > limit:
            return b"", info.st_size
        _check_deadline(deadline)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            if head_tail and info.st_size > limit:
                head = stream.read(limit // 2)
                stream.seek(-limit // 2, os.SEEK_END)
                tail = stream.read(limit // 2)
                # Discard boundary lines, which may contain partial credentials.
                head = head.rsplit(b"\n", 1)[0] if b"\n" in head else b""
                tail = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
                data = head + b"\n[TRUNCATED]\n" + tail
            else:
                data = stream.read(limit + 1)
        _check_deadline(deadline)
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("file changed during evidence capture")
        return data, info.st_size
    finally:
        os.close(fd)


def build_failure_diagnostics(
    task: Mapping[str, Any], result: Mapping[str, Any], task_root: Path,
    claim: Mapping[str, Any], *, secrets: Sequence[str] = (), deadline: float | None = None,
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], bytes]]]:
    """Build bounded evidence. Scientific native bytes are exact or omitted."""
    if result.get("status") != "failed" or claim.get("run_id") != task.get("task_id"):
        raise ValueError("diagnostics require an exactly claimed failed task")
    if type(claim.get("attempt_count")) is not int or claim["attempt_count"] < 1:
        raise ValueError("diagnostics require authoritative attempt count")
    if task_root.is_symlink() or not task_root.is_dir():
        raise ValueError("unsafe task root")
    root = task_root.resolve(strict=True)
    inventory: list[dict[str, Any]] = []
    files: list[tuple[dict[str, Any], bytes]] = []
    total = 0
    native_count = 0
    inventory_limited = False
    collection_deadline_reached = False
    scanned_entries = 0

    def collect(path: Path, role: str):
        nonlocal total, native_count
        _check_deadline(deadline)
        relative = path.relative_to(root).as_posix()
        # File names can themselves carry credentials. Record a digest only.
        if redact(relative, secrets, deadline=deadline) != relative:
            inventory.append({"path_sha256": hashlib.sha256(relative.encode()).hexdigest(), "status": "sensitive_path_omitted"})
            return
        record: dict[str, Any] = {"relative_path": relative, "role": role}
        inventory.append(record)
        if role == "native_output" and native_count >= MAX_NATIVE_FILES:
            record["status"] = "file_count_limit_omitted"
            return
        if total >= MAX_TOTAL_BYTES:
            record["status"] = "total_size_limit_omitted"
            return
        if path.is_symlink():
            record["status"] = "symlink_omitted"
            return
        try:
            limit = min(MAX_LOG_BYTES if role == "log" else MAX_FILE_BYTES, MAX_TOTAL_BYTES - total)
            data, size = _read_regular(path, limit, root=root, head_tail=role == "log", deadline=deadline)
        except (OSError, ValueError):
            record["status"] = "unreadable_or_nonregular"
            return
        record["source_size_bytes"] = size
        if role == "log":
            # Bounded head/tail retains both launch context and final traceback.
            text = data.decode("utf-8", errors="replace")
            clean = redact(text, secrets, deadline=deadline)
            data = clean.encode("utf-8", errors="replace")
            encoded_overflow = len(data) > MAX_LOG_BYTES
            if encoded_overflow:
                half = (MAX_LOG_BYTES - 16) // 2
                data = data[:half] + b"\n[TRUNCATED]\n" + data[-half:]
            record.update(truncated=size > MAX_LOG_BYTES or encoded_overflow, redacted=clean != text)
        else:
            if size > MAX_FILE_BYTES or len(data) > MAX_FILE_BYTES:
                record["status"] = "size_limit_omitted"
                return
            if size > MAX_TOTAL_BYTES - total:
                record["status"] = "total_size_limit_omitted"
                return
            _check_deadline(deadline)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                record["status"] = "nontext_native_omitted"
                return
            if redact(text, secrets, deadline=deadline) != text:
                record["status"] = "sensitive_native_omitted"
                return
            native_count += 1
        if not data:
            record["status"] = "empty"
            return
        if total + len(data) > MAX_TOTAL_BYTES:
            record["status"] = "total_size_limit_omitted"
            return
        total += len(data)
        record.update(status="included", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        files.append((record, data))

    try:
        logs = root / "logs"
        if not logs.is_symlink():
            for name in ("stdout.log", "stderr.log"):
                _check_deadline(deadline)
                if (logs / name).exists() or (logs / name).is_symlink():
                    collect(logs / name, "log")
        # Never materialize or sort an unbounded directory listing. Every entry
        # counts, including irrelevant extensions and directories. Only the
        # bounded candidate list is sorted for deterministic capture order.
        pending = [root / "output"]
        candidates = []
        directories = 0
        max_scanned = max(0, MAX_INVENTORY - len(inventory))
        while pending and scanned_entries < max_scanned:
            _check_deadline(deadline)
            directory = pending.pop()
            if directory.is_symlink():
                inventory_limited = True
                continue
            if not directory.is_dir():
                if directory.exists():
                    inventory_limited = True
                continue
            directories += 1
            if directories > MAX_DIRECTORIES:
                inventory_limited = True
                break
            directory_fd = _open_directory(root, directory)
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        _check_deadline(deadline)
                        scanned_entries += 1
                        path = directory / entry.name
                        if entry.is_symlink():
                            # Its target may contain native science, even when
                            # the link name has no recognized file extension.
                            inventory_limited = True
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                        else:
                            suffix = path.suffix.lower()
                            if suffix in (".cif", ".pdb") or (suffix == ".json" and any(k in entry.name.lower() for k in ("confidence", "ranking", "score"))):
                                candidates.append(path)
                        if scanned_entries >= max_scanned:
                            inventory_limited = True
                            break
            finally:
                os.close(directory_fd)
        for path in sorted(candidates):
            _check_deadline(deadline)
            collect(path, "native_output")
    except TimeoutError:
        collection_deadline_reached = True
        inventory_limited = True
        for record in inventory:
            if "status" not in record:
                record["status"] = "collection_time_limit_omitted"
    except OSError:
        inventory_limited = True
    from . import worker
    from .methods import boltz2, openfold3
    code = {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (worker, boltz2, openfold3)}
    code[Path(__file__).name] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    descriptor = {
        "format_version": VERSION, "run_id": task["task_id"],
        "attempt_count": claim["attempt_count"], "worker_id": claim["lease_owner"],
        "registered_task_sha256": claim["task_sha256"],
        "effective_task_sha256": hashlib.sha256(canonical_bytes(task)).hexdigest(),
        "method": task["method"], "method_version": task["method_version"],
        "container_image": task["container_image"], "code_sha256": code,
        "failure": {key: result[key] for key in ("error_code", "failure_stage", "validation_failure", "validation_exception_type", "exit_code") if key in result},
        "limits": {"native_files": MAX_NATIVE_FILES, "native_file_bytes": MAX_FILE_BYTES, "total_bytes": MAX_TOTAL_BYTES, "log_bytes": MAX_LOG_BYTES, "inventory": MAX_INVENTORY, "directories": MAX_DIRECTORIES},
        "inventory_limited": inventory_limited, "scanned_entries": scanned_entries,
        "collection_deadline_reached": collection_deadline_reached, "files": inventory,
    }
    if redact(canonical_bytes(descriptor).decode(), secrets, deadline=None if deadline is None else deadline + 5.0) != canonical_bytes(descriptor).decode():
        raise ValueError("sensitive diagnostic identity")
    return descriptor, files
