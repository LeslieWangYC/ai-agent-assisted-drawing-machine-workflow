from __future__ import annotations

import inspect
import json
import multiprocessing
import os
import subprocess
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from drawingmachine.adapters.hardware.fake_fluidnc import (
    FakeFluidNCOperation,
    FakeFluidNCStep,
    StrictFakeFluidNCFactory,
)
from drawingmachine.adapters.persistence.machine_sql import SQLiteMachineRepository
from drawingmachine.application.machine.commands import HomeMachineCommand, MachineCommandMode
from drawingmachine.application.machine.execution import MachineCommandResult
from drawingmachine.bootstrap import build_runtime
from drawingmachine.config import load_config, resolve_xdg_paths
from drawingmachine.domain.gcode import parse_machine_build_profile
from drawingmachine.domain.gcode.candidate import check_candidate
from drawingmachine.domain.jobs import ArtifactRef, ConfigSnapshot, ReadyToRunSnapshot
from drawingmachine.domain.machine import AuthenticatedMachinePrincipal, MachinePhase, StreamMilestone
from drawingmachine.ports.fluidnc import (
    InitialPreflightRequest,
    OpenSessionRequest,
    StreamOutcome,
    StreamProgramRequest,
)
from drawingmachine.protocol import PROTOCOL_VERSION, ProtocolRequest
from drawingmachine.service.models import PeerCredentials, RequestContext
from tests.conftest import (
    _package_b_environment,
    assert_package_c_repository_residue_unchanged,
    package_c_descendant_pids,
    package_c_repository_residue_snapshot,
)
from tests.unit.application.machine.test_admission import GCODE, _identities, _job, _preflight, _seed, _uuid
from tests.unit.application.machine.test_home import (
    OPERATOR,
    _chain,
    _challenge_id,
    _home_outcome,
    _home_request,
    _snapshot,
)
from tests.unit.application.machine.test_home_z import (
    _execute_home_z,
    _home_z_success_script,
)
from tests.unit.application.machine.test_stream import _execute_stream, _prepare_to_stream, _program, _stream_script


