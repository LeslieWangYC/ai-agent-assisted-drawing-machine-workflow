from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
import venv
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from drawingmachine.config import XdgPaths, resolve_xdg_paths

_PLANNING_TOML = """\
canvas_width_mm = 120.0
canvas_height_mm = 120.0
pen_width_mm = 0.5
min_gap_mm = 0.8
threshold = 128
invert = false
min_component_area_px = 8
simplify_tolerance_mm = 0.12
min_path_length_mm = 0.6
drop_short_stroke_mm = 0.35
merge_endpoint_distance_mm = 0.45
merge_angle_deg = 35.0
dedupe_short_path_length_mm = 2.0
dedupe_distance_mm = 0.3
dedupe_angle_deg = 25.0
dedupe_overlap_ratio = 0.65
hatch_spacing_mm = 0.8
hatch_min_run_mm = 0.8
fill_min_thickness_mm = 0.85
"""

_GCODE_TOML = """\
hardware_canvas_width_mm = 144.0
hardware_canvas_height_mm = 144.0
machine_width_mm = 192.0
machine_height_mm = 192.0
paper_center_x = 96.0
paper_center_y = 96.0
pen_up_z = 3.5
pen_down_z = 0.0
feed_travel = 1200.0
feed_draw = 900.0
feed_pen_down = 100.0
feed_pen_up = 400.0
max_feed = 1200.0
work_coordinate = "G54"
align_mode = "center"
mirror_y = true
safe_start = true
path_mode = "stroke"
"""

_HARDWARE_TOML = """\
firmware = "FluidNC"
transport = "serial"
serial_port = "/dev/serial/by-id/fluidnc-controller"
baud = 115200
line_ending = "\\n"
open_timeout_seconds = 2.0
settle_timeout_seconds = 15.0
read_timeout_seconds = 1.0
command_timeout_seconds = 10.0
home_timeout_seconds = 300.0
stream_idle_timeout_seconds = 30.0
approval_ttl_seconds = 60
require_g54_offset = true
expected_homed_mpos = [192.0, 192.0, 192.0]
position_tolerance_mm = 0.5
post_run_home_z = true

[profile.hardware.automation_principal]
uid = 1100
gid = 1100

[profile.hardware.operator_principal]
uid = 2200
gid = 2200
"""

_LOCAL_COMFYUI_TOML = """\
schema_version = 1
[profile]
name = "local-comfyui"
endpoint = "http://127.0.0.1:8188"
workflow_template = "workflow.json"
model_family = "qwen-image-edit-2511"
scale_to_length = 576
timeout_seconds = 240.0
poll_interval_seconds = 2.0
free_after_run = false
live_execution_requires_execute_flag = true

[profile.workflow_nodes]
load_image = "25"
prompt = "27"
sampler = "28"
save_image = "18"
scale = "221"

[profile.sampler_defaults]
steps = 20
cfg = 4.0
denoise = 0.7
"""

_HARDWARE_KEYS = frozenset(
    {
        "approval",
        "arm_phrase",
        "authorization",
        "bridge_command",
        "bridge_commands",
        "hardware_authority",
        "serial_device",
        "serial_path",
    }
)
_SUBPROCESS_COVERAGE_ROOTS: list[Path] = []

