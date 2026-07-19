from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from drawingmachine.domain.openclaw.manifest import load_packaged_openclaw_bundle


def _run_json(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *arguments: str,
    expected_returncode: int = 0,
) -> dict[str, Any]:
    result = subprocess.run(
        [str(executable), "--output", "json", *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert result.returncode == expected_returncode, result.stdout + result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def test_installed_service_explicitly_separates_simulated_principals_from_real_peer(
    package_b_guarded_service: Any,
    service_access_test_harness: Any,
    tmp_path: Path,
) -> None:
    service = package_b_guarded_service
    assert service.executable.parts[-6:] == (
        ".local",
        "lib",
        "drawingmachine",
        "venv",
        "bin",
        "drawingmachine",
    )

    automation = service_access_test_harness.client_environment(service.environment, "automation")
    allowed = _run_json(service.executable, automation, tmp_path, "service", "status")
    assert allowed["data"]["status"] == "RUNNING"

    operator = service_access_test_harness.client_environment(service.environment, "operator")
    denied = _run_json(
        service.executable,
        operator,
        tmp_path,
        "job",
        "status",
        "not-visible-to-operator",
        expected_returncode=1,
    )
    assert denied["error"]["code"] == "CLIENT_DATA_PERMISSION_DENIED"

    automation = service_access_test_harness.client_environment(service.environment, "automation")
    openclaw_denied = _run_json(
        service.executable,
        automation,
        tmp_path,
        "openclaw",
        "check",
        "--openclaw-root",
        str(tmp_path / "registry"),
        expected_returncode=1,
    )
    assert openclaw_denied["error"]["code"] == "OPENCLAW_OPERATOR_DENIED"

    operator = service_access_test_harness.client_environment(service.environment, "operator")
    openclaw_operator = _run_json(
        service.executable,
        operator,
        tmp_path,
        "openclaw",
        "check",
        "--openclaw-root",
        str(tmp_path / "registry"),
        expected_returncode=1,
    )
    assert openclaw_operator["error"]["code"] != "OPENCLAW_OPERATOR_DENIED"
    assert service.forbidden_events() == []

    evidence = {
        "simulated_dac_acl": {
            "service_uid": os.getuid(),
            "automation_uid": 1100,
            "operator_uid": 2200,
            "real_cross_uid": False,
        },
        "real_unix_peer": {
            "uid": os.getuid(),
            "gid": os.getgid(),
            "same_uid_only": True,
        },
    }
    assert evidence["simulated_dac_acl"]["real_cross_uid"] is False
    assert evidence["real_unix_peer"]["same_uid_only"] is True


def test_installed_operator_openclaw_check_install_check_and_exact_replay(
    package_b_guarded_service: Any,
    service_access_test_harness: Any,
    tmp_path: Path,
    request: Any,
) -> None:
    service = package_b_guarded_service
    config = Path(service.environment["XDG_CONFIG_HOME"]) / "drawingmachine"
    safe_root = Path(tempfile.mkdtemp(prefix="dm-d9-openclaw-", dir=Path.home()))
    request.addfinalizer(lambda: shutil.rmtree(safe_root, ignore_errors=True))
    registry_root = safe_root / "registry"
    managed_root = safe_root / "managed"
    registry_root.mkdir(parents=True)
    (managed_root / "agents/cnc-drawable-translator").mkdir(parents=True)
    (managed_root / "agents/cnc-drawable-router").mkdir()
    for directory in (managed_root, managed_root / "agents", *(managed_root / "agents").iterdir()):
        directory.chmod(0o2750)
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
    registry_path = registry_root / "openclaw.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    registry_path.chmod(0o440)
    policy_path = config / "openclaw-deployment.toml"
    policy_path.write_text(
        f'''schema_version = 1
registry_root = {json.dumps(str(registry_root))}
managed_root = {json.dumps(str(managed_root))}
managed_dir_mode = {0o2750}
target_mode = {0o640}
manifest_sha256 = "{bundle.manifest_sha256}"
registry_policy_sha256 = "{bundle.registry_policy_sha256}"

[operator_principal]
uid = 2200
gid = 2200
[automation_principal]
uid = 1100
gid = 1100
[automation_read_group]
name = "drawingmachine-openclaw-read"
gid = {os.getgid()}
[managed_owner]
uid = {os.getuid()}
gid = {os.getgid()}
[registry_owner]
uid = {os.getuid()}
gid = {os.getgid()}
''',
        encoding="utf-8",
    )
    policy_path.chmod(0o400)

    automation = service_access_test_harness.client_environment(service.environment, "automation")
    before_automation = tuple(sorted(path.relative_to(managed_root) for path in managed_root.rglob("*")))
    denied = _run_json(
        service.executable,
        automation,
        tmp_path,
        "openclaw",
        "check",
        "--openclaw-root",
        str(registry_root),
        expected_returncode=1,
    )
    assert denied["error"]["code"] == "OPENCLAW_OPERATOR_DENIED"
    assert tuple(sorted(path.relative_to(managed_root) for path in managed_root.rglob("*"))) == before_automation

    operator = service_access_test_harness.client_environment(service.environment, "operator")
    caller_data = tmp_path / "operator-caller-xdg-data"
    caller_data.mkdir()
    operator["XDG_DATA_HOME"] = str(caller_data)
    operator["DM_TEST_FIXED_OPENCLAW_REQUEST_ID"] = "12345678-1234-4234-8234-123456789abc"
    caller_before = tuple(caller_data.rglob("*"))
    checked = _run_json(
        service.executable,
        operator,
        tmp_path,
        "openclaw",
        "check",
        "--openclaw-root",
        str(registry_root),
        expected_returncode=1,
    )
    assert checked["error"]["code"] == "OPENCLAW_CHECK_NOT_IN_SYNC"
    assert not list(managed_root.rglob("*.md"))
    installed = _run_json(
        service.executable,
        operator,
        tmp_path,
        "openclaw",
        "install",
        "--openclaw-root",
        str(registry_root),
    )
    installed_tree = {path.relative_to(managed_root): path.read_bytes() for path in managed_root.rglob("*.md")}
    replayed = _run_json(
        service.executable,
        operator,
        tmp_path,
        "openclaw",
        "install",
        "--openclaw-root",
        str(registry_root),
    )
    final = _run_json(
        service.executable,
        operator,
        tmp_path,
        "openclaw",
        "check",
        "--openclaw-root",
        str(registry_root),
    )
    assert installed["data"]["installed"] is True
    assert replayed["data"]["deduplicated"] is True
    assert final["data"]["in_sync"] is True
    assert {str(path) for path in installed_tree} == {
        "agents/cnc-drawable-translator/AGENTS.md",
        "agents/cnc-drawable-translator/TOOLS.md",
        "agents/cnc-drawable-router/AGENTS.md",
        "agents/cnc-drawable-router/TOOLS.md",
    }
    assert {path.relative_to(managed_root): path.read_bytes() for path in managed_root.rglob("*.md")} == installed_tree
    assert all(path.stat().st_mode & 0o777 == 0o640 for path in managed_root.rglob("*.md"))
    assert tuple(caller_data.rglob("*")) == caller_before
    assert tuple(registry_root.iterdir()) == (registry_path,)
    assert service.forbidden_events() == []


def test_installed_client_helper_rebuilds_link_before_real_same_uid_peer_connect(
    installed_drawingmachine: Path,
    tmp_path: Path,
) -> None:
    script = r"""
import json, os, pathlib, socket, struct, threading
from drawingmachine.config.service_access import parse_service_access_config
from drawingmachine.install.client_endpoint import EndpointAclEntryV1, EndpointFactsV1, rebuild_client_endpoint

root = pathlib.Path(__import__("sys").argv[1])
root.mkdir()
runtime = root / "service-runtime"
client = root / "automation-runtime/drawingmachine"
runtime.mkdir(mode=0o710)
client.mkdir(parents=True, mode=0o700)
canonical = runtime / "service.sock"
endpoint = client / "service.sock"
listener = socket.socket(socket.AF_UNIX)
listener.bind(str(canonical))
listener.listen(1)
os.chmod(canonical, 0o660)
service_uid, service_gid = os.getuid() + 10000, os.getgid() + 10000
automation_uid, automation_gid = os.getuid(), os.getgid()
connect_gid = os.getgid() + 20000
config = parse_service_access_config(f'''schema_version = 1
canonical_runtime_dir = "{runtime}"
canonical_socket = "{canonical}"
automation_runtime_endpoint = "{endpoint}"
operator_runtime_endpoint = "{root / 'operator-runtime/drawingmachine/service.sock'}"
canonical_data_dir = "{root / 'data'}"
automation_import_root = "{root / 'data/imports/automation'}"
automation_export_root = "{root / 'data/exports/automation'}"
automation_import_endpoint = "{root / 'automation-data/imports'}"
automation_export_endpoint = "{root / 'automation-data/exports'}"
openclaw_policy_path = "{root / 'config/openclaw.toml'}"
[service_principal]
uid = {service_uid}
gid = {service_gid}
[automation_principal]
uid = {automation_uid}
gid = {automation_gid}
[operator_principal]
uid = {automation_uid + 1}
gid = {automation_gid + 1}
[connect_group]
name = "drawingmachine-connect"
gid = {connect_gid}
'''.encode())

def mode_acl(mode):
    return tuple(sorted((
        EndpointAclEntryV1("user_obj", None, mode >> 6 & 7),
        EndpointAclEntryV1("group_obj", None, mode >> 3 & 7),
        EndpointAclEntryV1("other", None, mode & 7),
    )))

class Facts:
    def inspect(self, path):
        value = os.lstat(path)
        if path == runtime:
            return EndpointFactsV1(path, "directory", value.st_dev, value.st_ino, service_uid, connect_gid, 0o710, mode_acl(0o710), ())
        if path == canonical:
            return EndpointFactsV1(path, "socket", value.st_dev, value.st_ino, service_uid, connect_gid, 0o660, mode_acl(0o660), ())
        if path == client:
            return EndpointFactsV1(path, "directory", value.st_dev, value.st_ino, automation_uid, automation_gid, 0o700, mode_acl(0o700), ())
        raise AssertionError(path)

rebuild_client_endpoint(config, role="automation", mode="runtime", current_uid=automation_uid, inspector=Facts())
accepted = {}
def receive():
    connection, _ = listener.accept()
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    accepted["peer"] = struct.unpack("3i", raw)
    connection.close()
thread = threading.Thread(target=receive)
thread.start()
connection = socket.socket(socket.AF_UNIX)
connection.connect(str(endpoint))
connection.close()
thread.join()
listener.close()
print(json.dumps({"peer": accepted["peer"], "endpoint_target": os.readlink(endpoint)}))
"""
    with tempfile.TemporaryDirectory(prefix="dm-d9-peer-", dir="/tmp") as root:
        result = subprocess.run(
            [str(installed_drawingmachine.with_name("python")), "-c", script, str(Path(root) / "peer")],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["peer"][1:] == [os.getuid(), os.getgid()]
    assert evidence["endpoint_target"].endswith("/service-runtime/service.sock")
