"""Execution and storage seams implemented by local or remote backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol


class ExecutionBackend(Protocol):
    def submit(self, task: Mapping[str, Any]) -> str:
        """Submit one normalized task and return the backend's opaque job ID."""


class ArtifactStore(Protocol):
    def materialize(self, uri: str, destination: Path, sha256: str) -> Path:
        """Fetch and verify an immutable input artifact."""

    def publish(self, source: Path, uri: str, sha256: str) -> None:
        """Publish an output at the task's immutable URI."""


class ControlPlane(Protocol):
    def claim(self, task_id: str, worker_id: str) -> bool:
        """Atomically claim an unstarted or retryable prediction run."""

    def finish(self, result: Mapping[str, Any]) -> None:
        """Persist the normalized result after its artifacts are durable."""