def test_bootstrap_exposes_only_explicit_object_fake_injection_seams() -> None:
    parameters = inspect.signature(build_runtime).parameters
    assert parameters["fluidnc_factory"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["machine_identity_factory"].kind is inspect.Parameter.KEYWORD_ONLY


def test_bootstrap_starts_and_migrates_without_opening_explicit_fake(
    valid_xdg_paths: object,
) -> None:
    paths = valid_xdg_paths
    epoch = _uuid(2)
    factory = StrictFakeFluidNCFactory(OpenSessionRequest(epoch), ())
    identities = _identities()
    runtime = build_runtime(  # type: ignore[arg-type]
        paths,
        fluidnc_factory=factory,
        machine_identity_factory=lambda: next(identities),
    )
    runtime.start()
    try:
        assert factory.connections_opened == 0
        repository = SQLiteMachineRepository(paths.database_file)  # type: ignore[attr-defined]
        try:
            version = repository._get_connection().execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            assert version == 5
        finally:
            repository.close()
    finally:
        runtime.stop()
    assert factory.connections_opened == 0


def test_complete_ready_to_stream_chain_uses_exact_program_once_on_one_connection(tmp_path: Path) -> None:
    epoch = _uuid(2)
    program = _program()
    final = _snapshot(
        mpos=(192.0, 192.0, 99.5),
        wpos=(96.0, 96.0, 3.5),
        wco=(96.0, 96.0, 96.0),
        g54=(96.0, 96.0, 96.0),
    )
    outcome = StreamOutcome(epoch, len(program.commands), len(program.commands), final)
    steps = _stream_script(epoch, outcome)
    chain, repository, factory, execution_id = _chain(tmp_path, steps)
    try:
        from drawingmachine.application.machine.commands import ZCalMachineCommand, ZConfirmMachineCommand
        from tests.unit.application.machine.test_home import _run_action

        _run_action(chain, repository, HomeMachineCommand, execution_id, "home-e2e")
        _run_action(chain, repository, ZCalMachineCommand, execution_id, "zcal-e2e")
        _run_action(chain, repository, ZConfirmMachineCommand, execution_id, "zconfirm-e2e")
        completed = _execute_stream(chain, execution_id, "stream-e2e")

        assert completed.phase is MachinePhase.COMPLETED
        assert completed.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        assert factory.connections_opened == 1
        assert factory.started_operations == (
            FakeFluidNCOperation.INITIAL_PREFLIGHT,
            FakeFluidNCOperation.HOME_ALL_AXES,
            FakeFluidNCOperation.Z_CALIBRATION,
            FakeFluidNCOperation.Z_CONFIRMATION_RAISE,
            FakeFluidNCOperation.STREAM_PROGRAM,
            FakeFluidNCOperation.CLOSE,
        )
        stream_calls = [call for call in factory.calls if call.operation is FakeFluidNCOperation.STREAM_PROGRAM]
        assert len(stream_calls) == 1
        request = cast(StreamProgramRequest, stream_calls[0].request)
        assert request.program == program
        assert request.program.gcode_sha256 == program.gcode_sha256
        assert request.program.source_size_bytes == program.source_size_bytes
        assert tuple((item.source_line, item.command) for item in request.program.commands) == tuple(
            (item.source_line, item.command) for item in program.commands
        )
        factory.assert_script_consumed()
    finally:
        repository.close()


def test_optional_home_z_is_separately_approved_after_confirmed_stream(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, execution_id = _prepare_to_stream(tmp_path, _home_z_success_script(epoch))
    chain._config = replace(chain._config, post_run_home_z=True)  # type: ignore[attr-defined]
    try:
        streamed = _execute_stream(chain, execution_id, "stream-home-z-e2e")
        assert streamed.phase is MachinePhase.AWAITING_HOME_Z_APPROVAL
        assert streamed.stream_milestone is StreamMilestone.STREAM_CONFIRMED
        completed = _execute_home_z(chain, execution_id)
        assert completed.phase is MachinePhase.COMPLETED
        assert completed.stream_milestone is StreamMilestone.STREAM_CONFIRMED
    finally:
        repository.close()


def test_operator_can_request_home_then_consume_with_later_pid_and_fixed_purpose(tmp_path: Path) -> None:
    epoch = _uuid(2)
    chain, repository, factory, execution_id = _chain(
        tmp_path,
        (FakeFluidNCStep(FakeFluidNCOperation.HOME_ALL_AXES, _home_request(), _home_outcome(epoch)),),
    )
    try:
        requested = cast(
            MachineCommandResult,
            chain.admit_action(
                HomeMachineCommand("operator-request", OPERATOR, execution_id, MachineCommandMode.REQUEST, None)
            ),
        )
        challenge = cast(dict[str, object], requested.response["challenge"])
        assert challenge["purpose"] == "motor high-voltage connected, physical area clear, approve HOME"
        admitted = chain.admit_action(
            HomeMachineCommand(
                "operator-execute",
                AuthenticatedMachinePrincipal(OPERATOR.uid, OPERATOR.gid, OPERATOR.pid + 9999),
                execution_id,
                MachineCommandMode.EXECUTE,
                _challenge_id(requested),
            )
        )
        result = chain.drive_admitted_action(admitted)
        assert result.phase is MachinePhase.AWAITING_ZCAL_APPROVAL
        assert factory.started_operations.count(FakeFluidNCOperation.HOME_ALL_AXES) == 1
    finally:
        repository.close()


def _installed_service_probe(root: Path) -> dict[str, object]:
    before_fds = len(tuple(Path("/proc/self/fd").iterdir()))
    before_threads = tuple(thread.name for thread in threading.enumerate())
    before_active_children = tuple(child.pid for child in multiprocessing.active_children())
    before_proc_children = package_c_descendant_pids()
    assert before_active_children == () and before_proc_children == ()
    environment = _package_b_environment(root)
    paths = resolve_xdg_paths(environment, home=root / "home")
    config = load_config(paths)
    digest = __import__("hashlib").sha256(GCODE).hexdigest()
    checked = check_candidate(
        GCODE.decode("utf-8"),
        profile=parse_machine_build_profile(config.machine),
        expected_gcode_sha256=digest,
    )
    job_id = _uuid(101)
    relative_path = "artifacts/attempt/drawing.gcode"
    snapshot = ReadyToRunSnapshot(
        1,
        job_id,
        7,
        ArtifactRef("gcode", relative_path, digest, len(GCODE), "text/x.gcode"),
        (),
        checked.static_result.to_json(),
        checked.send_plan.to_json(),
        checked.readiness.to_json(),
        ConfigSnapshot(
            config.application.machine_profile,
            config.application.provider_profile,
            config.application.schema_version,
            config.machine.schema_version,
            config.provider.schema_version,
            config.digests["application"],
            config.digests["machine"],
            config.digests["provider"],
        ),
        {},
        "0.2.0.dev0",
    )
    artifact = paths.jobs_dir / job_id / relative_path
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(GCODE)
    artifact.chmod(0o600)

    identities = _identities()
    epoch = _uuid(2)
    scripted = _home_z_success_script(epoch)
    factory = StrictFakeFluidNCFactory(
        OpenSessionRequest(epoch),
        (
            FakeFluidNCStep(FakeFluidNCOperation.INITIAL_PREFLIGHT, InitialPreflightRequest(), _preflight(epoch)),
            *scripted,
        ),
    )
    runtime = build_runtime(
        paths,
        fluidnc_factory=factory,
        machine_identity_factory=lambda: next(identities),
    )
    runtime.start()
    repository = SQLiteMachineRepository(paths.database_file)
    try:
        _seed(repository, _job(snapshot))
    finally:
        repository.close()

    counter = iter(range(201, 500))

    def dispatch(
        command: str,
        arguments: dict[str, object],
        *,
        operator: bool = False,
        request_id: str | None = None,
        pid: int | None = None,
    ) -> dict[str, object]:
        principal = (
            PeerCredentials(41000 + next(counter) if pid is None else pid, 2200, 2200)
            if operator
            else PeerCredentials(31000 + next(counter) if pid is None else pid, 1100, 1100)
        )
        response = runtime.dispatcher.dispatch(
            ProtocolRequest(
                PROTOCOL_VERSION,
                command,
                _uuid(next(counter)) if request_id is None else request_id,
                arguments,
            ),
            RequestContext(principal, datetime.now(UTC)),
        )
        assert response.ok, response.error
        return cast(dict[str, object], response.data)

    def wait_phase(execution_id: str, expected: MachinePhase) -> dict[str, object]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            data = dispatch("machine.status", {"schema_version": 1, "execution_id": execution_id})
            execution = cast(dict[str, object], data["execution"])
            if execution["phase"] == expected.value:
                return execution
            time.sleep(0.01)
        raise AssertionError(f"installed service did not reach {expected.value}")

    try:
        prepared = dispatch("machine.prepare", {"schema_version": 1, "job_id": job_id, "job_revision": 7})
        execution_id = cast(str, cast(dict[str, object], prepared["execution"])["execution_id"])
        wait_phase(execution_id, MachinePhase.AWAITING_HOME_APPROVAL)
        home_purpose: object = None
        cross_pid_request_replay = False
        cross_pid_execute_replay = False
        for command, phase in (
            ("machine.home", MachinePhase.AWAITING_ZCAL_APPROVAL),
            ("machine.zcal", MachinePhase.AWAITING_ZCONFIRM_APPROVAL),
            ("machine.zconfirm", MachinePhase.AWAITING_STREAM_APPROVAL),
            ("machine.stream", MachinePhase.AWAITING_HOME_Z_APPROVAL),
            ("machine.home-z", MachinePhase.COMPLETED),
        ):
            request_arguments = {
                "schema_version": 1,
                "execution_id": execution_id,
                "mode": "request",
                "challenge_id": None,
            }
            request_id = _uuid(next(counter))
            requested = dispatch(
                command,
                request_arguments,
                request_id=request_id,
                pid=31001 if command == "machine.home" else None,
            )
            challenge = cast(dict[str, object], requested["challenge"])
            if command == "machine.home":
                home_purpose = challenge["purpose"]
                replayed_request = dispatch(
                    command,
                    request_arguments,
                    request_id=request_id,
                    pid=31999,
                )
                cross_pid_request_replay = replayed_request["deduplicated"] is True
                assert replayed_request["challenge"] == challenge
            execute_arguments = {
                "schema_version": 1,
                "execution_id": execution_id,
                "mode": "execute",
                "challenge_id": challenge["challenge_id"],
            }
            execute_id = _uuid(next(counter))
            dispatch(
                command,
                execute_arguments,
                operator=True,
                request_id=execute_id,
                pid=41002 if command == "machine.home" else None,
            )
            if command == "machine.home":
                replayed_execute = dispatch(
                    command,
                    execute_arguments,
                    operator=True,
                    request_id=execute_id,
                    pid=41999,
                )
                cross_pid_execute_replay = replayed_execute["deduplicated"] is True
            wait_phase(execution_id, phase)
        final = dispatch("machine.status", {"schema_version": 1, "execution_id": execution_id})
        factory.assert_script_consumed()
        ledger = SQLiteMachineRepository(paths.database_file)
        try:
            database_versions = (
                ledger._get_connection().execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            )
        finally:
            ledger.close()
        evidence = {
            "phase": cast(dict[str, object], final["execution"])["phase"],
            "connections": factory.connections_opened,
            "operations": [operation.value for operation in factory.started_operations],
            "database_versions": database_versions,
            "home_purpose": home_purpose,
            "cross_pid_request_replay": cross_pid_request_replay,
            "cross_pid_execute_replay": cross_pid_execute_replay,
        }
    finally:
        runtime.stop()
    after_threads = tuple(thread.name for thread in threading.enumerate())
    after_fds = len(tuple(Path("/proc/self/fd").iterdir()))
    after_active_children = tuple(child.pid for child in multiprocessing.active_children())
    after_proc_children = package_c_descendant_pids()
    assert after_threads == before_threads
    assert after_fds <= before_fds
    assert after_active_children == before_active_children == ()
    assert after_proc_children == before_proc_children == ()
    assert not paths.socket_file.exists()
    evidence.update(
        {
            "socket_exists": False,
            "threads_before": list(before_threads),
            "threads_after": list(after_threads),
            "fds_before": before_fds,
            "fds_after": after_fds,
            "multiprocessing_children": list(after_active_children),
            "proc_children": list(after_proc_children),
            "proc_children_supported": Path(f"/proc/{os.getpid()}/task").is_dir(),
        }
    )
    return evidence


def test_wheel_installed_subprocess_runs_complete_fake_chain_with_no_live_event(
    installed_drawingmachine: Path,
    package_c_audit_guard: Any,
    repository_root: Path,
    tmp_path: Path,
) -> None:
    python = installed_drawingmachine.parent / "python"
    root = tmp_path / "installed-fake-chain"
    root.mkdir()
    before_residue = package_c_repository_residue_snapshot(repository_root)
    before_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    expected_parent_descendants = package_c_descendant_pids()
    environment = package_c_audit_guard.environment.copy()
    environment["PYTHONPATH"] = os.pathsep.join((environment["PYTHONPATH"], str(repository_root)))
    code = (
        "import json, pathlib; import drawingmachine; "
        "from tests.e2e.test_machine_ready_to_completed import _installed_service_probe; "
        f"assert not pathlib.Path(drawingmachine.__file__).resolve().is_relative_to(pathlib.Path({str(repository_root / 'src')!r})); "
        f"print(json.dumps(_installed_service_probe(pathlib.Path({str(root)!r})), sort_keys=True))"
    )
    result = subprocess.run(
        [str(python), "-c", code],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert_package_c_repository_residue_unchanged(
        repository_root,
        before_residue,
        expected_descendants=expected_parent_descendants,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence.pop("threads_before") == evidence.pop("threads_after") == ["MainThread"]
    assert evidence.pop("fds_after") <= evidence.pop("fds_before")
    assert evidence.pop("multiprocessing_children") == []
    assert evidence.pop("proc_children") == []
    assert isinstance(evidence.pop("proc_children_supported"), bool)
    assert evidence == {
        "connections": 1,
        "cross_pid_execute_replay": True,
        "cross_pid_request_replay": True,
        "database_versions": 5,
        "home_purpose": "motor high-voltage connected, physical area clear, approve HOME",
        "operations": [
            "INITIAL_PREFLIGHT",
            "HOME_ALL_AXES",
            "Z_CALIBRATION",
            "Z_CONFIRMATION_RAISE",
            "STREAM_PROGRAM",
            "HOME_Z_AXIS",
            "CLOSE",
        ],
        "phase": "COMPLETED",
        "socket_exists": False,
    }
    assert package_c_audit_guard.events() == []
    assert not any(root.rglob("*.sock"))
    after_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after_status == before_status
