from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

_SEMANTIC_DEFINITION_PREFIXES = (
    "parse",
    "_parse",
    "load",
    "_load",
    "validate",
    "_validate",
    "authorize",
    "_authorize",
    "apply_approval",
    "consume_approval",
    "allowed_",
    "transition",
    "_transition",
    "reconcile",
    "_reconcile",
    "recover",
    "_recover",
    "claim",
    "_claim",
    "consume",
    "_consume",
    "dispatch",
    "_dispatch",
    "execute",
    "_execute",
    "stream",
    "_stream",
    "install",
    "_install",
    "serve",
    "_serve",
    "classify",
    "_classify",
    "analyze",
    "evaluate",
    "run_",
    "check_",
    "gate_",
    "build_send_plan",
)
_AUTHORITY_TYPE_TOKENS = (
    "authority",
    "command",
    "approval",
    "recovery",
    "phase",
    "progress",
    "state",
    "outcome",
    "policy",
    "access",
    "admission",
    "transition",
    "install",
    "dispatch",
    "blocker",
    "event",
    "failure",
    "evidence",
    "snapshot",
)
_SECURE_WRITE_CALLS = {
    "atomic_write_json",
    "chmod",
    "chown",
    "create_regular_at",
    "fchmod",
    "fsync",
    "fsync_directory",
    "fsync_file",
    "mkdir",
    "rename",
    "rename_noreplace",
    "replace",
    "unlink",
    "write",
    "write_bytes",
    "write_text",
}
_AUTHORITY_ASSIGNMENTS = {"blocker", "error", "outcome", "phase", "state", "status"}


@dataclass(frozen=True, slots=True)
class _CandidateClassification:
    kind: Literal["delegate", "pure-data"]
    owner: str
    reason: str


