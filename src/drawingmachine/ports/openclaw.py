from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypeVar

from drawingmachine.config.openclaw_deployment import ServiceWriterSnapshotV1

_CheckResultT_co = TypeVar("_CheckResultT_co", covariant=True)
_InstallResultT_co = TypeVar("_InstallResultT_co", covariant=True)


class OpenClawDeploymentPort(Protocol[_CheckResultT_co, _InstallResultT_co]):
    def check(
        self,
        explicit_root: Path,
        service_writer_snapshot: ServiceWriterSnapshotV1,
    ) -> _CheckResultT_co: ...

    def install(
        self,
        explicit_root: Path,
        request_id: str,
        service_writer_snapshot: ServiceWriterSnapshotV1,
    ) -> _InstallResultT_co: ...


__all__ = ["OpenClawDeploymentPort"]