_SERVICE_ACCESS_TEST_HARNESS = r"""\
import os
import stat
import struct
from pathlib import Path

import drawingmachine.cli.service_access as _client_access
import drawingmachine.adapters.filesystem.client_data as _filesystem_client_data
import drawingmachine.config.service_access as _data_access
import drawingmachine.service._instance as _instance
import drawingmachine.service.access_policy as _service_access
import drawingmachine.service.server as _server
from drawingmachine.config.openclaw_deployment import PosixPrincipalV1
from drawingmachine.service.models import PeerCredentials

_CONNECT_GID = int(os.environ["DM_TEST_CONNECT_GID"])
_SERVICE = PosixPrincipalV1(os.getuid(), os.getgid())
_AUTOMATION = PosixPrincipalV1(1100, 1100)
_OPERATOR = PosixPrincipalV1(2200, 2200)
_SOURCE = Path(os.environ["DM_TEST_POLICY_PATH"])
_OUTER = Path(os.environ["DM_TEST_OUTER_RUNTIME"])
_RUNTIME = Path(os.environ["DM_TEST_CANONICAL_RUNTIME"])
_SOCKET = Path(os.environ["DM_TEST_CANONICAL_SOCKET"])
_AUTOMATION_ENDPOINT = Path(os.environ["DM_TEST_AUTOMATION_ENDPOINT"])
_OPERATOR_ENDPOINT = Path(os.environ["DM_TEST_OPERATOR_ENDPOINT"])
_AUTOMATION_IMPORT_ENDPOINT = Path(os.environ["DM_TEST_AUTOMATION_IMPORT_ENDPOINT"])
_AUTOMATION_EXPORT_ENDPOINT = Path(os.environ["DM_TEST_AUTOMATION_EXPORT_ENDPOINT"])
_CLIENT_DATA_IMPORT_ROOT = Path(os.environ["DM_TEST_CLIENT_DATA_IMPORT_ROOT"])
_CLIENT_DATA_EXPORT_ROOT = Path(os.environ["DM_TEST_CLIENT_DATA_EXPORT_ROOT"])
_EVENTS = Path(os.environ["DM_TEST_ACCESS_EVENTS"])
_PEER_ROLE = Path(os.environ["DM_TEST_PEER_ROLE"])
_REAL_LSTAT = os.lstat
_REAL_GETXATTR = os.getxattr

if "DM_TEST_FIXED_OPENCLAW_REQUEST_ID" in os.environ:
    import drawingmachine.cli.client as _service_client
    from uuid import UUID

    _service_client.uuid4 = lambda: UUID(os.environ["DM_TEST_FIXED_OPENCLAW_REQUEST_ID"])


def _selected_client():
    role = os.environ.get("DM_TEST_CLIENT_ROLE", "automation")
    return _OPERATOR if role == "operator" else _AUTOMATION


def _modeled_lstat(path, *args, **kwargs):
    value = _REAL_LSTAT(path, *args, **kwargs)
    candidate = Path(path)
    endpoint = not args and not kwargs and candidate in {
        _AUTOMATION_IMPORT_ENDPOINT,
        _AUTOMATION_EXPORT_ENDPOINT,
    }
    client_owned = False
    if not args and not kwargs:
        for root in (_CLIENT_DATA_IMPORT_ROOT, _AUTOMATION_IMPORT_ENDPOINT):
            if candidate.is_relative_to(root):
                relative = candidate.relative_to(root)
                client_owned = len(relative.parts) >= 3 and relative.parts[1] in {
                    "prepare",
                    "drop",
                    "admitted",
                    "quarantine",
                }
                break
    if endpoint or client_owned:
        fields = list(value)
        fields[4] = _AUTOMATION.uid
        fields[5] = _CONNECT_GID
        return os.stat_result(fields)
    return value


_data_access.os.lstat = _modeled_lstat


def _named_acl(mode, permissions, include_service=False):
    entries = [
        struct.pack("<HHI", 1, mode >> 6 & 7, 0),
        struct.pack("<HHI", 2, permissions, _AUTOMATION.uid),
        struct.pack("<HHI", 4, 0, 0),
        struct.pack("<HHI", 16, mode >> 3 & 7, 0),
        struct.pack("<HHI", 32, mode & 7, 0),
    ]
    if include_service:
        entries.append(struct.pack("<HHI", 2, 7, _SERVICE.uid))
    return b"".join((struct.pack("<I", 2), *entries))


def _modeled_getxattr(path, attribute, *args, **kwargs):
    candidate = Path(path)
    import_relative = candidate.relative_to(_CLIENT_DATA_IMPORT_ROOT) if candidate.is_relative_to(_CLIENT_DATA_IMPORT_ROOT) else None
    import_directory = candidate == _CLIENT_DATA_IMPORT_ROOT or (
        import_relative is not None
        and (len(import_relative.parts) == 1 or (
            len(import_relative.parts) == 2 and import_relative.parts[1] in {"prepare", "drop"}
        ))
    )
    jobs = _CLIENT_DATA_EXPORT_ROOT / "jobs"
    export_entry = candidate == jobs or candidate.is_relative_to(jobs)
    if attribute in {"system.posix_acl_access", "system.posix_acl_default"}:
        if import_directory:
            return _named_acl(0o2770, 0o7, include_service=True)
        if export_entry:
            value = _REAL_LSTAT(candidate)
            if stat.S_ISDIR(value.st_mode):
                return _named_acl(0o2750, 0o5)
            if attribute == "system.posix_acl_access" and stat.S_ISREG(value.st_mode):
                return _named_acl(0o640, 0o5)
    return _REAL_GETXATTR(path, attribute, *args, **kwargs)


_data_access.os.getxattr = _modeled_getxattr
_REAL_PEER_CREDENTIALS = _server._peer_credentials


def _modeled_peer_credentials(writer):
    actual = _REAL_PEER_CREDENTIALS(writer)
    principal = _OPERATOR if _PEER_ROLE.read_text(encoding="ascii").strip() == "operator" else _AUTOMATION
    return PeerCredentials(actual.pid, principal.uid, principal.gid)


_server._peer_credentials = _modeled_peer_credentials
_REAL_LEASE_ACQUIRE = _instance.InstanceLease.acquire


def _acquire_with_client_data_roots(lease):
    _REAL_LEASE_ACQUIRE(lease)


_instance.InstanceLease.acquire = _acquire_with_client_data_roots


def _event(name):
    with _EVENTS.open("a", encoding="ascii") as stream:
        stream.write(name + "\n")


_REAL_REGISTER_DECODED = _filesystem_client_data.FilesystemServiceClientData.register_decoded


def _record_register_decoded(self, request_id, role, staged_path):
    _event(f"decoded|{request_id}|{role.value}|{staged_path}")
    return _REAL_REGISTER_DECODED(self, request_id, role, staged_path)


_filesystem_client_data.FilesystemServiceClientData.register_decoded = _record_register_decoded


def _service_acl(mode):
    return tuple(sorted((
        _service_access.PosixAclEntryV1("user_obj", None, mode >> 6 & 7),
        _service_access.PosixAclEntryV1("group_obj", None, mode >> 3 & 7),
        _service_access.PosixAclEntryV1("other", None, mode & 7),
    )))


def _service_kind(mode):
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


class _TestServiceFactsInspector:
    def current_principal(self):
        _event("pre_lease_snapshot")
        return _SERVICE

    def supplemental_gids(self, principal):
        return frozenset({principal.gid, _CONNECT_GID})

    def group_member_uids(self, gid):
        assert gid == _CONNECT_GID
        return frozenset({_SERVICE.uid, _AUTOMATION.uid, _OPERATOR.uid})

    def inspect(self, path):
        value = os.lstat(path)
        mode = stat.S_IMODE(value.st_mode)
        acl = _service_acl(mode)
        if path == _OUTER:
            acl = tuple(sorted((
                _service_access.PosixAclEntryV1("user_obj", None, 7),
                _service_access.PosixAclEntryV1("group_obj", None, 0),
                _service_access.PosixAclEntryV1("group", _CONNECT_GID, 1),
                _service_access.PosixAclEntryV1("mask", None, 1),
                _service_access.PosixAclEntryV1("other", None, 0),
            )))
        if path == _SOURCE:
            _event("policy_load")
        if path == _SOCKET and stat.S_ISSOCK(value.st_mode) and mode == 0o660:
            _event("post_bind_revalidation")
        return _service_access.PathAccessFactsV1(
            path,
            _service_kind(value.st_mode),
            value.st_dev,
            value.st_ino,
            value.st_uid,
            value.st_gid,
            mode,
            acl,
        )


class _TestClientFactsInspector:
    def current_principal(self):
        _event("client_snapshot")
        return _selected_client()

    def supplemental_gids(self, principal):
        return frozenset({principal.gid, _CONNECT_GID})

    def inspect(self, path):
        value = os.lstat(path)
        if stat.S_ISDIR(value.st_mode):
            kind = "directory"
        elif stat.S_ISREG(value.st_mode):
            kind = "regular"
        elif stat.S_ISSOCK(value.st_mode):
            kind = "socket"
        elif stat.S_ISLNK(value.st_mode):
            kind = "symlink"
        else:
            kind = "special"
        uid, gid = value.st_uid, value.st_gid
        principal = _selected_client()
        selected_endpoint = _OPERATOR_ENDPOINT if principal == _OPERATOR else _AUTOMATION_ENDPOINT
        if path in {selected_endpoint.parent, selected_endpoint}:
            uid, gid = principal.uid, principal.gid
        elif path == _SOCKET:
            uid, gid = _SERVICE.uid, _CONNECT_GID
        return _client_access.ClientPathFactsV1(
            path,
            kind,
            value.st_dev,
            value.st_ino,
            uid,
            gid,
            stat.S_IMODE(value.st_mode),
        )


_service_access.LinuxAccessFactsInspector = _TestServiceFactsInspector
_client_access.LinuxClientAccessInspector = _TestClientFactsInspector
"""


