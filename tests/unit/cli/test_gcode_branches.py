from __future__ import annotations

from typing import NoReturn

import pytest

from drawingmachine.cli.gcode import _static_result, _strict_json_object
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError


class UnusedReader:
    def read_bytes(
        self,
        job_id: str,
        artifact: ArtifactRef,
        *,
        expected_media_type: str,
        max_bytes: int,
    ) -> NoReturn:
        del job_id, artifact, expected_media_type, max_bytes
        raise AssertionError("invalid metadata must fail before artifact access")


def test_static_result_requires_artifact_array_and_exactly_one_match() -> None:
    reader = UnusedReader()
    for status in ({}, {"artifacts": []}, {"artifacts": [{"role": "other"}]}):
        with pytest.raises(DrawingMachineError):
            _static_result(reader, "job-1", status)


def test_static_result_rejects_invalid_reference_and_media_type() -> None:
    reader = UnusedReader()
    with pytest.raises(DrawingMachineError):
        _static_result(reader, "job-1", {"artifacts": [{"role": "gcode_static"}]})

    artifact = ArtifactRef(
        "gcode_static",
        "artifacts/attempt/static.txt",
        "a" * 64,
        2,
        "text/plain",
    )
    with pytest.raises(DrawingMachineError):
        _static_result(reader, "job-1", {"artifacts": [artifact.to_json()]})


@pytest.mark.parametrize("contents", [b"[]", b"1e400", b'{"value": NaN}'])
def test_strict_static_result_rejects_nonobject_and_nonfinite_numbers(contents: bytes) -> None:
    with pytest.raises(DrawingMachineError):
        _strict_json_object(contents, "job-1")
