from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.adapters.filesystem import FilesystemArtifactStore
from drawingmachine.adapters.hardware.fake_fluidnc import StrictFakeFluidNCFactory
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine.execution import MachineExecutionChain
from drawingmachine.config import XdgPaths
from drawingmachine.domain.gcode.stream_program import ValidatedStreamProgram
from drawingmachine.domain.jobs import ArtifactRef
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.fluidnc import OpenSessionRequest
from tests.unit.application.machine.test_admission import (
    GCODE,
    NOW,
    _authority,
    _command,
    _config,
    _identities,
    _job,
    _ready_snapshot,
    _seed,
    _uuid,
)


def _fixture(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> tuple[FilesystemArtifactStore, ArtifactRef]:
    source = tmp_path / "drawing.gcode"
    source.write_bytes(GCODE)
    store = FilesystemArtifactStore(valid_xdg_paths)
    promoted = store.import_file(
        "job-c9",
        "attempt-c9",
        source,
        role="gcode",
        relative_path="drawing.gcode",
        media_type="text/x.gcode",
        max_bytes=len(GCODE),
    )
    return store, promoted.artifacts[0]


def _chain(
    tmp_path: Path,
    store: FilesystemArtifactStore,
    artifact: ArtifactRef,
) -> tuple[MachineExecutionChain, SQLiteMachineRepository, StrictFakeFluidNCFactory]:
    repository = SQLiteMachineRepository(tmp_path / "machine.db")
    repository.initialize(applied_at=NOW.isoformat())
    _seed(repository, _job(_ready_snapshot(gcode=artifact)))
    identities = _identities()
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(_uuid(2)), ())
    chain = MachineExecutionChain(
        repository,
        store,
        factory,
        _config(),
        _authority(),
        identity_factory=lambda: next(identities),
        wall_now=lambda: NOW + timedelta(seconds=1),
        monotonic_now=lambda: 1000.0,
    )
    return chain, repository, factory


def test_real_descriptor_reader_builds_complete_program_before_zero_io_admission(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    store, artifact = _fixture(valid_xdg_paths, tmp_path)
    chain, repository, factory = _chain(tmp_path, store, artifact)
    try:
        admission = chain.admit_action(_command())
        program = cast(ValidatedStreamProgram, cast(object, admission).program)  # type: ignore[attr-defined]
        assert program.gcode_sha256 == artifact.sha256
        assert tuple(item.command for item in program.commands) != tuple(
            item["command"]
            for item in cast(list[dict[str, object]], _ready_snapshot().send_plan["first_sendable_lines"])
        )
        assert len(program.commands) == program.send_plan.sendable_line_count
        assert factory.connections_opened == 0
    finally:
        repository.close()


@pytest.mark.parametrize("replacement", ["bytes", "symlink"])
def test_descriptor_reader_rejects_content_or_inode_replacement_before_reservation(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
    replacement: str,
) -> None:
    store, artifact = _fixture(valid_xdg_paths, tmp_path)
    path = store.resolve("job-c9", artifact)
    if replacement == "bytes":
        path.write_bytes(GCODE + b"; tampered\n")
    else:
        outside = tmp_path / "outside.gcode"
        outside.write_bytes(GCODE)
        os.unlink(path)
        path.symlink_to(outside)
    chain, repository, factory = _chain(tmp_path, store, artifact)
    try:
        with pytest.raises(DrawingMachineError):
            chain.admit_action(_command())
        assert repository.get_active_execution() is None
        assert factory.connections_opened == 0
    finally:
        repository.close()