@dataclass(frozen=True)
class ServiceAccessTestHarness:
    root: Path

    @property
    def events_path(self) -> Path:
        return self.root / "events.log"

    def events(self) -> set[str]:
        if not self.events_path.exists():
            return set()
        return set(self.events_path.read_text(encoding="ascii").splitlines())

    def decoded_requests(self) -> tuple[tuple[str, str, Path], ...]:
        if not self.events_path.exists():
            return ()
        return tuple(
            (request_id, role, Path(path))
            for line in self.events_path.read_text(encoding="ascii").splitlines()
            if line.startswith("decoded|")
            for request_id, role, path in (line.split("|", 3)[1:],)
        )

    @property
    def peer_role_path(self) -> Path:
        return self.root / "peer-role"

    def select_peer(self, role: str) -> None:
        assert role in {"automation", "operator"}
        self.peer_role_path.write_text(role, encoding="ascii")

    def client_environment(self, environment: Mapping[str, str], role: str) -> dict[str, str]:
        self.select_peer(role)
        selected = environment.copy()
        selected["DM_TEST_CLIENT_ROLE"] = role
        selected["XDG_RUNTIME_DIR"] = str(self.root / f"{role}-runtime")
        return selected

    def install(self, environment: dict[str, str]) -> dict[str, str]:
        connect_gid = next(gid for gid in os.getgroups() if gid != os.getgid())
        outer = Path(environment["XDG_RUNTIME_DIR"])
        outer.mkdir(parents=True, exist_ok=True)
        outer.chmod(0o710)
        canonical_runtime = outer / "drawingmachine"
        canonical_socket = canonical_runtime / "service.sock"
        automation_root = self.root / "automation-runtime"
        automation_runtime = automation_root / "drawingmachine"
        operator_root = self.root / "operator-runtime"
        operator_runtime = operator_root / "drawingmachine"
        automation_runtime.mkdir(parents=True, mode=0o700)
        operator_runtime.mkdir(parents=True, mode=0o700)
        automation_runtime.chmod(0o700)
        operator_runtime.chmod(0o700)
        automation_endpoint = automation_runtime / "service.sock"
        automation_endpoint.symlink_to(canonical_socket)
        operator_endpoint = operator_runtime / "service.sock"
        operator_endpoint.symlink_to(canonical_socket)
        config_dir = Path(environment["XDG_CONFIG_HOME"]) / "drawingmachine"
        policy_path = config_dir / "service-access.toml"
        data_dir = Path(environment["XDG_DATA_HOME"]) / "drawingmachine"
        automation_data = self.root / "automation-data"
        automation_data.mkdir(parents=True)
        automation_import_endpoint = automation_data / "imports"
        automation_export_endpoint = automation_data / "exports"
        automation_import_endpoint.symlink_to(data_dir / "imports/automation")
        automation_export_endpoint.symlink_to(data_dir / "exports/automation")
        import_root = data_dir / "imports/automation"
        export_root = data_dir / "exports/automation"
        for data_root in (import_root, export_root):
            data_root.mkdir(parents=True, exist_ok=True)
            os.chown(data_root, os.getuid(), connect_gid)
            data_root.chmod(0o2770)
        for role in ("image", "gcode"):
            role_root = import_root / role
            role_root.mkdir(exist_ok=True)
            os.chown(role_root, os.getuid(), connect_gid)
            role_root.chmod(0o2770)
            for phase in ("prepare", "drop"):
                directory = role_root / phase
                directory.mkdir(exist_ok=True)
                os.chown(directory, os.getuid(), connect_gid)
                directory.chmod(0o2770)
            for phase in ("admitted", "quarantine", "observations"):
                directory = role_root / phase
                directory.mkdir(exist_ok=True)
                os.chown(directory, os.getuid(), os.getgid())
                directory.chmod(0o700)
        jobs = export_root / "jobs"
        jobs.mkdir(exist_ok=True)
        os.chown(jobs, os.getuid(), connect_gid)
        jobs.chmod(0o2750)
        policy_path.write_text(
            f'''\
schema_version = 1
canonical_runtime_dir = "{canonical_runtime}"
canonical_socket = "{canonical_socket}"
automation_runtime_endpoint = "{automation_endpoint}"
operator_runtime_endpoint = "{operator_endpoint}"
canonical_data_dir = "{data_dir}"
automation_import_root = "{data_dir / "imports/automation"}"
automation_export_root = "{data_dir / "exports/automation"}"
automation_import_endpoint = "{automation_import_endpoint}"
automation_export_endpoint = "{automation_export_endpoint}"
openclaw_policy_path = "{config_dir / "openclaw-deployment.toml"}"
[service_principal]
uid = {os.getuid()}
gid = {os.getgid()}
[automation_principal]
uid = 1100
gid = 1100
[operator_principal]
uid = 2200
gid = 2200
[connect_group]
name = "drawingmachine-connect"
gid = {connect_gid}
''',
            encoding="utf-8",
        )
        os.chown(policy_path, os.getuid(), connect_gid)
        policy_path.chmod(0o640)
        environment.update(
            {
                "DM_TEST_ACCESS_EVENTS": str(self.events_path),
                "DM_TEST_AUTOMATION_ENDPOINT": str(automation_endpoint),
                "DM_TEST_AUTOMATION_IMPORT_ENDPOINT": str(automation_import_endpoint),
                "DM_TEST_AUTOMATION_EXPORT_ENDPOINT": str(automation_export_endpoint),
                "DM_TEST_CLIENT_DATA_IMPORT_ROOT": str(import_root),
                "DM_TEST_CLIENT_DATA_EXPORT_ROOT": str(export_root),
                "DM_TEST_CANONICAL_RUNTIME": str(canonical_runtime),
                "DM_TEST_CANONICAL_SOCKET": str(canonical_socket),
                "DM_TEST_CONNECT_GID": str(connect_gid),
                "DM_TEST_OUTER_RUNTIME": str(outer),
                "DM_TEST_OPERATOR_ENDPOINT": str(operator_endpoint),
                "DM_TEST_PEER_ROLE": str(self.peer_role_path),
                "DM_TEST_POLICY_PATH": str(policy_path),
            }
        )
        self.select_peer("automation")
        existing = environment.get("PYTHONPATH")
        paths = [] if not existing else [Path(item) for item in existing.split(os.pathsep) if item]
        startup_dir = next((path for path in paths if (path / "sitecustomize.py").is_file()), self.root)
        startup_dir.mkdir(parents=True, exist_ok=True)
        with (startup_dir / "sitecustomize.py").open("a", encoding="utf-8") as stream:
            stream.write("\n" + _SERVICE_ACCESS_TEST_HARNESS)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(startup_dir), *(str(path) for path in paths if path != startup_dir)]
        )
        return self.client_environment(environment, "automation")


