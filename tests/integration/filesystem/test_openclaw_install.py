from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

import drawingmachine.adapters.filesystem.openclaw_install as subject
from drawingmachine.adapters.filesystem.openclaw_install import OpenClawFilesystemAdapter
from drawingmachine.config.openclaw_deployment import (
    ServiceWriterSnapshotV1,
    parse_openclaw_deployment_policy,
)
from drawingmachine.domain.openclaw import (
    OpenClawCheckStatus,
    OpenClawInitialState,
    OpenClawInstallFileStatus,
    OpenClawRegistryStatus,
)
from drawingmachine.domain.openclaw.manifest import load_packaged_openclaw_bundle


@pytest.fixture
def valid_policy(tmp_path: Path) -> object:
    registry_root = tmp_path / "registry"
    managed_root = tmp_path / "managed"
    registry_root.mkdir(mode=0o750)
    managed_root.mkdir(mode=0o2750)
    (managed_root / "agents").mkdir()
    for agent_id in ("cnc-drawable-translator", "cnc-drawable-router"):
        (managed_root / "agents" / agent_id).mkdir()
    for directory in (managed_root, managed_root / "agents", *(managed_root / "agents").iterdir()):
        os.chmod(directory, 0o2750)
    bundle = load_packaged_openclaw_bundle()
    registry = {
        "agents": {
            "list": [
                {
                    "id": agent.agent_id,
                    "workspace": str(managed_root / agent.workspace_relative_path),
                    "tools": {"alsoAllow": list(agent.required_tools)},
                }
                for agent in bundle.registry_policy.agents
            ]
        }
    }
    (registry_root / "openclaw.json").write_text(json.dumps(registry), encoding="utf-8")
    os.chmod(registry_root / "openclaw.json", 0o440)
    encoded = f'''schema_version = 1
registry_root = {json.dumps(str(registry_root))}
managed_root = {json.dumps(str(managed_root))}
managed_dir_mode = {0o2750}
target_mode = {0o640}
manifest_sha256 = "{bundle.manifest_sha256}"
registry_policy_sha256 = "{bundle.registry_policy_sha256}"

[operator_principal]
uid = {os.getuid() + 10001}
gid = {os.getgid() + 10001}

[automation_principal]
uid = {os.getuid() + 10002}
gid = {os.getgid() + 10002}

[automation_read_group]
name = "drawingmachine-openclaw-read"
gid = {os.getgid()}

[managed_owner]
uid = {os.getuid()}
gid = {os.getgid()}

[registry_owner]
uid = {os.getuid()}
gid = {os.getgid()}
'''.encode()
    return parse_openclaw_deployment_policy(encoded, source_sha256=hashlib.sha256(encoded).hexdigest())


def _adapter(policy: object) -> OpenClawFilesystemAdapter:
    return OpenClawFilesystemAdapter(policy, ancestor_authority=lambda _path, _status: True)  # type: ignore[arg-type]


def _seed_targets(policy: object, contents: bytes = b"prior\n") -> None:
    for resource in load_packaged_openclaw_bundle().managed_resources:
        target = policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
        target.write_bytes(contents)
        os.chmod(target, 0o640)


def _registry_path(policy: object) -> Path:
    return policy.registry_root / "openclaw.json"  # type: ignore[attr-defined]


def _replace_registry(policy: object, contents: bytes = b"{}") -> None:
    path = _registry_path(policy)
    replacement = path.with_name("openclaw.json.replacement")
    replacement.write_bytes(contents)
    os.chmod(replacement, 0o440)
    os.replace(replacement, path)