_SEMANTIC_CLASSIFICATIONS = {
    "src/drawingmachine/adapters/filesystem/_artifact_validation.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/adapters/filesystem/artifact_store.py",
        "private manifest/path validator called only by the artifact-store secure publication owner",
    ),
    "src/drawingmachine/adapters/filesystem/json_files.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/adapters/filesystem/_secure_fs.py",
        "unwired generic JSON helper; descriptor-stable production writes are owned by _secure_fs",
    ),
    "src/drawingmachine/adapters/persistence/migrator.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/adapters/persistence/sqlite_repository.py",
        "schema bootstrap helper invoked by the SQLite repository and unable to admit or transition jobs",
    ),
    "src/drawingmachine/adapters/providers/local_comfyui.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/application/offline_job_chain.py",
        "provider executor; the offline chain selects the effect and accepts or rejects its worker outcome",
    ),
    "src/drawingmachine/adapters/system/clock.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/service/runtime.py",
        "clock implementation only supplies timestamps; runtime owns command and state orchestration",
    ),
    "src/drawingmachine/adapters/workers/process_supervisor.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/application/offline_job_chain.py",
        "process executor; the offline chain owns task selection, retries, outcome identity and promotion",
    ),
    "src/drawingmachine/adapters/workers/resource_limits.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/application/offline_job_chain.py",
        "worker sandbox mechanic applied after the offline chain admits an exact worker task",
    ),
    "src/drawingmachine/adapters/workers/task_registry.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/application/offline_job_chain.py",
        "closed worker-kind function lookup used only after the offline chain constructs a validated task",
    ),
    "src/drawingmachine/adapters/workers/tasks/_payload_validation.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/ports/workers.py",
        "task-local shape checks subordinate to the immutable WorkerTaskV1 payload contract owner",
    ),
    "src/drawingmachine/adapters/workers/tasks/image.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/application/offline_job_chain.py",
        "deterministic worker implementation invoked only through the offline-chain task authority",
    ),
    "src/drawingmachine/adapters/workers/tasks/planning.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/application/offline_job_chain.py",
        "deterministic worker implementation invoked only through the offline-chain task authority",
    ),
    "src/drawingmachine/adapters/workers/worker_entrypoint.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/application/offline_job_chain.py",
        "worker child transport entrypoint; it cannot select a task or accept a promoted outcome",
    ),
    "src/drawingmachine/bootstrap.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/service/runtime.py",
        "composition root wiring already-validated owners; runtime owns the installed command effects",
    ),
    "src/drawingmachine/cli/gcode.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/service/runtime.py",
        "CLI request renderer; installed service runtime owns G-code command admission and effects",
    ),
    "src/drawingmachine/cli/config.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/config/machine.py",
        "config-check entrypoint only invokes existing config parsers and reports their result without new authority",
    ),
    "src/drawingmachine/cli/jobs.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/service/runtime.py",
        "CLI request renderer; installed service runtime owns job lookup and cancellation effects",
    ),
    "src/drawingmachine/cli/main.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/service/runtime.py",
        "CLI composition and output only; service runtime owns the command behavior",
    ),
    "src/drawingmachine/cli/service.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/service/runtime.py",
        "foreground entrypoint composes runtime and server lifecycles; those owners govern state and endpoint effects",
    ),
    "src/drawingmachine/config/loader.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/config/machine.py",
        "generic TOML/JSON loader; machine and dedicated policy parsers own semantic authority",
    ),
    "src/drawingmachine/domain/fluidnc/reports.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/ports/fluidnc.py",
        "report-only projection of typed FluidNC results; the port contracts own outcome classification",
    ),
    "src/drawingmachine/domain/gcode/build.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/domain/jobs/artifact_contracts.py",
        "deterministic G-code producer whose promoted output is admitted by exact artifact contracts",
    ),
    "src/drawingmachine/domain/gcode/preview.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/domain/jobs/artifact_contracts.py",
        "preview calculation cannot authorize streaming; artifact contracts validate its promoted document",
    ),
    "src/drawingmachine/domain/jobs/events.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/domain/jobs/models.py",
        "immutable event and audit data; job models own state-coupled invariant validation",
    ),
    "src/drawingmachine/domain/planning/models.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/application/offline_job_chain.py",
        "planning configuration/data validation; offline chain owns whether planning is dispatched",
    ),
    "src/drawingmachine/domain/workflow/complexity.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/domain/workflow/routing.py",
        "deterministic image metrics only; routing owns the semantic admission decision",
    ),
    "src/drawingmachine/domain/workflow/review.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/domain/workflow/models.py",
        "one-line parser forwarding to ProcessedImageReview.from_json and validate_for authority",
    ),
    "src/drawingmachine/ports/artifacts.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/adapters/filesystem/artifact_store.py",
        "artifact port and immutable DTOs; filesystem artifact store owns validation and publication",
    ),
    "src/drawingmachine/ports/machine_repository.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/adapters/persistence/machine_sql.py",
        "repository protocol and DTOs; machine_sql owns durable admission, transition and outcome commits",
    ),
    "src/drawingmachine/ports/providers.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/application/offline_job_chain.py",
        "provider port/config contract; offline chain owns provider dispatch and outcome acceptance",
    ),
    "src/drawingmachine/ports/repository.py": _CandidateClassification(
        "pure-data",
        "src/drawingmachine/adapters/persistence/sqlite_repository.py",
        "repository protocol; SQLite implementation owns durable job and staging transitions",
    ),
    "src/drawingmachine/protocol/codec.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/service/dispatcher.py",
        "bounded frame codec parses transport shape only; dispatcher owns command selection and failure mapping",
    ),
    "src/drawingmachine/service/_instance.py": _CandidateClassification(
        "delegate",
        "src/drawingmachine/service/server.py",
        "private socket/PID identity primitive used by the server's publication and cleanup authority",
    ),
}