@pytest.fixture
def service_access_test_harness(tmp_path: Path) -> ServiceAccessTestHarness:
    return ServiceAccessTestHarness(tmp_path / "service-access-test-harness")


def _write_local_comfyui_profile(config_dir: Path) -> None:
    providers_dir = config_dir / "providers"
    (providers_dir / "workflow.json").write_text(
        json.dumps(
            {
                "25": {"inputs": {"image": "old.png"}},
                "27": {"inputs": {"prompt": "Template prompt."}},
                "28": {"inputs": {"seed": 1, "steps": 20, "cfg": 7.0, "denoise": 0.5}},
                "18": {"inputs": {"filename_prefix": "old"}},
                "221": {"inputs": {"scale_to_length": 576}},
            }
        ),
        encoding="utf-8",
    )
    (providers_dir / "local-comfyui.toml").write_text(_LOCAL_COMFYUI_TOML, encoding="utf-8")


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def golden_root(repository_root: Path) -> Path:
    return repository_root / "tests/fixtures/package_b/golden"


@dataclass(frozen=True)
class PackageCRepositoryResidueEntry:
    relative_path: str
    kind: str
    mode: int
    payload: bytes


@dataclass(frozen=True)
class PackageCRepositoryResidueSnapshot:
    entries: tuple[PackageCRepositoryResidueEntry, ...]