def test_absent_install_check_replay_and_undeclared_preservation(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    undeclared = valid_policy.managed_root / "keep.txt"  # type: ignore[attr-defined]
    undeclared.write_text("keep", encoding="utf-8")

    before = adapter.check(valid_policy.registry_root, writer)  # type: ignore[attr-defined]
    assert {row.status for row in before.files} == {OpenClawCheckStatus.MISSING}
    request_id = str(uuid4())
    installed = adapter.install(valid_policy.registry_root, request_id, writer)  # type: ignore[attr-defined]
    assert installed.installed is True
    assert installed.globally_atomic is False
    assert {row.status for row in installed.files} == {OpenClawInstallFileStatus.INSTALLED}
    assert undeclared.read_text(encoding="utf-8") == "keep"

    replay = adapter.install(valid_policy.registry_root, request_id, writer)  # type: ignore[attr-defined]
    assert replay.deduplicated is True
    assert adapter.check(valid_policy.registry_root, writer).in_sync is True  # type: ignore[attr-defined]


def test_registry_drift_and_unsafe_target_make_install_zero_write(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    target = valid_policy.managed_root / "agents/cnc-drawable-router/AGENTS.md"  # type: ignore[attr-defined]
    target.symlink_to(valid_policy.registry_root / "openclaw.json")  # type: ignore[attr-defined]

    checked = adapter.check(valid_policy.registry_root, writer)  # type: ignore[attr-defined]
    assert checked.unsafe is True
    with pytest.raises(ValueError, match=r"unsafe|drift"):
        adapter.install(valid_policy.registry_root, str(uuid4()), writer)  # type: ignore[attr-defined]
    assert target.is_symlink()


def test_present_targets_replace_atomically_and_new_request_observes_unchanged(valid_policy: object) -> None:
    _seed_targets(valid_policy)
    adapter = _adapter(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())

    result = adapter.install(valid_policy.registry_root, str(uuid4()), writer)  # type: ignore[attr-defined]
    assert result.installed is True
    assert {row.status for row in result.files} == {OpenClawInstallFileStatus.REPLACED}
    second = adapter.install(valid_policy.registry_root, str(uuid4()), writer)  # type: ignore[attr-defined]
    assert second.mutation_started is False
    assert {row.status for row in second.files} == {OpenClawInstallFileStatus.UNCHANGED}


@pytest.mark.parametrize(
    "request_id", ["bad", "00000000-0000-1000-8000-000000000000", "ABCDEFAB-CDEF-4ABC-8ABC-ABCDEFABCDEF"]
)
def test_install_rejects_noncanonical_request_ids(valid_policy: object, request_id: str) -> None:
    with pytest.raises(ValueError, match="UUIDv4"):
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            request_id,
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )


def test_call_authority_rejects_root_writer_and_process_mismatch(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    with pytest.raises(ValueError, match="root"):
        adapter.check(valid_policy.managed_root, writer)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="writer"):
        adapter.check(valid_policy.registry_root, ServiceWriterSnapshotV1(os.getuid() + 1, os.getgid()))  # type: ignore[attr-defined]
    monkeypatch.setattr(subject.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ValueError, match="process"):
        adapter.check(valid_policy.registry_root, writer)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data, policy: {},
        lambda data, policy: {"agents": {}},
        lambda data, policy: {"agents": {"list": []}},
        lambda data, policy: {"agents": {"list": [data["agents"]["list"][0]]}},
        lambda data, policy: {"agents": {"list": [*data["agents"]["list"], data["agents"]["list"][0]]}},
        lambda data, policy: {
            "agents": {
                "list": [
                    {**data["agents"]["list"][0], "workspace": str(policy.registry_root)},
                    data["agents"]["list"][1],
                ]
            }
        },
        lambda data, policy: {
            "agents": {
                "list": [
                    {**data["agents"]["list"][0], "tools": {"alsoAllow": []}},
                    data["agents"]["list"][1],
                ]
            }
        },
    ],
)
def test_registry_closed_contract_drift_is_read_only(
    valid_policy: object,
    mutation: object,
) -> None:
    path = _registry_path(valid_policy)
    data = json.loads(path.read_text(encoding="utf-8"))
    os.chmod(path, 0o600)
    path.write_text(json.dumps(mutation(data, valid_policy)), encoding="utf-8")  # type: ignore[operator]
    os.chmod(path, 0o440)
    adapter = _adapter(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    checked = adapter.check(valid_policy.registry_root, writer)  # type: ignore[attr-defined]
    assert checked.registry.status.value == "DIFFERS"
    with pytest.raises(ValueError, match="registry"):
        adapter.install(valid_policy.registry_root, str(uuid4()), writer)  # type: ignore[attr-defined]


def test_registry_missing_duplicate_key_fifo_and_oversize_fail_closed(valid_policy: object) -> None:
    path = _registry_path(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    path.unlink()
    assert _adapter(valid_policy).check(valid_policy.registry_root, writer).registry.status.value == "MISSING"  # type: ignore[attr-defined]
    path.write_text('{"agents":{},"agents":{}}', encoding="utf-8")
    os.chmod(path, 0o440)
    assert _adapter(valid_policy).check(valid_policy.registry_root, writer).registry.status.value == "DIFFERS"  # type: ignore[attr-defined]
    path.unlink()
    os.mkfifo(path)
    assert _adapter(valid_policy).check(valid_policy.registry_root, writer).registry.status.value == "UNSAFE"  # type: ignore[attr-defined]
    path.unlink()
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    os.chmod(path, 0o440)
    assert _adapter(valid_policy).check(valid_policy.registry_root, writer).registry.status.value == "UNSAFE"  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["mode", "hardlink", "fifo"])
def test_unsafe_target_types_and_authority_never_mutate(valid_policy: object, kind: str) -> None:
    target = valid_policy.managed_root / "agents/cnc-drawable-router/AGENTS.md"  # type: ignore[attr-defined]
    if kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_text("prior", encoding="utf-8")
        os.chmod(target, 0o600 if kind == "mode" else 0o640)
        if kind == "hardlink":
            os.link(target, target.with_name("extra-link"))
    checked = _adapter(valid_policy).check(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert checked.unsafe is True


def test_transaction_residue_requires_recovery_without_mutation(valid_policy: object) -> None:
    residue = (
        valid_policy.managed_root  # type: ignore[attr-defined]
        / "agents/cnc-drawable-router/.drawingmachine-openclaw-00000000-0000-4000-8000-000000000000-tmp-0123456789abcdef"
    )
    residue.write_text("evidence", encoding="utf-8")
    os.chmod(residue, 0o640)
    with pytest.raises(ValueError, match="recovery"):
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert residue.exists()


def test_one_shot_commit_failure_restores_initially_absent_targets(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real = subject.fsync_directory

    def fail_once(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected parent fsync failure")
        real(descriptor)

    monkeypatch.setattr(subject, "fsync_directory", fail_once)
    result = _adapter(valid_policy).install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.installed is False
    assert result.rollback_attempted is True
    assert result.rollback_completed is True
    assert all(not (valid_policy.managed_root / row.destination).exists() for row in result.files)  # type: ignore[attr-defined]


def test_one_shot_replace_failure_restores_present_target_bytes(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted-prior")
    real = subject.rename_noreplace
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected replace failure")
        real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subject, "rename_noreplace", fail_once)
    result = _adapter(valid_policy).install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.rollback_completed is True
    assert all((valid_policy.managed_root / row.destination).read_bytes() == b"trusted-prior" for row in result.files)  # type: ignore[attr-defined]


def test_constructor_rejects_untrusted_policy_bundle_and_digest_drift(valid_policy: object) -> None:
    bundle = load_packaged_openclaw_bundle()
    with pytest.raises(TypeError, match="policy"):
        OpenClawFilesystemAdapter(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bundle"):
        OpenClawFilesystemAdapter(valid_policy, bundle=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="manifest"):
        OpenClawFilesystemAdapter(replace(valid_policy, manifest_sha256="f" * 64), bundle=bundle)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="registry"):
        OpenClawFilesystemAdapter(replace(valid_policy, registry_policy_sha256="f" * 64), bundle=bundle)  # type: ignore[arg-type]


def test_unsafe_ancestor_and_managed_directory_mode_fail_check_closed(valid_policy: object) -> None:
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    checked = OpenClawFilesystemAdapter(
        valid_policy,  # type: ignore[arg-type]
        ancestor_authority=lambda _path, _status: False,
    ).check(valid_policy.registry_root, writer)  # type: ignore[attr-defined]
    assert checked.unsafe is True
    assert all(row.status is OpenClawCheckStatus.UNSAFE for row in checked.files)
    os.chmod(valid_policy.managed_root / "agents", 0o750)  # type: ignore[attr-defined]
    checked = _adapter(valid_policy).check(valid_policy.registry_root, writer)  # type: ignore[attr-defined]
    assert checked.unsafe is True


def test_cached_request_binding_mismatch_is_rejected(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    request_id = str(uuid4())
    result = adapter.install(valid_policy.registry_root, request_id, writer)  # type: ignore[attr-defined]
    adapter._completed[request_id] = (("changed",), result)
    with pytest.raises(ValueError, match="changed authority"):
        adapter.install(valid_policy.registry_root, request_id, writer)  # type: ignore[attr-defined]


def test_mixed_unchanged_and_absent_targets_keep_manifest_order(valid_policy: object) -> None:
    resource = load_packaged_openclaw_bundle().managed_resources[0]
    target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
    target.write_bytes(resource.contents)
    os.chmod(target, 0o640)
    result = _adapter(valid_policy).install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert [row.status for row in result.files].count(OpenClawInstallFileStatus.UNCHANGED) == 1
    assert [row.status for row in result.files].count(OpenClawInstallFileStatus.INSTALLED) == 3


def test_cleanup_failure_cannot_report_install_success(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    monkeypatch.setattr(adapter, "_cleanup", lambda _mutations: False)
    with pytest.raises(ValueError, match="recovery") as captured:
        adapter.install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]


def test_corrupt_complete_write_rolls_back_without_false_success(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def corrupt(descriptor: int, contents: bytes) -> None:
        subject.os.write(descriptor, b"x" * len(contents))

    monkeypatch.setattr(subject, "write_all", corrupt)
    result = _adapter(valid_policy).install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.installed is False
    assert result.rollback_completed is True


def test_temp_mode_drift_is_rolled_back(valid_policy: object, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.fchmod
    monkeypatch.setattr(subject.os, "fchmod", lambda descriptor, _mode: real(descriptor, 0o600))
    result = _adapter(valid_policy).install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.installed is False
    assert result.rollback_completed is True


def test_private_identity_parent_final_rollback_and_cleanup_fault_boundaries(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    resource = load_packaged_openclaw_bundle().managed_resources[0]
    parent_fds = adapter._open_target_parents()
    agent_id, name = subject._destination_parts(resource.destination)  # type: ignore[arg-type]
    parent_fd = parent_fds[agent_id]
    absent = subject._Initial(resource, OpenClawInitialState.ABSENT, None, None)
    mutation = subject._Mutation(absent, parent_fd, name)
    try:
        adapter._verify_initial_identity(mutation)
        os.mkdir(name, dir_fd=parent_fd)
        with pytest.raises(ValueError, match="identity"):
            adapter._verify_initial_identity(mutation)
        os.rmdir(name, dir_fd=parent_fd)

        target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
        target.write_bytes(b"wrong")
        os.chmod(target, 0o640)
        present_status = target.stat()
        present = subject._Initial(
            resource,
            OpenClawInitialState.PRESENT,
            hashlib.sha256(b"wrong").hexdigest(),
            subject.stable_file_identity(present_status),
        )
        present_mutation = subject._Mutation(present, parent_fd, name)
        target.unlink()
        with pytest.raises(ValueError, match="disappeared"):
            adapter._verify_initial_identity(present_mutation)

        target.write_bytes(b"wrong")
        os.chmod(target, 0o600)
        with pytest.raises(ValueError, match="authority"):
            adapter._verify_final(present_mutation)
        os.chmod(target, 0o640)
        with pytest.raises(ValueError, match="digest"):
            adapter._verify_final(present_mutation)

        artifact = ".drawingmachine-openclaw-00000000-0000-4000-8000-000000000000-tmp-0123456789abcdef"
        os.mkdir(artifact, dir_fd=parent_fd)
        present_mutation.temporary_name = artifact
        assert adapter._cleanup([present_mutation]) is False
        os.rmdir(artifact, dir_fd=parent_fd)
    finally:
        with pytest.raises(FileNotFoundError):
            os.stat("definitely-absent", dir_fd=parent_fd)
        for descriptor in parent_fds.values():
            os.close(descriptor)


def test_default_ancestor_authority_and_json_bounds_cover_hostile_facts(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    base = list(valid_policy.managed_root.stat())  # type: ignore[attr-defined]

    def status(*, mode: int = stat.S_IFDIR | 0o755, uid: int = 0, gid: int = 0) -> os.stat_result:
        values = base[:10]
        values[0], values[4], values[5] = mode, uid, gid
        return os.stat_result(values)

    assert adapter._default_ancestor_authority(Path("/"), status()) is True
    assert adapter._default_ancestor_authority(Path("/"), status(mode=stat.S_IFLNK | 0o777)) is False
    assert adapter._default_ancestor_authority(Path("/"), status(uid=12345)) is False
    assert adapter._default_ancestor_authority(Path("/"), status(mode=stat.S_IFDIR | 0o757)) is False
    assert (
        adapter._default_ancestor_authority(
            Path("/"),
            status(uid=valid_policy.automation_principal.uid),  # type: ignore[attr-defined]
        )
        is False
    )

    for payload in (
        b"\xff",
        b"[1]",
        b"NaN",
        json.dumps("x" * 65_537).encode(),
        json.dumps([[[[[[[[[[[[[[0]]]]]]]]]]]]]]).encode(),
    ):
        with pytest.raises(ValueError):
            subject._decode_registry(payload)
    subject._validate_json(1.0, [2], 0)
    with pytest.raises(ValueError, match="unsupported"):
        subject._validate_json(object(), [2], 0)
    with pytest.raises(ValueError, match="key"):
        subject._validate_json({1: "x"}, [3], 0)
    with pytest.raises(ValueError, match="bounds"):
        subject._validate_json(None, [0], 0)
    with pytest.raises(ValueError, match="sentinel"):
        subject._invalid_registry("sentinel")


@pytest.mark.parametrize(
    ("fault", "match"), [("link", "transaction file"), ("owner", "transaction file"), ("identity", "identity")]
)
def test_created_temp_identity_faults_fail_before_publication(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    match: str,
) -> None:
    adapter = _adapter(valid_policy)
    resource = load_packaged_openclaw_bundle().managed_resources[0]
    parent_fds = adapter._open_target_parents()
    agent_id, name = subject._destination_parts(resource.destination)  # type: ignore[arg-type]
    mutation = subject._Mutation(
        subject._Initial(resource, OpenClawInitialState.ABSENT, None, None),
        parent_fds[agent_id],
        name,
    )
    real = subject.os.fstat

    def hostile(descriptor: int) -> os.stat_result:
        current = real(descriptor)
        if not stat.S_ISREG(current.st_mode):
            return current
        values = list(current)[:10]
        if fault == "link":
            values[3] = 2
        elif fault == "owner":
            values[5] = current.st_gid + 1
        else:
            values[1] = current.st_ino + 1
        return os.stat_result(values)

    monkeypatch.setattr(subject.os, "fstat", hostile)
    try:
        with pytest.raises(ValueError, match=match):
            adapter._apply_one(mutation, str(uuid4()))
    finally:
        monkeypatch.setattr(subject.os, "fstat", real)
        if mutation.temporary_name is not None:
            os.unlink(mutation.temporary_name, dir_fd=mutation.parent_fd)
        for descriptor in parent_fds.values():
            os.close(descriptor)


def test_parent_swap_and_rollback_identity_faults_are_recovery_required(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    resource = load_packaged_openclaw_bundle().managed_resources[0]
    parent_fds = adapter._open_target_parents()
    agent_id, name = subject._destination_parts(resource.destination)  # type: ignore[arg-type]
    parent_fd = parent_fds[agent_id]
    initial = subject._Initial(resource, OpenClawInitialState.ABSENT, None, None)
    mutation = subject._Mutation(initial, parent_fd, name)
    agent_path = valid_policy.managed_root / "agents" / agent_id  # type: ignore[attr-defined]
    moved = agent_path.with_name(f"{agent_id}.moved")
    agent_path.rename(moved)
    agent_path.mkdir()
    os.chmod(agent_path, 0o2750)
    try:
        with pytest.raises(ValueError, match="parent"):
            adapter._verify_parent_path(mutation)
    finally:
        agent_path.rmdir()
        moved.rename(agent_path)

    os.mkdir(name, dir_fd=parent_fd)
    assert adapter._rollback([mutation]) is False
    os.rmdir(name, dir_fd=parent_fd)

    backup = ".drawingmachine-openclaw-00000000-0000-4000-8000-000000000000-bak-0123456789abcdef"
    backup_path = moved / backup if moved.exists() else agent_path / backup
    backup_path.write_bytes(b"prior")
    os.chmod(backup_path, 0o640)
    present = subject._Initial(
        resource, OpenClawInitialState.PRESENT, hashlib.sha256(b"other").hexdigest(), (0, 0, 0, 0, 0, 0)
    )
    present_mutation = subject._Mutation(present, parent_fd, name, backup_name=backup)
    assert adapter._rollback([present_mutation]) is False
    assert os.stat(backup, dir_fd=parent_fd)
    os.unlink(backup, dir_fd=parent_fd)
    for descriptor in parent_fds.values():
        os.close(descriptor)


def test_registry_matcher_ignores_unrelated_entries_but_rejects_malformed_tools(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    path = _registry_path(valid_policy)
    registry = json.loads(path.read_text(encoding="utf-8"))
    registry["agents"]["list"][:0] = [1, {"id": "unrelated"}]
    assert adapter._registry_matches(registry) is True
    registry["agents"]["list"][2]["tools"] = None
    assert adapter._registry_matches(registry) is False
    registry["agents"]["list"][2]["tools"] = {"alsoAllow": [1]}
    assert adapter._registry_matches(registry) is False


def test_open_parent_and_root_failures_close_descriptors(valid_policy: object, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    second_agent = valid_policy.managed_root / "agents/cnc-drawable-router"  # type: ignore[attr-defined]
    os.chmod(second_agent, 0o750)
    assert _adapter(valid_policy).check(valid_policy.registry_root, writer).unsafe is True  # type: ignore[attr-defined]
    os.chmod(second_agent, 0o2750)

    registry = valid_policy.registry_root  # type: ignore[attr-defined]
    os.chmod(registry, 0o777)
    assert _adapter(valid_policy).check(registry, writer).unsafe is True
    os.chmod(registry, 0o750)

    moved = valid_policy.managed_root.with_name("managed-real")  # type: ignore[attr-defined]
    valid_policy.managed_root.rename(moved)  # type: ignore[attr-defined]
    valid_policy.managed_root.symlink_to(moved, target_is_directory=True)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="root"):
        _adapter(valid_policy)._open_root(valid_policy.managed_root, managed=True)  # type: ignore[attr-defined]
    valid_policy.managed_root.unlink()  # type: ignore[attr-defined]
    moved.rename(valid_policy.managed_root)  # type: ignore[attr-defined]

    real_identity = subject.identity
    calls = 0

    def changed_identity(value: os.stat_result) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        actual = real_identity(value)
        return actual if calls % 2 else (actual[0], actual[1] + 1)

    monkeypatch.setattr(subject, "identity", changed_identity)
    with pytest.raises(ValueError, match="changed"):
        _adapter(valid_policy)._open_root(valid_policy.managed_root, managed=True)  # type: ignore[attr-defined]


def test_private_invalid_request_destination_and_json_forms() -> None:
    with pytest.raises(ValueError, match="UUIDv4"):
        subject._request_id(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="layout"):
        subject._destination_parts("escape")
    with pytest.raises(ValueError, match="bounded JSON"):
        subject._decode_registry(b"{")
    with pytest.raises(ValueError, match="finite"):
        subject._validate_json(float("nan"), [2], 0)


def test_parent_authority_initial_unsafe_and_missing_agents_fail_closed(valid_policy: object) -> None:
    parent = valid_policy.registry_root.parent  # type: ignore[attr-defined]
    adapter = OpenClawFilesystemAdapter(  # type: ignore[arg-type]
        valid_policy,
        ancestor_authority=lambda path, _status: path != parent,
    )
    with pytest.raises(ValueError, match="parent"):
        adapter._validate_ancestor_chains()

    target = valid_policy.managed_root / "agents/cnc-drawable-router/AGENTS.md"  # type: ignore[attr-defined]
    target.write_text("unsafe", encoding="utf-8")
    os.chmod(target, 0o600)
    with pytest.raises(ValueError, match="target"):
        _adapter(valid_policy)._initial_targets()
    target.unlink()

    agents = valid_policy.managed_root / "agents"  # type: ignore[attr-defined]
    moved = valid_policy.managed_root / "agents-moved"  # type: ignore[attr-defined]
    agents.rename(moved)
    try:
        assert (
            _adapter(valid_policy)
            .check(  # type: ignore[attr-defined]
                valid_policy.registry_root,
                ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
            )
            .unsafe
            is True
        )
        with pytest.raises(ValueError, match="filesystem"):
            _adapter(valid_policy).install(  # type: ignore[attr-defined]
                valid_policy.registry_root,
                str(uuid4()),
                ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
            )
        with pytest.raises(RuntimeError, match="directory"):
            _adapter(valid_policy)._open_target_parents()
    finally:
        moved.rename(agents)


def test_i1_residue_blocks_unchanged_fast_path_and_cached_replay(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    request_id = str(uuid4())
    assert adapter.install(valid_policy.registry_root, request_id, writer).installed is True  # type: ignore[attr-defined]
    residue = (
        valid_policy.managed_root  # type: ignore[attr-defined]
        / "agents/cnc-drawable-router"
        / ".drawingmachine-openclaw-00000000-0000-4000-8000-000000000000-bak-0123456789abcdef"
    )
    residue.write_bytes(b"preserved recovery evidence")
    os.chmod(residue, 0o640)

    for replay_id in (str(uuid4()), request_id):
        with pytest.raises(ValueError, match="recovery") as captured:
            adapter.install(valid_policy.registry_root, replay_id, writer)  # type: ignore[attr-defined]
        assert captured.value.recovery_required is True  # type: ignore[attr-defined]
        assert residue.read_bytes() == b"preserved recovery evidence"


def test_i2_failure_marks_only_started_rows_and_preserves_manifest_order(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    real = adapter._apply_one
    calls = 0

    def fail_before_second(mutation: object, request_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected before second mutation")
        real(mutation, request_id)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter, "_apply_one", fail_before_second)
    result = adapter.install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    expected = [resource.destination for resource in load_packaged_openclaw_bundle().managed_resources]
    assert [row.destination for row in result.files] == expected
    assert result.mutation_started is True
    assert result.files[0].status is OpenClawInstallFileStatus.RESTORED_ABSENT
    assert all(row.status is OpenClawInstallFileStatus.NOT_ATTEMPTED for row in result.files[1:])


def test_i3_foreign_published_target_is_preserved_as_recovery_evidence(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = valid_policy.managed_root / "agents/cnc-drawable-translator/AGENTS.md"  # type: ignore[attr-defined]
    real = subject.fsync_directory
    injected = False

    def replace_published_target(descriptor: int) -> None:
        nonlocal injected
        if not injected and target.exists():
            injected = True
            target.unlink()
            target.write_bytes(b"foreign replacement")
            os.chmod(target, 0o640)
            raise OSError("injected after publication")
        real(descriptor)

    monkeypatch.setattr(subject, "fsync_directory", replace_published_target)
    with pytest.raises(ValueError, match="recovery") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert target.read_bytes() == b"foreign replacement"


def test_i3_foreign_temp_is_preserved_before_publication(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = subject.rename_noreplace
    replaced: Path | None = None

    def replace_source_then_fail(source_fd: int, source: str, target_fd: int, target: str) -> None:
        nonlocal replaced
        if "-tmp-" in source:
            parent = valid_policy.managed_root / "agents/cnc-drawable-translator"  # type: ignore[attr-defined]
            replaced = parent / source
            replaced.unlink()
            replaced.write_bytes(b"foreign temp")
            os.chmod(replaced, 0o640)
            raise OSError("injected temp replacement")
        real(source_fd, source, target_fd, target)

    monkeypatch.setattr(subject, "rename_noreplace", replace_source_then_fail)
    with pytest.raises(ValueError, match="recovery") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert replaced is not None and replaced.read_bytes() == b"foreign temp"


def test_i3_foreign_backup_is_preserved_before_restore_or_cleanup(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    parent = valid_policy.managed_root / "agents/cnc-drawable-translator"  # type: ignore[attr-defined]
    real = subject.fsync_directory
    replaced: Path | None = None

    def replace_backup_then_fail(descriptor: int) -> None:
        nonlocal replaced
        backups = list(parent.glob(".drawingmachine-openclaw-*-bak-*"))
        if replaced is None and backups:
            replaced = backups[0]
            replaced.unlink()
            replaced.write_bytes(b"foreign backup")
            os.chmod(replaced, 0o640)
            raise OSError("injected backup replacement")
        real(descriptor)

    monkeypatch.setattr(subject, "fsync_directory", replace_backup_then_fail)
    with pytest.raises(ValueError, match="recovery") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert replaced is not None and replaced.read_bytes() == b"foreign backup"


def test_i4_physical_same_root_alias_is_rejected_before_target_write(valid_policy: object) -> None:
    managed_root = valid_policy.managed_root  # type: ignore[attr-defined]
    source_registry = _registry_path(valid_policy)
    (managed_root / "openclaw.json").write_bytes(source_registry.read_bytes())
    os.chmod(managed_root / "openclaw.json", 0o440)
    aliased = replace(valid_policy, registry_root=managed_root)

    with pytest.raises(ValueError, match=r"distinct|nested|root"):
        _adapter(aliased).install(
            managed_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert all(
        not (managed_root / resource.destination).exists()
        for resource in load_packaged_openclaw_bundle().managed_resources
    )


def test_i5_registry_drift_immediately_before_mutation_is_zero_write(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    real = adapter._initial_targets

    def drift_after_initial_read() -> object:
        initial = real()
        _replace_registry(valid_policy)
        return initial

    monkeypatch.setattr(adapter, "_initial_targets", drift_after_initial_read)
    with pytest.raises(ValueError, match="registry"):
        adapter.install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert all(
        not (valid_policy.managed_root / resource.destination).exists()  # type: ignore[attr-defined]
        for resource in load_packaged_openclaw_bundle().managed_resources
    )


def test_i5_registry_drift_after_publication_rolls_back_without_compliance_claim(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    real = adapter._apply_one
    drifted = False

    def drift_after_first(mutation: object, request_id: str) -> None:
        nonlocal drifted
        real(mutation, request_id)  # type: ignore[arg-type]
        if not drifted:
            drifted = True
            _replace_registry(valid_policy)

    monkeypatch.setattr(adapter, "_apply_one", drift_after_first)
    result = adapter.install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.installed is False
    assert result.registry_compliant is False
    assert result.rollback_completed is True
    assert all(
        not (valid_policy.managed_root / resource.destination).exists()  # type: ignore[attr-defined]
        for resource in load_packaged_openclaw_bundle().managed_resources
    )


def test_identity_freeze_rejects_backup_and_published_rename_drift(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def drifting_freeze(real: object, present: bool) -> object:
        def drift(parent_fd: int, name: str, expected: bytes | None = None) -> object:
            frozen = real(parent_fd, name, expected)  # type: ignore[operator]
            selected = "-bak-" in name if present else name == "AGENTS.md"
            if selected:
                changed = (frozen.identity[0] + 1, *frozen.identity[1:])  # type: ignore[attr-defined]
                return subject._FrozenFile(changed, frozen.sha256)  # type: ignore[attr-defined]
            return frozen

        return drift

    for present in (True, False):
        if present:
            _seed_targets(valid_policy, b"trusted prior")
        adapter = _adapter(valid_policy)
        real = adapter._freeze_named
        monkeypatch.setattr(adapter, "_freeze_named", drifting_freeze(real, present))
        with pytest.raises(ValueError, match="recovery"):
            adapter.install(  # type: ignore[attr-defined]
                valid_policy.registry_root,
                str(uuid4()),
                ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
            )
        monkeypatch.undo()
        for resource in load_packaged_openclaw_bundle().managed_resources:
            target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
            if target.exists():
                target.unlink()
        for artifact in valid_policy.managed_root.glob("agents/*/.drawingmachine-openclaw-*"):  # type: ignore[attr-defined]
            artifact.unlink()


def test_descriptor_freeze_enforces_bound_and_stable_full_identity(
    tmp_path: Path,
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    path = tmp_path / "descriptor"
    path.write_bytes(b"x" * 65_537)
    descriptor = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(ValueError, match="bound"):
            adapter._freeze_descriptor(descriptor)
    finally:
        os.close(descriptor)

    path.write_bytes(b"small")
    descriptor = os.open(path, os.O_RDWR)
    real = subject.os.fstat
    calls = 0

    def drift_after_read(candidate: int) -> os.stat_result:
        nonlocal calls
        current = real(candidate)
        if candidate == descriptor:
            calls += 1
            if calls == 2:
                values = list(current)[:10]
                values[8] += 1
                return os.stat_result(values)
        return current

    monkeypatch.setattr(subject.os, "fstat", drift_after_read)
    try:
        with pytest.raises(ValueError, match="changed"):
            adapter._freeze_descriptor(descriptor)
    finally:
        os.close(descriptor)


def test_present_rollback_uses_frozen_backup_and_reports_only_touched_row(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    adapter = _adapter(valid_policy)
    real = adapter._apply_one
    calls = 0

    def fail_before_second(mutation: object, request_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("stop after first present publication")
        real(mutation, request_id)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter, "_apply_one", fail_before_second)
    result = adapter.install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.files[0].status is OpenClawInstallFileStatus.RESTORED_PRESENT
    assert all(row.status is OpenClawInstallFileStatus.NOT_ATTEMPTED for row in result.files[1:])
    assert all((valid_policy.managed_root / row.destination).read_bytes() == b"trusted prior" for row in result.files)  # type: ignore[attr-defined]


def test_root_ancestry_registry_exception_and_cached_drift_fail_closed(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    nested = valid_policy.managed_root / "agents/cnc-drawable-translator"  # type: ignore[attr-defined]
    descriptor = os.open(nested, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert adapter._descriptor_has_ancestor(descriptor, subject.identity(valid_policy.managed_root.stat())) is True  # type: ignore[attr-defined]
    finally:
        os.close(descriptor)

    monkeypatch.setattr(adapter, "_inspect_registry", lambda: (_ for _ in ()).throw(OSError("registry race")))
    with pytest.raises(ValueError, match="revalidated"):
        adapter._assert_registry_unchanged(subject._RegistrySnapshot((0, 0, 0, 0, 0, 0), "a" * 64))
    monkeypatch.undo()

    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())
    request_id = str(uuid4())
    assert adapter.install(valid_policy.registry_root, request_id, writer).installed is True  # type: ignore[attr-defined]
    _replace_registry(valid_policy)
    with pytest.raises(ValueError, match="registry"):
        adapter.install(valid_policy.registry_root, request_id, writer)  # type: ignore[attr-defined]


def test_preflight_and_pre_mutation_failures_are_sanitized_without_false_result(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    monkeypatch.setattr(adapter, "_assert_registry_unchanged", lambda _snapshot: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ValueError, match="filesystem"):
        adapter.install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    monkeypatch.undo()

    adapter = _adapter(valid_policy)
    monkeypatch.setattr(adapter, "_apply_one", lambda _mutation, _request: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ValueError, match="before mutation"):
        adapter.install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )


def test_final_identity_and_failed_row_private_guards(valid_policy: object) -> None:
    adapter = _adapter(valid_policy)
    resource = load_packaged_openclaw_bundle().managed_resources[0]
    target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
    target.write_bytes(resource.contents)
    os.chmod(target, 0o640)
    parents = adapter._open_target_parents()
    agent_id, name = subject._destination_parts(resource.destination)  # type: ignore[arg-type]
    mutation = subject._Mutation(
        subject._Initial(resource, OpenClawInitialState.ABSENT, None, None),
        parents[agent_id],
        name,
    )
    try:
        with pytest.raises(ValueError, match="identity"):
            adapter._verify_final(mutation)
        mutation.stage = subject._MutationStage.TEMP_READY
        row = adapter._failed_mutation_row(mutation)
        assert row.status is OpenClawInstallFileStatus.FAILED
    finally:
        for descriptor in parents.values():
            os.close(descriptor)


def test_restore_post_rename_identity_drift_and_second_root_open_failure(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(valid_policy)
    resource = load_packaged_openclaw_bundle().managed_resources[0]
    parents = adapter._open_target_parents()
    agent_id, name = subject._destination_parts(resource.destination)  # type: ignore[arg-type]
    backup_name = ".drawingmachine-openclaw-00000000-0000-4000-8000-000000000000-bak-0123456789abcdef"
    backup_path = valid_policy.managed_root / "agents" / agent_id / backup_name  # type: ignore[attr-defined]
    backup_path.write_bytes(b"trusted backup")
    os.chmod(backup_path, 0o640)
    backup = adapter._freeze_named(parents[agent_id], backup_name)
    mutation = subject._Mutation(
        subject._Initial(resource, OpenClawInitialState.PRESENT, backup.sha256, backup.identity),
        parents[agent_id],
        name,
        backup_name=backup_name,
        backup=backup,
        stage=subject._MutationStage.BACKUP_MOVED,
    )
    real_freeze = adapter._freeze_named

    def drift_restored(parent_fd: int, candidate: str, expected: bytes | None = None) -> object:
        frozen = real_freeze(parent_fd, candidate, expected)
        if candidate == name:
            return subject._FrozenFile((frozen.identity[0] + 1, *frozen.identity[1:]), frozen.sha256)
        return frozen

    monkeypatch.setattr(adapter, "_freeze_named", drift_restored)
    assert adapter._rollback([mutation]) is False
    for descriptor in parents.values():
        os.close(descriptor)
    target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
    target.unlink()
    monkeypatch.undo()

    adapter = _adapter(valid_policy)
    real_open = adapter._open_root
    registry_fd: int | None = None
    calls = 0

    def fail_second(path: Path, *, managed: bool) -> int:
        nonlocal calls, registry_fd
        calls += 1
        if calls == 2:
            raise OSError("managed root open failed")
        registry_fd = real_open(path, managed=managed)
        return registry_fd

    monkeypatch.setattr(adapter, "_open_root", fail_second)
    with pytest.raises(OSError, match="managed root"):
        adapter._validate_root_separation()
    assert registry_fd is not None
    with pytest.raises(OSError):
        os.fstat(registry_fd)


def test_i2b_temp_create_oserror_is_zero_mutation_not_recovery(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = subject.os.open

    def fail_temp(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if isinstance(path, str) and "-tmp-" in path:
            raise OSError("deterministic create failure")
        return real(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(subject.os, "open", fail_temp)
    with pytest.raises(ValueError, match="before mutation") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is False  # type: ignore[attr-defined]
    assert not list(valid_policy.managed_root.glob("agents/*/.drawingmachine-openclaw-*"))  # type: ignore[attr-defined]


def test_n2_temp_fstat_failure_after_create_retains_recovery_evidence(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = subject.os.open
    real_fstat = subject.os.fstat
    temporary_fd: int | None = None

    def remember_temp(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal temporary_fd
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and "-tmp-" in path:
            temporary_fd = descriptor
        return descriptor

    def fail_temp_fstat(descriptor: int) -> os.stat_result:
        if descriptor == temporary_fd:
            raise OSError("injected temporary fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(subject.os, "open", remember_temp)
    monkeypatch.setattr(subject.os, "fstat", fail_temp_fstat)
    with pytest.raises(ValueError, match="recovery") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    artifacts = list(valid_policy.managed_root.glob("agents/*/.drawingmachine-openclaw-*-tmp-*"))  # type: ignore[attr-defined]
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert len(artifacts) == 1


@pytest.mark.parametrize("fault", ["stat_at", "owner"])
def test_n2_created_temp_validation_failure_is_identity_safely_cleaned(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    real_open = subject.os.open
    real_fstat = subject.os.fstat
    real_stat_at = subject.stat_at
    temporary_fd: int | None = None

    def remember_temp(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal temporary_fd
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and "-tmp-" in path:
            temporary_fd = descriptor
        return descriptor

    def inspect_temp(descriptor: int) -> object:
        status = real_fstat(descriptor)
        if fault != "owner" or descriptor != temporary_fd:
            return status

        class UnsafeOwner:
            st_mode = status.st_mode
            st_dev = status.st_dev
            st_ino = status.st_ino
            st_nlink = status.st_nlink
            st_uid = status.st_uid + 1
            st_gid = status.st_gid
            st_size = status.st_size
            st_mtime_ns = status.st_mtime_ns
            st_ctime_ns = status.st_ctime_ns

        return UnsafeOwner()

    def inspect_path(parent_fd: int, name: str) -> os.stat_result:
        if fault == "stat_at" and "-tmp-" in name:
            raise OSError("injected temporary stat_at failure")
        return real_stat_at(parent_fd, name)

    monkeypatch.setattr(subject.os, "open", remember_temp)
    monkeypatch.setattr(subject.os, "fstat", inspect_temp)
    monkeypatch.setattr(subject, "stat_at", inspect_path)
    result = _adapter(valid_policy).install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.installed is False
    assert result.mutation_started is True
    assert result.rollback_completed is True
    assert not list(valid_policy.managed_root.glob("agents/*/.drawingmachine-openclaw-*"))  # type: ignore[attr-defined]


def test_n2_foreign_temp_replacement_after_create_is_preserved_for_recovery(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_stat_at = subject.stat_at
    replaced: Path | None = None

    def replace_temp(parent_fd: int, name: str) -> os.stat_result:
        nonlocal replaced
        if replaced is None and "-tmp-" in name:
            os.unlink(name, dir_fd=parent_fd)
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o640,
                dir_fd=parent_fd,
            )
            os.write(descriptor, b"foreign temporary evidence")
            os.close(descriptor)
            agent_id = "cnc-drawable-translator"
            replaced = valid_policy.managed_root / "agents" / agent_id / name  # type: ignore[attr-defined,operator]
        return real_stat_at(parent_fd, name)

    monkeypatch.setattr(subject, "stat_at", replace_temp)
    with pytest.raises(ValueError, match="recovery") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert replaced is not None and replaced.read_bytes() == b"foreign temporary evidence"


def test_i3_incomplete_rollback_retains_trusted_backup_and_foreign_target(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    target = valid_policy.managed_root / "agents/cnc-drawable-translator/AGENTS.md"  # type: ignore[attr-defined]
    real = subject.fsync_directory
    injected = False

    def replace_after_publish(descriptor: int) -> None:
        nonlocal injected
        backups = list(target.parent.glob(".drawingmachine-openclaw-*-bak-*"))
        if not injected and target.exists() and backups:
            injected = True
            target.unlink()
            target.write_bytes(b"foreign published target")
            os.chmod(target, 0o640)
            raise OSError("foreign target after publish")
        real(descriptor)

    monkeypatch.setattr(subject, "fsync_directory", replace_after_publish)
    with pytest.raises(ValueError, match="recovery") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    backups = list(target.parent.glob(".drawingmachine-openclaw-*-bak-*"))
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert target.read_bytes() == b"foreign published target"
    assert len(backups) == 1 and backups[0].read_bytes() == b"trusted prior"


def test_i5_cleanup_window_registry_drift_cannot_succeed_or_cache(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    adapter = _adapter(valid_policy)
    real = adapter._cleanup

    def drift_after_cleanup(mutations: object) -> bool:
        cleaned = real(mutations)  # type: ignore[arg-type]
        _replace_registry(valid_policy)
        return cleaned

    monkeypatch.setattr(adapter, "_cleanup", drift_after_cleanup)
    request_id = str(uuid4())
    with pytest.raises(ValueError, match="recovery") as captured:
        adapter.install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            request_id,
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert request_id not in adapter._completed
    for resource in load_packaged_openclaw_bundle().managed_resources:
        target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
        assert target.read_bytes() == resource.contents


def test_n1_unchanged_path_target_drift_is_preserved_and_not_cached(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_packaged_openclaw_bundle()
    for resource in bundle.managed_resources:
        target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
        target.write_bytes(resource.contents)
        os.chmod(target, 0o640)
    adapter = _adapter(valid_policy)
    real = adapter._initial_targets
    foreign = valid_policy.managed_root / bundle.managed_resources[0].destination  # type: ignore[attr-defined,operator]

    def drift_after_initial() -> object:
        initial = real()
        foreign.unlink()
        foreign.write_bytes(b"foreign unchanged replacement")
        os.chmod(foreign, 0o640)
        return initial

    monkeypatch.setattr(adapter, "_initial_targets", drift_after_initial)
    request_id = str(uuid4())
    with pytest.raises(ValueError, match=r"target|mutation") as captured:
        adapter.install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            request_id,
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is False  # type: ignore[attr-defined]
    assert foreign.read_bytes() == b"foreign unchanged replacement"
    assert request_id not in adapter._completed


def test_n1_post_cleanup_earlier_target_drift_is_recovery_not_success(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    adapter = _adapter(valid_policy)
    real = adapter._cleanup
    first = load_packaged_openclaw_bundle().managed_resources[0]
    foreign = valid_policy.managed_root / first.destination  # type: ignore[attr-defined,operator]

    def drift_after_cleanup(mutations: object) -> bool:
        cleaned = real(mutations)  # type: ignore[arg-type]
        foreign.unlink()
        foreign.write_bytes(b"foreign post-cleanup target")
        os.chmod(foreign, 0o640)
        return cleaned

    monkeypatch.setattr(adapter, "_cleanup", drift_after_cleanup)
    request_id = str(uuid4())
    with pytest.raises(ValueError, match="recovery") as captured:
        adapter.install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            request_id,
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert foreign.read_bytes() == b"foreign post-cleanup target"
    assert request_id not in adapter._completed


def test_n1_final_check_detects_managed_root_swap_through_retained_parent_fds(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    adapter = _adapter(valid_policy)
    real = adapter._cleanup
    managed = valid_policy.managed_root  # type: ignore[attr-defined]
    moved = managed.with_name("managed-retained")
    swapped = False

    def swap_after_cleanup(mutations: object) -> bool:
        nonlocal swapped
        cleaned = real(mutations)  # type: ignore[arg-type]
        if not swapped:
            swapped = True
            managed.rename(moved)
            managed.mkdir(mode=0o2750)
            (managed / "agents").mkdir(mode=0o2750)
            for agent_id in ("cnc-drawable-translator", "cnc-drawable-router"):
                (managed / "agents" / agent_id).mkdir(mode=0o2750)
            for directory in (managed, managed / "agents", *(managed / "agents").iterdir()):
                os.chmod(directory, 0o2750)
        return cleaned

    monkeypatch.setattr(adapter, "_cleanup", swap_after_cleanup)
    request_id = str(uuid4())
    try:
        with pytest.raises(ValueError, match="recovery") as captured:
            adapter.install(  # type: ignore[attr-defined]
                valid_policy.registry_root,
                request_id,
                ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
            )
        assert captured.value.recovery_required is True  # type: ignore[attr-defined]
        assert request_id not in adapter._completed
        assert not list(managed.glob("agents/*/*"))
        assert len(list(moved.glob("agents/*/AGENTS.md"))) == 2
        assert len(list(moved.glob("agents/*/TOOLS.md"))) == 2
    finally:
        for directory in (managed / "agents").iterdir():
            directory.rmdir()
        (managed / "agents").rmdir()
        managed.rmdir()
        moved.rename(managed)


def test_n3_final_validation_precedes_irreversible_backup_cleanup(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    adapter = _adapter(valid_policy)
    real = adapter._verify_target_facts
    observed_backup_counts: list[int] = []

    def observe_boundary(
        parent_fds: object,
        facts: object,
        *,
        require_packaged: bool,
    ) -> None:
        observed_backup_counts.append(
            len(list(valid_policy.managed_root.glob("agents/*/.drawingmachine-openclaw-*-bak-*")))  # type: ignore[attr-defined]
        )
        real(parent_fds, facts, require_packaged=require_packaged)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter, "_verify_target_facts", observe_boundary)
    result = adapter.install(  # type: ignore[attr-defined]
        valid_policy.registry_root,
        str(uuid4()),
        ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
    )
    assert result.installed is True
    assert observed_backup_counts == [4, 0]


@pytest.mark.parametrize("backup_delete_index", [1, 2, 3, 4])
def test_n3_cleanup_failure_at_each_backup_index_never_deletes_published_targets(
    valid_policy: object,
    monkeypatch: pytest.MonkeyPatch,
    backup_delete_index: int,
) -> None:
    _seed_targets(valid_policy, b"trusted prior")
    real_unlink = subject.os.unlink
    seen_backups = 0

    def fail_selected_backup(
        path: object,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal seen_backups
        if isinstance(path, str) and "-bak-" in path:
            seen_backups += 1
            if seen_backups == backup_delete_index:
                raise OSError(f"injected backup cleanup failure {backup_delete_index}")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(subject.os, "unlink", fail_selected_backup)
    with pytest.raises(ValueError, match="recovery") as captured:
        _adapter(valid_policy).install(  # type: ignore[attr-defined]
            valid_policy.registry_root,
            str(uuid4()),
            ServiceWriterSnapshotV1(os.getuid(), os.getgid()),
        )
    assert captured.value.recovery_required is True  # type: ignore[attr-defined]
    assert seen_backups == backup_delete_index
    for resource in load_packaged_openclaw_bundle().managed_resources:
        target = valid_policy.managed_root / resource.destination  # type: ignore[attr-defined,operator]
        assert target.read_bytes() == resource.contents


def test_final_target_fact_private_fail_closed_matrix(valid_policy: object) -> None:
    _seed_targets(valid_policy, b"prior target")
    adapter = _adapter(valid_policy)
    facts = adapter._initial_targets()
    parents = adapter._open_target_parents()
    first = facts[0]
    agent_id, name = subject._destination_parts(first.resource.destination)  # type: ignore[arg-type]
    target = valid_policy.managed_root / first.resource.destination  # type: ignore[attr-defined,operator]
    try:
        with pytest.raises(ValueError, match="ordered"):
            adapter._verify_target_facts(parents, tuple(reversed(facts)), require_packaged=False)

        target.unlink()
        with pytest.raises(ValueError, match="disappeared"):
            adapter._verify_target_facts(parents, facts, require_packaged=False)
        target.write_bytes(b"prior target")
        os.chmod(target, 0o640)
        refreshed = adapter._initial_targets()
        absent = replace(refreshed[0], state=OpenClawInitialState.ABSENT, sha256=None, identity=None)
        with pytest.raises(ValueError, match="appeared"):
            adapter._verify_target_facts(parents, (absent, *refreshed[1:]), require_packaged=False)

        os.chmod(target, 0o600)
        with pytest.raises(ValueError, match="authority"):
            adapter._verify_target_facts(parents, refreshed, require_packaged=False)
        os.chmod(target, 0o640)
        refreshed = adapter._initial_targets()
        with pytest.raises(ValueError, match="packaged"):
            adapter._verify_target_facts(parents, refreshed, require_packaged=True)

        mutation = subject._Mutation(refreshed[0], parents[agent_id], name)
        with pytest.raises(ValueError, match="unavailable"):
            adapter._final_target_facts([mutation])
    finally:
        for descriptor in parents.values():
            os.close(descriptor)


def _admin_owned_policy(tmp_path: Path, *, registry_owner_uid: int, registry_owner_gid: int) -> object:
    """A policy identical to `valid_policy` except registry_root is nominally admin-owned.

    The registry_root and managed_root are real directories owned by the test process
    (unprivileged tests cannot chown), but `registry_owner` is set to a distinct
    synthetic admin identity. Tests exercising this fixture fake the *observed* uid/gid
    of the registry root and its registry file (via `_fake_registry_root_owner`) so the
    production authority checks see genuine admin ownership without weakening any check.
    """
    registry_root = tmp_path / "registry"
    managed_root = tmp_path / "managed"
    registry_root.mkdir(mode=0o750)
    managed_root.mkdir(mode=0o2750)
    (managed_root / "agents").mkdir()
    for agent_id in ("cnc-drawable-translator", "cnc-drawable-router"):
        (managed_root / "agents" / agent_id).mkdir()
    for directory in (managed_root, managed_root / "agents", *(managed_root / "agents").iterdir()):
        os.chmod(directory, 0o2750)
    bundle = load_packaged_openclaw_bundle()
    encoded = f'''schema_version = 1
registry_root = {json.dumps(str(registry_root))}
managed_root = {json.dumps(str(managed_root))}
managed_dir_mode = {0o2750}
target_mode = {0o640}
manifest_sha256 = "{bundle.manifest_sha256}"
registry_policy_sha256 = "{bundle.registry_policy_sha256}"

[operator_principal]
uid = {os.getuid() + 10001}
gid = {os.getgid() + 10001}

[automation_principal]
uid = {os.getuid() + 10002}
gid = {os.getgid() + 10002}

[automation_read_group]
name = "drawingmachine-openclaw-read"
gid = {os.getgid()}

[managed_owner]
uid = {os.getuid()}
gid = {os.getgid()}

[registry_owner]
uid = {registry_owner_uid}
gid = {registry_owner_gid}
'''.encode()
    return parse_openclaw_deployment_policy(encoded, source_sha256=hashlib.sha256(encoded).hexdigest())


def _fake_registry_root_owner(
    monkeypatch: pytest.MonkeyPatch,
    registry_root: Path,
    uid: int,
    gid: int,
) -> None:
    """Make the registry root directory and its openclaw.json appear owned by (uid, gid).

    Only the observed uid/gid are faked (via the exact call sites the production
    authority checks use: `os.fstat` on the opened registry root descriptor, and
    `stat_at` for the registry file). Real modes, link counts, and identities are left
    untouched, and every other real filesystem object (managed root, managed targets,
    unrelated descriptors) passes through unmodified.
    """
    real_fstat = subject.os.fstat
    real_stat_at = subject.stat_at

    def fake_fstat(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        if stat.S_ISDIR(result.st_mode):
            try:
                resolved = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                return result
            if resolved == registry_root:
                fields = list(result)[:10]
                fields[4], fields[5] = uid, gid
                return os.stat_result(fields)
        return result

    def fake_stat_at(parent_fd: int, name: str) -> os.stat_result:
        result = real_stat_at(parent_fd, name)
        if name == "openclaw.json":
            fields = list(result)[:10]
            fields[4], fields[5] = uid, gid
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(subject.os, "fstat", fake_fstat)
    monkeypatch.setattr(subject, "stat_at", fake_stat_at)


def test_admin_owned_registry_root_is_not_blanket_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_uid, admin_gid = os.getuid() + 7001, os.getgid() + 7001
    policy = _admin_owned_policy(tmp_path, registry_owner_uid=admin_uid, registry_owner_gid=admin_gid)
    _fake_registry_root_owner(monkeypatch, policy.registry_root, admin_uid, admin_gid)  # type: ignore[attr-defined]
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())

    checked = _adapter(policy).check(policy.registry_root, writer)  # type: ignore[attr-defined]

    assert checked.unsafe is False
    assert checked.registry.status is OpenClawRegistryStatus.MISSING
    assert {row.status for row in checked.files} == {OpenClawCheckStatus.MISSING}


def test_safe_registry_file_admits_admin_owned_openclaw_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_uid, admin_gid = os.getuid() + 7002, os.getgid() + 7002
    policy = _admin_owned_policy(tmp_path, registry_owner_uid=admin_uid, registry_owner_gid=admin_gid)
    bundle = load_packaged_openclaw_bundle()
    registry = {
        "agents": {
            "list": [
                {
                    "id": agent.agent_id,
                    "workspace": str(policy.managed_root / agent.workspace_relative_path),  # type: ignore[attr-defined,operator]
                    "tools": {"alsoAllow": list(agent.required_tools)},
                }
                for agent in bundle.registry_policy.agents
            ]
        }
    }
    registry_path = policy.registry_root / "openclaw.json"  # type: ignore[attr-defined,operator]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    os.chmod(registry_path, 0o600)
    _fake_registry_root_owner(monkeypatch, policy.registry_root, admin_uid, admin_gid)  # type: ignore[attr-defined]
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())

    checked = _adapter(policy).check(policy.registry_root, writer)  # type: ignore[attr-defined]

    assert checked.registry.status is OpenClawRegistryStatus.MATCHES
    assert checked.unsafe is False


@pytest.mark.parametrize(
    "case",
    ["automation_owned_root", "operator_owned_root", "world_writable_admin_root", "group_writable_admin_registry_file"],
)
def test_admin_allowance_does_not_weaken_other_registry_authority_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    admin_uid, admin_gid = os.getuid() + 7003, os.getgid() + 7003
    policy = _admin_owned_policy(tmp_path, registry_owner_uid=admin_uid, registry_owner_gid=admin_gid)
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())

    if case == "automation_owned_root":
        _fake_registry_root_owner(
            monkeypatch,
            policy.registry_root,  # type: ignore[attr-defined]
            policy.automation_principal.uid,  # type: ignore[attr-defined]
            policy.automation_principal.gid,  # type: ignore[attr-defined]
        )
    elif case == "operator_owned_root":
        _fake_registry_root_owner(
            monkeypatch,
            policy.registry_root,  # type: ignore[attr-defined]
            policy.operator_principal.uid,  # type: ignore[attr-defined]
            policy.operator_principal.gid,  # type: ignore[attr-defined]
        )
    elif case == "world_writable_admin_root":
        _fake_registry_root_owner(monkeypatch, policy.registry_root, admin_uid, admin_gid)  # type: ignore[attr-defined]
        os.chmod(policy.registry_root, 0o757)  # type: ignore[attr-defined]
    else:
        bundle = load_packaged_openclaw_bundle()
        registry = {
            "agents": {
                "list": [
                    {
                        "id": agent.agent_id,
                        "workspace": str(policy.managed_root / agent.workspace_relative_path),  # type: ignore[attr-defined,operator]
                        "tools": {"alsoAllow": list(agent.required_tools)},
                    }
                    for agent in bundle.registry_policy.agents
                ]
            }
        }
        registry_path = policy.registry_root / "openclaw.json"  # type: ignore[attr-defined,operator]
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        os.chmod(registry_path, 0o660)
        _fake_registry_root_owner(monkeypatch, policy.registry_root, admin_uid, admin_gid)  # type: ignore[attr-defined]

    checked = _adapter(policy).check(policy.registry_root, writer)  # type: ignore[attr-defined]

    assert checked.unsafe is True


def test_alias_walk_tolerates_traversal_only_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect #19: `_descriptor_has_ancestor` must not require read permission on ancestors.

    The registry root's ancestor directory may grant only traversal (`--x`), as on the
    live host where the registry sits inside the admin's home directory and the service
    is granted execute-only ACL access. Opening `..` during the ROOT_ALIAS walk must
    succeed via O_PATH (traversal only), not O_RDONLY (which additionally demands read
    permission on the ancestor itself and fails closed into a blanket UNSAFE result).
    """
    outer = tmp_path / "outer"
    outer.mkdir(mode=0o700)
    admin_uid, admin_gid = os.getuid() + 7004, os.getgid() + 7004
    policy = _admin_owned_policy(outer, registry_owner_uid=admin_uid, registry_owner_gid=admin_gid)
    _fake_registry_root_owner(monkeypatch, policy.registry_root, admin_uid, admin_gid)  # type: ignore[attr-defined]
    writer = ServiceWriterSnapshotV1(os.getuid(), os.getgid())

    os.chmod(outer, 0o300)
    try:
        checked = _adapter(policy).check(policy.registry_root, writer)  # type: ignore[attr-defined]
    finally:
        os.chmod(outer, 0o700)

    assert checked.unsafe is False
    assert checked.registry.status is OpenClawRegistryStatus.MISSING
    assert {row.status for row in checked.files} == {OpenClawCheckStatus.MISSING}
