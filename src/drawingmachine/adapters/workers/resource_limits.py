from __future__ import annotations

import resource as _resource
from dataclasses import dataclass

from drawingmachine.ports.artifacts import MAX_GCODE_IMPORT_BYTES, MAX_IMAGE_IMPORT_BYTES
from drawingmachine.ports.workers import WorkerKind, WorkerOutcomeV1, WorkerTaskV1


class WorkerResourceLimitError(RuntimeError):
    """A worker crossed or could not install its bounded resource contract."""


@dataclass(frozen=True, slots=True)
class WorkerResourcePolicy:
    max_input_file_bytes: int
    max_output_file_bytes: int
    max_output_bundle_bytes: int
    max_image_pixels: int
    address_space_bytes: int
    cpu_seconds: int
    open_files: int


_MIB = 1024 * 1024
_IMAGE_PIXELS = 25_000_000
_ADDRESS_SPACE = 2 * 1024 * _MIB

_POLICIES: dict[WorkerKind, WorkerResourcePolicy] = {
    WorkerKind.IMAGE_PREPARE: WorkerResourcePolicy(
        MAX_IMAGE_IMPORT_BYTES, 32 * _MIB, 64 * _MIB, _IMAGE_PIXELS, _ADDRESS_SPACE, 300, 64
    ),
    WorkerKind.PROVIDER_LOCAL_COMFYUI: WorkerResourcePolicy(
        MAX_IMAGE_IMPORT_BYTES, 32 * _MIB, 64 * _MIB, _IMAGE_PIXELS, _ADDRESS_SPACE, 300, 64
    ),
    WorkerKind.PATHS_PLAN: WorkerResourcePolicy(
        MAX_IMAGE_IMPORT_BYTES, 16 * _MIB, 32 * _MIB, _IMAGE_PIXELS, _ADDRESS_SPACE, 300, 64
    ),
    WorkerKind.GCODE_BUILD: WorkerResourcePolicy(
        MAX_GCODE_IMPORT_BYTES, 16 * _MIB, 32 * _MIB, 1, _ADDRESS_SPACE, 300, 64
    ),
    WorkerKind.GCODE_CHECK: WorkerResourcePolicy(
        MAX_GCODE_IMPORT_BYTES, 4 * _MIB, 8 * _MIB, 1, _ADDRESS_SPACE, 300, 64
    ),
    WorkerKind.TEST_ECHO: WorkerResourcePolicy(32 * _MIB, 32 * _MIB, 32 * _MIB, _IMAGE_PIXELS, _ADDRESS_SPACE, 60, 64),
    WorkerKind.TEST_HANG: WorkerResourcePolicy(32 * _MIB, 32 * _MIB, 32 * _MIB, _IMAGE_PIXELS, _ADDRESS_SPACE, 60, 64),
}


def policy_for(kind: WorkerKind) -> WorkerResourcePolicy:
    try:
        return _POLICIES[kind]
    except KeyError as error:  # pragma: no cover - WorkerKind is closed, fail closed for future additions.
        raise WorkerResourceLimitError(f"worker kind has no resource policy: {kind}") from error


def validate_outcome(
    task: WorkerTaskV1,
    outcome: WorkerOutcomeV1,
    *,
    policy: WorkerResourcePolicy | None = None,
) -> None:
    selected = policy_for(task.kind) if policy is None else policy
    total = 0
    for artifact in outcome.artifacts:
        if artifact.size_bytes > selected.max_output_file_bytes:
            raise WorkerResourceLimitError("worker artifact exceeds its per-file output limit")
        total += artifact.size_bytes
        if total > selected.max_output_bundle_bytes:
            raise WorkerResourceLimitError("worker artifacts exceed their bundle output limit")


def _install_limit(name: int, requested: int) -> None:
    _soft, current_hard = _resource.getrlimit(name)
    selected = requested if current_hard == _resource.RLIM_INFINITY else min(requested, current_hard)
    if selected <= 0:
        raise WorkerResourceLimitError("worker resource limit cannot be installed")
    _resource.setrlimit(name, (selected, selected))


def apply_process_limits(policy: WorkerResourcePolicy) -> None:
    try:
        _install_limit(_resource.RLIMIT_AS, policy.address_space_bytes)
        _install_limit(_resource.RLIMIT_CPU, policy.cpu_seconds)
        _install_limit(_resource.RLIMIT_FSIZE, policy.max_output_file_bytes)
        _install_limit(_resource.RLIMIT_NOFILE, policy.open_files)
    except (OSError, ValueError) as error:
        raise WorkerResourceLimitError("worker process resource limits could not be installed") from error


__all__ = [
    "WorkerResourceLimitError",
    "WorkerResourcePolicy",
    "apply_process_limits",
    "policy_for",
    "validate_outcome",
]