def _package_c_residue_paths(repository_root: Path) -> tuple[Path, ...]:
    selected: set[Path] = set()

    def add_tree(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        selected.add(path)
        if path.is_dir() and not path.is_symlink():
            selected.update(path.rglob("*"))

    for name in ("build", "dist"):
        add_tree(repository_root / name)
    for path in repository_root.rglob("*.egg-info"):
        relative = path.relative_to(repository_root)
        if not ({".git", ".venv"} & set(relative.parts)):
            add_tree(path)
    for pattern in (".coverage*", "coverage*.json", "*-coverage.json", "coverage.xml"):
        for path in repository_root.glob(pattern):
            add_tree(path)
    excluded = {".git", ".venv", ".mypy_cache", ".pytest_cache", "__pycache__", "build", "dist"}
    for current, directories, files in os.walk(repository_root):
        directories[:] = [name for name in directories if name not in excluded and not name.endswith(".egg-info")]
        current_path = Path(current)
        for name in files:
            if name == "drawingmachine.db" or name.endswith((".sock", ".db-wal", ".db-shm", ".pid")):
                selected.add(current_path / name)
    return tuple(sorted(selected, key=lambda path: path.relative_to(repository_root).as_posix()))


def package_c_repository_residue_snapshot(repository_root: Path) -> PackageCRepositoryResidueSnapshot:
    entries: list[PackageCRepositoryResidueEntry] = []
    for path in _package_c_residue_paths(repository_root):
        relative = path.relative_to(repository_root).as_posix()
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink():
            kind = "symlink"
            payload = os.fsencode(os.readlink(path))
        elif path.is_dir():
            kind = "directory"
            payload = b""
        elif path.is_file():
            kind = "file"
            payload = path.read_bytes()
        else:
            kind = "special"
            payload = b""
        entries.append(PackageCRepositoryResidueEntry(relative, kind, mode, payload))
    return PackageCRepositoryResidueSnapshot(tuple(entries))


def restore_package_c_repository_residue_snapshot(
    repository_root: Path,
    snapshot: PackageCRepositoryResidueSnapshot,
) -> None:
    expected = {entry.relative_path: entry for entry in snapshot.entries}
    current = {entry.relative_path: entry for entry in package_c_repository_residue_snapshot(repository_root).entries}
    for relative in sorted(set(current) - set(expected), key=lambda value: (value.count("/"), value), reverse=True):
        path = repository_root / relative
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    for relative, entry in sorted(expected.items(), key=lambda item: (item[0].count("/"), item[0])):
        path = repository_root / relative
        if entry.kind == "directory":
            if path.is_symlink() or path.is_file():
                path.unlink()
            path.mkdir(parents=True, exist_ok=True)
        elif entry.kind == "symlink":
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(os.fsdecode(entry.payload))
        elif entry.kind == "file":
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.is_symlink():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(entry.payload)
        elif not path.exists():
            raise AssertionError(f"cannot restore pre-existing special residue path: {relative}")
        if not path.is_symlink():
            path.chmod(entry.mode)


def package_c_descendant_pids(pid: int | None = None) -> tuple[int, ...]:
    root_pid = os.getpid() if pid is None else pid
    pending = [root_pid]
    visited = {root_pid}
    descendants: set[int] = set()
    while pending:
        parent = pending.pop()
        try:
            tasks = tuple((Path(f"/proc/{parent}/task")).iterdir())
        except (FileNotFoundError, OSError):
            continue
        for task in tasks:
            try:
                values = (task / "children").read_text(encoding="ascii").split()
            except (FileNotFoundError, OSError):
                continue
            for value in values:
                try:
                    child = int(value)
                except ValueError:
                    continue
                descendants.add(child)
                if child not in visited:
                    visited.add(child)
                    pending.append(child)
    return tuple(sorted(descendants))


def assert_package_c_repository_residue_unchanged(
    repository_root: Path,
    before: PackageCRepositoryResidueSnapshot,
    *,
    check_descendants: bool = True,
    expected_descendants: tuple[int, ...] = (),
) -> None:
    after = package_c_repository_residue_snapshot(repository_root)
    descendants = package_c_descendant_pids() if check_descendants else expected_descendants
    if after != before or descendants != expected_descendants:
        before_paths = {entry.relative_path for entry in before.entries}
        after_paths = {entry.relative_path for entry in after.entries}
        changed = sorted(before_paths ^ after_paths)
        if after != before and not changed:
            changed = sorted(after_paths)
        added = tuple(sorted(set(descendants) - set(expected_descendants)))
        missing = tuple(sorted(set(expected_descendants) - set(descendants)))
        raise AssertionError(
            f"Package C repository residue detected: paths={changed!r} "
            f"descendants_added={added!r} descendants_missing={missing!r}"
        )


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory: pytest.TempPathFactory, repository_root: Path) -> Path:
    wheel_dir = tmp_path_factory.mktemp("drawingmachine-wheel")
    before = package_c_repository_residue_snapshot(repository_root)
    try:
        build_result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheel_dir)],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        restore_package_c_repository_residue_snapshot(repository_root, before)
    assert_package_c_repository_residue_unchanged(repository_root, before, check_descendants=False)
    assert build_result.returncode == 0, build_result.stdout + build_result.stderr

    wheels = tuple(wheel_dir.glob("drawingmachine-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture(scope="session")
def installed_drawingmachine(
    tmp_path_factory: pytest.TempPathFactory,
    repository_root: Path,
    built_wheel: Path,
) -> Path:
    environment_dir = tmp_path_factory.mktemp("drawingmachine-install") / "home/.local/lib/drawingmachine/venv"
    venv.EnvBuilder(with_pip=True).create(environment_dir)
    python = environment_dir / "bin" / "python"
    fresh_site_result = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert fresh_site_result.returncode == 0, fresh_site_result.stdout + fresh_site_result.stderr
    fresh_site = Path(fresh_site_result.stdout.strip()).resolve()
    dependency_site = next(
        Path(entry).resolve()
        for entry in sys.path
        if entry and (Path(entry) / "PIL").is_dir() and (Path(entry) / "pytest").is_dir()
    )
    assert fresh_site.is_dir() and dependency_site.is_dir()
    assert fresh_site != dependency_site
    assert dependency_site.name == "site-packages"
    assert not dependency_site.is_relative_to(repository_root / "src")
    (fresh_site / "drawingmachine-local-dependencies.pth").write_text(
        f"{dependency_site}\n",
        encoding="utf-8",
    )
    install_result = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(built_wheel)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert install_result.returncode == 0, install_result.stdout + install_result.stderr
    provenance_result = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.util, json, pathlib, sys; import PIL, drawingmachine; "
                "serial_spec = importlib.util.find_spec('serial'); assert serial_spec is not None; "
                "print(json.dumps({'drawingmachine': drawingmachine.__file__, 'PIL': PIL.__file__, "
                "'serial': serial_spec.origin, 'sys_path': sys.path}))"
            ),
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert provenance_result.returncode == 0, provenance_result.stdout + provenance_result.stderr
    provenance = cast(dict[str, object], json.loads(provenance_result.stdout))
    drawingmachine_path = Path(cast(str, provenance["drawingmachine"])).resolve()
    pillow_path = Path(cast(str, provenance["PIL"])).resolve()
    serial_path = Path(cast(str, provenance["serial"])).resolve()
    search_path = [Path(item).resolve() for item in cast(list[str], provenance["sys_path"]) if item]
    assert drawingmachine_path.is_relative_to(fresh_site)
    assert pillow_path.is_relative_to(dependency_site)
    assert serial_path.is_relative_to(dependency_site)
    assert search_path.index(fresh_site) < search_path.index(dependency_site)
    return environment_dir / "bin" / "drawingmachine"


def _package_b_environment(root: Path, *, runtime_root: Path | None = None) -> dict[str, str]:
    roots = {
        "XDG_CONFIG_HOME": root / "config",
        "XDG_STATE_HOME": root / "state",
        "XDG_DATA_HOME": root / "data",
        "XDG_RUNTIME_DIR": root / "run" if runtime_root is None else runtime_root,
    }
    config_dir = roots["XDG_CONFIG_HOME"] / "drawingmachine"
    (config_dir / "machines").mkdir(parents=True)
    (config_dir / "providers").mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        'schema_version = 1\nmachine_profile = "package-b"\n'
        'provider_profile = "local-comfyui"\nlog_level = "WARNING"\n',
        encoding="utf-8",
    )
    (config_dir / "machines/package-b.toml").write_text(
        'schema_version = 1\n[profile]\nname = "package-b"\n'
        f"[profile.planning]\n{_PLANNING_TOML}"
        f"[profile.gcode]\n{_GCODE_TOML}"
        f"[profile.hardware]\n{_HARDWARE_TOML}",
        encoding="utf-8",
    )
    _write_local_comfyui_profile(config_dir)
    data_dir = roots["XDG_DATA_HOME"] / "drawingmachine"
    for role in ("image", "gcode"):
        for phase in ("prepare", "drop"):
            directory = data_dir / "imports/automation" / role / phase
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o2770)
    (data_dir / "exports/automation").mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({name: str(path) for name, path in roots.items()})
    environment["HOME"] = str(root / "home")
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    return environment