def _load_universal_classifications() -> dict[str, _CandidateClassification]:
    classifications: dict[str, _CandidateClassification] = {}
    fixture = Path("tests/architecture/package_d_source_classifications.txt")
    for line_number, raw_line in enumerate(fixture.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line or raw_line.startswith("#"):
            continue
        fields = raw_line.split("|", maxsplit=3)
        if len(fields) != 4:
            raise AssertionError(f"{fixture}:{line_number}: expected four pipe-delimited fields")
        path, raw_kind, owner, reason = fields
        if raw_kind == "delegate":
            kind: Literal["delegate", "pure-data"] = "delegate"
        elif raw_kind == "pure-data":
            kind = "pure-data"
        else:
            raise AssertionError(f"{fixture}:{line_number}: invalid classification kind {raw_kind!r}")
        if path in classifications:
            raise AssertionError(f"{fixture}:{line_number}: duplicate source path {path}")
        classifications[path] = _CandidateClassification(kind, owner, reason)
    return classifications


_CLASSIFICATIONS = _SEMANTIC_CLASSIFICATIONS | _load_universal_classifications()


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _assignment_targets(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> tuple[ast.expr, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    return (node.target,)


@cache
def _structural_authority_candidates() -> dict[str, frozenset[str]]:
    candidates: dict[str, frozenset[str]] = {}
    for path in sorted(Path("src/drawingmachine").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        reasons: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.lower().startswith(
                _SEMANTIC_DEFINITION_PREFIXES
            ):
                reasons.add(f"semantic-definition:{node.name}")
            elif isinstance(node, ast.ClassDef) and any(
                isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and member.name == "__post_init__"
                for member in node.body
            ):
                reasons.add(f"invariant-post-init:{node.name}")
            elif isinstance(node, ast.ClassDef) and any(token in node.name.lower() for token in _AUTHORITY_TYPE_TOKENS):
                reasons.add(f"authority-type:{node.name}")
            elif isinstance(node, ast.Call) and (name := _call_name(node)) in _SECURE_WRITE_CALLS:
                reasons.add(f"secure-write-call:{name}")
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                for target in _assignment_targets(node):
                    if isinstance(target, ast.Attribute) and target.attr in _AUTHORITY_ASSIGNMENTS:
                        reasons.add(f"authority-assignment:{target.attr}")
        if reasons:
            candidates[path.as_posix()] = frozenset(reasons)
    return candidates


@cache
def _source_modules() -> frozenset[str]:
    return frozenset(path.as_posix() for path in Path("src/drawingmachine").rglob("*.py"))


def _discovery_errors(
    manifest: set[str],
    classifications: dict[str, _CandidateClassification],
) -> tuple[str, ...]:
    source_modules = set(_source_modules())
    errors = {f"unclassified source module: {path}" for path in source_modules - manifest - classifications.keys()}
    errors.update(f"manifest owner is not a source module: {path}" for path in manifest - source_modules)
    errors.update(f"stale source classification: {path}" for path in classifications.keys() - source_modules)
    errors.update(f"owner is also classified: {path}" for path in manifest & classifications.keys())
    for path, classification in classifications.items():
        if classification.owner not in manifest:
            errors.add(f"classification owner is not manifested: {path} -> {classification.owner}")
        if not classification.reason.strip():
            errors.add(f"classification reason is empty: {path}")
    return tuple(sorted(errors))


def _manifest() -> set[str]:
    return {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _source_authority_modules() -> set[str]:
    return set(_source_modules()) - set(_CLASSIFICATIONS)


def test_final_safety_manifest_equals_the_universal_source_authority_inventory() -> None:
    assert _manifest() == _source_authority_modules()


def test_every_source_module_is_an_owner_or_has_a_narrow_classification() -> None:
    assert _discovery_errors(_manifest(), _CLASSIFICATIONS) == ()


def test_universal_source_closure_rejects_every_owner_and_classification_removal() -> None:
    manifest = _manifest()
    for owner in manifest:
        assert _discovery_errors(manifest - {owner}, _CLASSIFICATIONS)
    for candidate in _CLASSIFICATIONS:
        reduced = {path: value for path, value in _CLASSIFICATIONS.items() if path != candidate}
        assert _discovery_errors(manifest, reduced)


def test_final_safety_manifest_rejects_every_single_owner_removal() -> None:
    manifest = _manifest()
    candidates = _source_authority_modules()
    for candidate in candidates:
        assert manifest - {candidate} != candidates


def test_every_fluidnc_authority_parser_and_gate_is_in_the_safety_manifest() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    fluidnc_modules = {
        path.as_posix()
        for path in Path("src/drawingmachine/domain/fluidnc").glob("*.py")
        if path.name not in {"__init__.py", "reports.py"}
    }
    authority_modules = fluidnc_modules | {"src/drawingmachine/domain/gcode/stream_program.py"}
    authority_modules.add("src/drawingmachine/ports/fluidnc.py")
    authority_modules.update(
        path.as_posix()
        for path in Path("src/drawingmachine/adapters/hardware").glob("*.py")
        if path.name != "__init__.py"
    )
    assert authority_modules <= manifest


def test_fluidnc_port_and_strict_fake_are_individual_safety_authorities() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {
        "src/drawingmachine/ports/fluidnc.py",
        "src/drawingmachine/adapters/hardware/fake_fluidnc.py",
        "src/drawingmachine/adapters/hardware/serial_fluidnc.py",
    } <= manifest


def test_every_c9_application_machine_authority_is_in_the_safety_manifest() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    authority_modules = {
        path.as_posix()
        for path in Path("src/drawingmachine/application/machine").glob("*.py")
        if path.name != "__init__.py"
    }
    assert authority_modules == {
        "src/drawingmachine/application/machine/commands.py",
        "src/drawingmachine/application/machine/execution.py",
        "src/drawingmachine/application/machine/recovery.py",
    }
    assert authority_modules <= manifest


def test_frozen_package_c_manifest_covers_every_authority_family_and_motion_caller() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    expected = {
        "src/drawingmachine/config/machine.py",
        "src/drawingmachine/ports/fluidnc.py",
        "src/drawingmachine/adapters/persistence/machine_sql.py",
        "src/drawingmachine/service/machine_coordinator.py",
        "src/drawingmachine/service/machine_protocol.py",
        "src/drawingmachine/domain/gcode/stream_program.py",
    }
    expected.update(
        path.as_posix()
        for directory in (
            Path("src/drawingmachine/adapters/hardware"),
            Path("src/drawingmachine/application/machine"),
        )
        for path in directory.glob("*.py")
        if path.name != "__init__.py"
    )
    expected.update(
        path.as_posix()
        for path in Path("src/drawingmachine/domain/fluidnc").glob("*.py")
        if path.name not in {"__init__.py", "reports.py"}
    )
    expected.update(
        path.as_posix() for path in Path("src/drawingmachine/domain/machine").glob("*.py") if path.name != "__init__.py"
    )
    assert expected <= manifest

    motion_tokens = (
        ".home_all_axes(",
        ".run_z_calibration(",
        ".raise_after_z_confirmation(",
        ".stream_program(",
        ".home_z_axis(",
    )
    callers = {
        path.as_posix()
        for path in Path("src/drawingmachine").rglob("*.py")
        if any(token in path.read_text(encoding="utf-8") for token in motion_tokens)
    }
    assert callers == {"src/drawingmachine/application/machine/execution.py"}
    assert callers <= manifest


def test_package_d_service_access_authorities_are_individually_manifested() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {
        "src/drawingmachine/config/service_access.py",
        "src/drawingmachine/service/access_policy.py",
        "src/drawingmachine/cli/service_access.py",
        "src/drawingmachine/cli/client.py",
    } <= manifest


def test_package_d_openclaw_service_protocol_is_individually_manifested() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "src/drawingmachine/service/openclaw_protocol.py" in manifest
    source = Path("src/drawingmachine/service/openclaw_protocol.py").read_text(encoding="utf-8")
    assert "drawingmachine.adapters" not in source


def test_final_manifest_covers_every_client_data_staging_and_endpoint_authority() -> None:
    manifest = {
        line.strip()
        for line in Path("tests/architecture/package_c_safety_modules.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {
        "src/drawingmachine/ports/client_data.py",
        "src/drawingmachine/adapters/filesystem/client_data.py",
        "src/drawingmachine/application/staging_reconciliation.py",
        "src/drawingmachine/install/client_endpoint.py",
    } <= manifest
