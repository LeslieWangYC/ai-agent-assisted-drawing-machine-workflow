from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from drawingmachine.domain.machine.models import (
    SCHEMA_VERSION,
    MachineFailureEvidenceV1,
    _integer,
    _invalid,
    _string,
    _timestamp,
)


@dataclass(frozen=True, slots=True)
class MachineProgress:
    schema_version: int
    execution_id: str
    execution_revision: int
    total_lines: int
    acknowledged_lines: int
    ok_count: int
    error_count: int
    percent: float
    last_source_line: int | None
    last_command: str | None
    last_event_at: datetime
    failure: MachineFailureEvidenceV1 | None = None

    def __post_init__(self) -> None:
        if _integer(self.schema_version, "progress.schema_version", minimum=1) != SCHEMA_VERSION:
            _invalid("progress schema version is unsupported", field="progress.schema_version")
        _string(self.execution_id, "progress.execution_id")
        _integer(self.execution_revision, "progress.execution_revision")
        total = _integer(self.total_lines, "progress.total_lines")
        acknowledged = _integer(self.acknowledged_lines, "progress.acknowledged_lines")
        ok_count = _integer(self.ok_count, "progress.ok_count")
        error_count = _integer(self.error_count, "progress.error_count")
        post_stream_failure = acknowledged == total and error_count == 1
        if acknowledged != ok_count or (acknowledged + error_count > total and not post_stream_failure):
            _invalid("progress counters are inconsistent")
        if isinstance(self.percent, bool) or not isinstance(self.percent, int | float):
            _invalid("progress.percent must be numeric", field="progress.percent")
        percent = float(self.percent)
        if not math.isfinite(percent):
            _invalid("progress.percent must be finite", field="progress.percent")
        expected_percent = 100.0 if total == 0 else acknowledged * 100.0 / total
        if not math.isclose(percent, expected_percent, rel_tol=0.0, abs_tol=1e-9):
            _invalid("progress.percent must exactly reflect acknowledged lines")
        object.__setattr__(self, "percent", percent)
        if self.last_source_line is not None:
            _integer(self.last_source_line, "progress.last_source_line", minimum=1)
        if self.last_command is not None:
            _string(self.last_command, "progress.last_command")
        has_attempt = acknowledged + error_count > 0
        if has_attempt != (self.last_source_line is not None and self.last_command is not None):
            _invalid("progress last source line and command must track the last attempted line")
        _timestamp(self.last_event_at, "progress.last_event_at")
        if self.failure is not None and type(self.failure) is not MachineFailureEvidenceV1:
            _invalid("progress.failure must be exact MachineFailureEvidenceV1")
        if (error_count > 0) != (self.failure is not None):
            _invalid("progress failure evidence must match error_count")