@pytest.fixture
def package_b_xdg_environment(
    tmp_path: Path,
    pytestconfig: pytest.Config,
    repository_root: Path,
    service_access_test_harness: ServiceAccessTestHarness,
) -> Iterator[tuple[dict[str, str], dict[str, str]]]:
    runtime_root = Path(tempfile.mkdtemp(prefix="dm-b15-", dir="/tmp"))
    root = tmp_path / "package-b-xdg"
    environment = _package_b_environment(root, runtime_root=runtime_root)
    client_environment = service_access_test_harness.install(environment)
    _install_subprocess_coverage(root, environment, pytestconfig, repository_root)
    try:
        yield environment, client_environment
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


@dataclass
class PackageBService:
    executable: Path
    environment: dict[str, str]
    cwd: Path
    client_environment: dict[str, str] | None = None
    process: subprocess.Popen[str] | None = None
    audit_log: Path | None = None

    @property
    def runtime_root(self) -> Path:
        return Path(self.environment["XDG_RUNTIME_DIR"])

    @property
    def socket_path(self) -> Path:
        return self.runtime_root / "drawingmachine/service.sock"

    @property
    def database_path(self) -> Path:
        return Path(self.environment["XDG_STATE_HOME"]) / "drawingmachine/drawingmachine.db"

    @property
    def jobs_dir(self) -> Path:
        return Path(self.environment["XDG_DATA_HOME"]) / "drawingmachine/jobs"

    def start(self) -> PackageBService:
        assert self.process is None
        self.process = subprocess.Popen(
            [str(self.executable), "--output", "json", "service", "run"],
            cwd=self.cwd,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 15.0
        last: subprocess.CompletedProcess[str] | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise AssertionError(f"service exited before readiness: stdout={stdout!r} stderr={stderr!r}")
            if self.socket_path.exists():
                last = self._run("service", "status", timeout=2.0)
                if last.returncode == 0:
                    return self
            time.sleep(0.02)
        raise AssertionError(f"service did not become ready: {last}")

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15.0)
        except subprocess.TimeoutExpired as error:
            process.kill()
            stdout, stderr = process.communicate(timeout=5.0)
            raise AssertionError(f"service did not stop cleanly: stdout={stdout!r} stderr={stderr!r}") from error
        finally:
            self.process = None
        assert process.returncode == 0, f"service failed: stdout={stdout!r} stderr={stderr!r}"
        assert not self.socket_path.exists()

    def restart(self) -> PackageBService:
        self.stop()
        return self.start()

    def _run(self, *arguments: str, timeout: float = 45.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.executable), "--output", "json", *arguments],
            cwd=self.cwd,
            env=self.environment if self.client_environment is None else self.client_environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def run_json(
        self,
        *arguments: str,
        expected_returncode: int = 0,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        result = self._run(*arguments, timeout=timeout)
        assert result.returncode == expected_returncode, result.stdout + result.stderr
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        assert set(payload) == {"schema_version", "ok", "command", "request_id", "data", "error"}
        return cast(dict[str, Any], payload)

    def wait(self, job_id: str) -> dict[str, Any]:
        return self.run_json(
            "job",
            "wait",
            job_id,
            "--timeout-seconds",
            "30",
            "--poll-interval-seconds",
            "0.02",
        )

    def status(self, job_id: str) -> dict[str, Any]:
        return self.run_json("job", "status", job_id)

    @staticmethod
    def artifact(payload: Mapping[str, Any], role: str) -> dict[str, Any]:
        artifacts = payload["data"]["artifacts"]
        matches = [item for item in artifacts if isinstance(item, dict) and item.get("role") == role]
        assert len(matches) == 1
        return cast(dict[str, Any], matches[0])

    def artifact_path(self, job_id: str, artifact: Mapping[str, Any]) -> Path:
        return self.jobs_dir / job_id / cast(str, artifact["relative_path"])

    def write_pass_review(self, blocked: Mapping[str, Any]) -> Path:
        job = cast(dict[str, Any], blocked["data"]["job"])
        processed = self.artifact(blocked, "processed_image")
        processed_path = self.artifact_path(cast(str, job["job_id"]), processed)
        review_dir = self.cwd / "reviews"
        review_dir.mkdir(exist_ok=True)
        path = review_dir / f"{job['job_id']}.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "processed_image_review_v1",
                    "job_name": job["name"],
                    "reviewed_at": "2026-07-11T00:00:00+00:00",
                    "reviewer": "test:package-b-acceptance",
                    "processed_image": str(processed_path),
                    "handoff": None,
                    "status": "PASS_TO_BUILD",
                    "checks": {
                        "recognizable_subject": True,
                        "background_simplified": True,
                        "black_on_white": True,
                        "no_edge_clipping": True,
                        "line_density_drawable": True,
                        "no_soft_gradients": True,
                        "limited_tiny_isolated_marks": True,
                        "vectorization_complexity_ok": True,
                    },
                    "issues": [],
                    "revised_prompt": None,
                    "next_allowed_stage": "build_drawable_job",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def forbidden_hardware_keys(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            found.update(str(key) for key in value if str(key).lower() in _HARDWARE_KEYS)
            for child in value.values():
                found.update(PackageBService.forbidden_hardware_keys(child))
        elif isinstance(value, list):
            for child in value:
                found.update(PackageBService.forbidden_hardware_keys(child))
        return found

    def simulate_interrupted_state(self, job_id: str, state: str) -> None:
        self.stop()
        with sqlite3.connect(self.database_path) as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = ?, revision = revision + 1,
                    blocker_json = NULL, error_json = NULL, ready_snapshot_json = NULL
                WHERE job_id = ?
                """,
                (state, job_id),
            ).rowcount
        assert changed == 1

    @staticmethod
    def repository_snapshot(repository_root: Path) -> tuple[str, tuple[tuple[str, str], ...]]:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        names = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repository_root,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        digests = tuple(
            (name.decode(), hashlib.sha256((repository_root / name.decode()).read_bytes()).hexdigest())
            for name in names
            if name
        )
        return status, digests

    def forbidden_events(self) -> list[str]:
        if self.audit_log is None or not self.audit_log.exists():
            return []
        return [line for line in self.audit_log.read_text(encoding="utf-8").splitlines() if line]


@dataclass(frozen=True)
class PackageCAuditGuard:
    root: Path
    environment: dict[str, str]
    audit_log: Path

    def events(self) -> list[str]:
        if not self.audit_log.exists():
            return []
        return [line for line in self.audit_log.read_text(encoding="utf-8").splitlines() if line]

    def run_probe(self, probe: str) -> subprocess.CompletedProcess[str]:
        probes = {
            "inet4": "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
            "inet6": "import socket; socket.socket(socket.AF_INET6, socket.SOCK_STREAM)",
            "serial-import": "import serial",
            "serial-device": "open('/dev/ttyS0', 'rb')",
            "serial-open": "import os; os.open('/dev/ttyS0', os.O_RDONLY)",
            "openclaw-cli": "import subprocess; subprocess.run(['openclaw', 'status'])",
            "openclaw-path": "import subprocess; subprocess.run(['/usr/bin/openclaw', 'status'])",
            "openclaw-shell": "import subprocess; subprocess.run('openclaw status', shell=True)",
            "systemd": "import subprocess; subprocess.run(['systemctl', '--version'])",
            "systemd-path": "import subprocess; subprocess.run(['/usr/bin/systemctl', '--version'])",
            "systemd-shell": "import subprocess; subprocess.run('systemctl --version', shell=True)",
            "outside-xdg-unix": "import socket; sock = socket.socket(socket.AF_UNIX); sock.connect('/tmp/package-c.sock')",
        }
        return subprocess.run(
            [sys.executable, "-c", probes[probe]],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )


@pytest.fixture
def package_b_service(
    installed_drawingmachine: Path,
    package_b_xdg_environment: tuple[dict[str, str], dict[str, str]],
    tmp_path: Path,
) -> Iterator[PackageBService]:
    environment, client_environment = package_b_xdg_environment
    service = PackageBService(
        installed_drawingmachine,
        environment,
        tmp_path,
        client_environment=client_environment,
    ).start()
    try:
        yield service
    finally:
        service.stop()


def _install_audit_guard(root: Path, environment: dict[str, str]) -> Path:
    guard_dir = root / "audit-guard"
    guard_dir.mkdir()
    audit_log = root / "audit-events.log"
    (guard_dir / "sitecustomize.py").write_text(
        """\
import os
import socket

_LOG = os.environ["DRAWINGMACHINE_TEST_AUDIT_LOG"]
_RUNTIME = os.path.realpath(os.environ["XDG_RUNTIME_DIR"])
_CANONICAL_SOCKET = os.path.realpath(os.environ["DM_TEST_CANONICAL_SOCKET"])

def _reject(message):
    with open(_LOG, "a", encoding="utf-8") as stream:
        stream.write(message + "\\n")
    raise RuntimeError("Package B live boundary denied")

def _audit(event, args):
    if event == "socket.__new__" and len(args) > 1 and args[1] in {socket.AF_INET, socket.AF_INET6}:
        _reject("network-socket")
    if event in {"socket.bind", "socket.connect"} and len(args) > 1:
        address = args[1]
        if isinstance(address, tuple):
            _reject("network-address")
        if isinstance(address, (str, bytes)):
            value = os.path.realpath(os.fsdecode(address))
            if value != _CANONICAL_SOCKET and os.path.commonpath((_RUNTIME, value)) != _RUNTIME:
                _reject("unix-socket-outside-xdg")
    if event == "import" and args and str(args[0]).split(".", 1)[0] in {"serial", "openclaw"}:
        _reject("forbidden-import:" + str(args[0]))
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        value = os.fsdecode(args[0]).lower()
        if value.startswith(("/dev/tty", "/dev/serial")):
            _reject("serial-open")

import sys
sys.addaudithook(_audit)
""",
        encoding="utf-8",
    )
    environment["PYTHONPATH"] = str(guard_dir)
    environment["DRAWINGMACHINE_TEST_AUDIT_LOG"] = str(audit_log)
    return audit_log


def _install_package_c_audit_guard(root: Path, environment: dict[str, str]) -> Path:
    guard_dir = root / "package-c-audit-guard"
    guard_dir.mkdir()
    runtime = root / "runtime"
    runtime.mkdir()
    audit_log = root / "package-c-audit-events.log"
    (guard_dir / "sitecustomize.py").write_text(
        """\\
import os
import socket
import subprocess
import sys

_LOG = os.environ["DRAWINGMACHINE_PACKAGE_C_AUDIT_LOG"]
_RUNTIME = os.path.realpath(os.environ["XDG_RUNTIME_DIR"])

def _reject(event):
    with open(_LOG, "a", encoding="utf-8") as stream:
        stream.write(event + "\\n")
    raise RuntimeError("Package C live boundary denied: " + event)

def _audit(event, args):
    if event == "socket.__new__" and len(args) > 1 and args[1] in {socket.AF_INET, socket.AF_INET6}:
        _reject("inet4" if args[1] == socket.AF_INET else "inet6")
    if event in {"socket.bind", "socket.connect"} and len(args) > 1:
        address = args[1]
        if isinstance(address, tuple):
            _reject("network-address")
        if isinstance(address, (str, bytes)):
            value = os.path.realpath(os.fsdecode(address))
            if os.path.commonpath((_RUNTIME, value)) != _RUNTIME:
                _reject("outside-xdg-unix")
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        value = os.fsdecode(args[0]).lower()
        if value.startswith(("/dev/tty", "/dev/serial")):
            _reject("serial-open" if len(args) > 1 and args[1] is None else "serial-device")
    if event == "subprocess.Popen" and args:
        command = args[1] if len(args) > 1 else args[0]
        values = (str(args[0]),) + ((command,) if isinstance(command, str) else tuple(str(item) for item in command))
        joined = " ".join(values)
        lowered = joined.lower()
        command_names = {os.path.basename(item).lower() for item in values}
        if command_names & {"systemctl", "systemd-run"} or "systemctl" in lowered or "systemd-run" in lowered:
            _reject("systemd")
        if "openclaw" in lowered:
            _reject("openclaw-cli")
    if event == "import" and args:
        imported = str(args[0]).split(".", 1)[0]
        if imported == "serial":
            _reject("serial-import")
        if imported == "openclaw":
            _reject("openclaw")

sys.addaudithook(_audit)
""",
        encoding="utf-8",
    )
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(guard_dir) if not existing else str(guard_dir) + os.pathsep + existing
    environment["XDG_RUNTIME_DIR"] = str(runtime)
    environment["DRAWINGMACHINE_PACKAGE_C_AUDIT_LOG"] = str(audit_log)
    return audit_log


def _install_subprocess_coverage(
    root: Path,
    environment: dict[str, str],
    pytestconfig: pytest.Config,
    repository_root: Path,
) -> None:
    if not pytestconfig.getoption("cov_source"):
        return
    existing = environment.get("PYTHONPATH")
    existing_paths = [] if not existing else [Path(item) for item in existing.split(os.pathsep) if item]
    guarded = next((path for path in existing_paths if (path / "sitecustomize.py").is_file()), None)
    startup_dir = root / "coverage-startup" if guarded is None else guarded
    startup_dir.mkdir(exist_ok=True)
    startup = startup_dir / "sitecustomize.py"
    with startup.open("a", encoding="utf-8") as stream:
        stream.write("\nimport coverage\ncoverage.process_startup()\n")
    rcfile = root / "subprocess-coveragerc"
    rcfile.write_text(
        "[run]\nbranch = True\nparallel = True\nsource = drawingmachine\n",
        encoding="utf-8",
    )
    purelib = Path(cast(str, sysconfig.get_paths()["purelib"]))
    search = [str(startup_dir), str(purelib)]
    search.extend(str(path) for path in existing_paths if path != startup_dir)
    environment["PYTHONPATH"] = os.pathsep.join(search)
    environment["COVERAGE_PROCESS_START"] = str(rcfile)
    environment["COVERAGE_FILE"] = str(root / "subprocess-coverage")
    environment["DRAWINGMACHINE_COVERAGE_SOURCE_ROOT"] = str(repository_root / "src/drawingmachine")
    _SUBPROCESS_COVERAGE_ROOTS.append(root)


@pytest.fixture(scope="session", autouse=True)
def _merge_package_b_subprocess_coverage(
    request: pytest.FixtureRequest,
    repository_root: Path,
) -> Iterator[None]:
    yield
    plugin = request.config.pluginmanager.getplugin("_cov")
    controller = None if plugin is None else getattr(plugin, "cov_controller", None)
    measured = None if controller is None else getattr(controller, "cov", None)
    if measured is None:
        return
    from coverage import CoverageData

    combined: dict[str, set[tuple[int, int]]] = {}
    for root in _SUBPROCESS_COVERAGE_ROOTS:
        for path in root.glob("subprocess-coverage.*"):
            child = CoverageData(basename=str(path))
            child.read()
            for filename in child.measured_files():
                normalized = filename.replace(os.sep, "/")
                marker = "/drawingmachine/"
                if marker not in normalized:
                    continue
                relative = normalized.rsplit(marker, 1)[1]
                canonical = str(repository_root / "src/drawingmachine" / relative)
                combined.setdefault(canonical, set()).update(child.arcs(filename) or ())
    if combined:
        measured.get_data().add_arcs({filename: sorted(arcs) for filename, arcs in combined.items()})


@pytest.fixture
def package_b_guarded_service(
    installed_drawingmachine: Path,
    tmp_path: Path,
    pytestconfig: pytest.Config,
    repository_root: Path,
    service_access_test_harness: ServiceAccessTestHarness,
) -> Iterator[PackageBService]:
    root = tmp_path / "guarded-xdg"
    runtime_root = Path(tempfile.mkdtemp(prefix="dm-b15-guard-", dir="/tmp"))
    environment = _package_b_environment(root, runtime_root=runtime_root)
    audit_log = _install_audit_guard(root, environment)
    client_environment = service_access_test_harness.install(environment)
    _install_subprocess_coverage(root, environment, pytestconfig, repository_root)
    service = PackageBService(
        installed_drawingmachine,
        environment,
        root,
        client_environment=client_environment,
        audit_log=audit_log,
    ).start()
    try:
        yield service
    finally:
        service.stop()
        shutil.rmtree(runtime_root, ignore_errors=True)


@pytest.fixture
def package_c_audit_guard(
    tmp_path: Path,
    pytestconfig: pytest.Config,
    repository_root: Path,
) -> PackageCAuditGuard:
    root = tmp_path / "package-c-audit"
    root.mkdir()
    environment = os.environ.copy()
    audit_log = _install_package_c_audit_guard(root, environment)
    _install_subprocess_coverage(root, environment, pytestconfig, repository_root)
    return PackageCAuditGuard(root=root, environment=environment, audit_log=audit_log)


@pytest.fixture
def valid_xdg_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> XdgPaths:
    roots = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }
    for name, value in roots.items():
        monkeypatch.setenv(name, value)
    paths = resolve_xdg_paths(roots, home=tmp_path / "home")
    (paths.config_dir / "machines").mkdir(parents=True)
    (paths.config_dir / "providers").mkdir(parents=True)
    paths.config_file.write_text(
        'schema_version = 1\nmachine_profile = "default"\nprovider_profile = "local-comfyui"\nlog_level = "INFO"\n',
        encoding="utf-8",
    )
    (paths.config_dir / "machines/default.toml").write_text(
        'schema_version = 1\n[profile]\nname = "default"\n'
        f"[profile.planning]\n{_PLANNING_TOML}"
        f"[profile.gcode]\n{_GCODE_TOML}"
        f"[profile.hardware]\n{_HARDWARE_TOML}",
        encoding="utf-8",
    )
    _write_local_comfyui_profile(paths.config_dir)
    return paths
