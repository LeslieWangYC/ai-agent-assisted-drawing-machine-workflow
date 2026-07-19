from __future__ import annotations

import ast
import configparser
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPOSITORY_ROOT / "src/drawingmachine"


def _python_files(*package_names: str) -> tuple[Path, ...]:
    files: list[Path] = []
    for package_name in package_names:
        package = _PACKAGE_ROOT / package_name
        if package.exists():
            files.extend(package.rglob("*.py"))
    return tuple(sorted(files))


def _module_name(path: Path) -> str:
    relative = path.relative_to(_PACKAGE_ROOT).with_suffix("")
    parts = ("drawingmachine", *relative.parts)
    if path.name == "__init__.py":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_name = _module_name(path)
    package_parts = module_name.split(".") if path.name == "__init__.py" else module_name.split(".")[:-1]
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package_parts) - (node.level - 1)
                prefix = package_parts[:keep]
                imported = [*prefix, *(node.module or "").split(".")]
                base = ".".join(part for part in imported if part)
            else:
                base = node.module or ""
            if base:
                imports.append(base)
            imports.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return tuple(imports)


def _forbidden_imports(paths: Iterable[Path], forbidden: tuple[str, ...]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        for imported in _imported_modules(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(_REPOSITORY_ROOT)}:{imported}")
    return violations


def test_domain_and_ports_do_not_import_implementations() -> None:
    paths = _python_files("domain", "ports")
    assert paths, "the architecture gate must inspect at least the current ports package"
    forbidden = ("drawingmachine.cli", "drawingmachine.service", "drawingmachine.adapters")
    assert _forbidden_imports(paths, forbidden) == []


def test_provider_port_is_provider_neutral_and_does_not_import_planning_outputs() -> None:
    provider_port = _PACKAGE_ROOT / "ports/providers.py"
    assert provider_port.is_file()
    imports = _imported_modules(provider_port)
    forbidden = (
        "drawingmachine.adapters",
        "drawingmachine.domain.planning",
        "importlib.metadata",
        "pkg_resources",
    )
    assert all(not name.startswith(forbidden) for name in imports)


def test_provider_registry_has_no_discovery_mechanism() -> None:
    source = (_PACKAGE_ROOT / "ports/providers.py").read_text(encoding="utf-8")
    forbidden = ("entry_points", "iter_modules", "walk_packages", "rglob", "glob(")
    assert all(term not in source for term in forbidden)


def test_production_code_does_not_import_planning_experiments() -> None:
    experiments = _PACKAGE_ROOT / "domain/planning/experiments"
    paths = tuple(path for path in _PACKAGE_ROOT.rglob("*.py") if experiments not in path.parents)
    forbidden = ("drawingmachine.domain.planning.experiments",)

    assert _forbidden_imports(paths, forbidden) == []


def test_worker_task_registry_does_not_name_graph_clean() -> None:
    worker_root = _PACKAGE_ROOT / "adapters/workers"
    registry_files = tuple(worker_root.glob("*registry*.py"))
    paths = registry_files or tuple(worker_root.glob("*.py"))

    violations: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8").lower()
        if "graph_clean" in source or "graph clean" in source:
            violations.append(str(path.relative_to(_REPOSITORY_ROOT)))
    assert violations == []


def test_repository_transaction_port_does_not_expose_sqlite() -> None:
    repository_port = _PACKAGE_ROOT / "ports/repository.py"
    imports = _imported_modules(repository_port)
    source = repository_port.read_text(encoding="utf-8")

    assert all(not name.startswith("sqlite3") for name in imports)
    assert "sqlite3.Connection" not in source
    assert "execute(" not in source


def test_application_does_not_depend_on_entrypoints_or_adapters() -> None:
    paths = _python_files("application")
    forbidden = (
        "drawingmachine.cli",
        "drawingmachine.service",
        "drawingmachine.adapters",
        "drawingmachine.bootstrap",
    )
    assert _forbidden_imports(paths, forbidden) == []


def test_adapters_do_not_import_cli_or_service() -> None:
    paths = _python_files("adapters")
    assert paths, "the architecture gate must inspect the current adapters package"
    assert _forbidden_imports(paths, ("drawingmachine.cli", "drawingmachine.service")) == []


def test_fluidnc_port_and_fake_keep_the_closed_hardware_boundary() -> None:
    port = _PACKAGE_ROOT / "ports/fluidnc.py"
    fake = _PACKAGE_ROOT / "adapters/hardware/fake_fluidnc.py"
    assert port.is_file()
    assert fake.is_file()
    assert _forbidden_imports((port,), ("drawingmachine.adapters", "serial", "pyserial")) == []
    assert _forbidden_imports((fake,), ("serial", "pyserial")) == []
    port_source = port.read_text(encoding="utf-8").casefold()
    forbidden_definitions = ("def write", "def send_command", "def jog", "def unlock", "def reopen", "def resume")
    assert all(definition not in port_source for definition in forbidden_definitions)


def test_only_the_serial_fluidnc_adapter_imports_pyserial() -> None:
    serial_adapter = _PACKAGE_ROOT / "adapters/hardware/serial_fluidnc.py"
    importers = {
        path.relative_to(_PACKAGE_ROOT).as_posix()
        for path in _PACKAGE_ROOT.rglob("*.py")
        if path != serial_adapter and _forbidden_imports((path,), ("serial", "pyserial"))
    }
    assert importers == set()
    assert _forbidden_imports((serial_adapter,), ("serial",)) == [
        "src/drawingmachine/adapters/hardware/serial_fluidnc.py:serial"
    ]


def test_cli_and_service_do_not_bypass_concrete_boundaries() -> None:
    concrete = (
        "drawingmachine.adapters",
        "drawingmachine.persistence",
        "drawingmachine.providers",
        "drawingmachine.files",
        "drawingmachine.filesystem",
        "drawingmachine.workers",
        "drawingmachine.hardware",
        "drawingmachine.motion",
    )
    paths = _python_files("cli", "service")
    assert paths, "the architecture gate must inspect the current entrypoint packages"
    violations = _forbidden_imports(paths, concrete)
    violations.extend(_forbidden_imports(_python_files("service"), ("drawingmachine.bootstrap", "drawingmachine.cli")))

    cli_service_path = _PACKAGE_ROOT / "cli/service.py"
    for path in _python_files("cli"):
        for imported in _imported_modules(path):
            if imported.startswith(("drawingmachine.bootstrap", "drawingmachine.service")) and path != cli_service_path:
                violations.append(f"{path.relative_to(_REPOSITORY_ROOT)}:{imported}")
    assert violations == []


def test_bootstrap_is_the_only_external_runtime_adapter_assembler() -> None:
    concrete_runtime_adapters = (
        "drawingmachine.adapters.filesystem",
        "drawingmachine.adapters.hardware",
        "drawingmachine.adapters.persistence",
        "drawingmachine.adapters.system",
        "drawingmachine.adapters.workers",
    )
    bootstrap = _PACKAGE_ROOT / "bootstrap.py"
    assert any(name.startswith(concrete_runtime_adapters) for name in _imported_modules(bootstrap))

    candidates = tuple(
        path
        for path in _PACKAGE_ROOT.rglob("*.py")
        if path != bootstrap and "adapters" not in path.relative_to(_PACKAGE_ROOT).parts
    )
    assert _forbidden_imports(candidates, concrete_runtime_adapters) == []


def test_import_linter_declares_package_a_contracts() -> None:
    config_path = _REPOSITORY_ROOT / ".importlinter"
    assert config_path.is_file(), ".importlinter must define the Package A architecture contracts"

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    assert parser["importlinter"]["root_package"] == "drawingmachine"
    contracts = {
        parser[section]["name"]: parser[section]
        for section in parser.sections()
        if section.startswith("importlinter:contract:")
    }
    expected = {
        "Package dependency direction",
        "Ports do not depend on implementations",
        "Adapters do not depend on entrypoints",
        "Entry points do not bypass concrete adapters",
        "Concrete runtime adapters are assembled only by bootstrap",
        "Production code does not depend on planning experiments",
        "Provider ports do not depend on provider adapters",
    }
    assert expected <= contracts.keys()

    layers = {line.strip() for line in contracts["Package dependency direction"]["layers"].splitlines()}
    assert {"cli", "service", "(application)", "(adapters)", "(domain) | ports"} <= layers
    ignored_layer_imports = {
        line.strip()
        for line in contracts["Package dependency direction"]["ignore_imports"].splitlines()
        if line.strip()
    }
    assert ignored_layer_imports == {
        "drawingmachine.ports.repository -> drawingmachine.domain.jobs",
        "drawingmachine.ports.artifacts -> drawingmachine.domain.jobs",
        "drawingmachine.ports.machine_repository -> drawingmachine.domain.jobs",
        "drawingmachine.ports.machine_repository -> drawingmachine.domain.jobs.models",
        "drawingmachine.ports.machine_repository -> drawingmachine.domain.machine",
        "drawingmachine.ports.fluidnc -> drawingmachine.domain.fluidnc.gates",
        "drawingmachine.ports.fluidnc -> drawingmachine.domain.fluidnc.status",
        "drawingmachine.ports.fluidnc -> drawingmachine.domain.gcode.stream_program",
    }
    assert contracts["Ports do not depend on implementations"]["source_modules"].strip() == "drawingmachine.ports"
    assert contracts["Provider ports do not depend on provider adapters"]["source_modules"].strip() == (
        "drawingmachine.ports.providers"
    )
    assert contracts["Concrete runtime adapters are assembled only by bootstrap"]["type"] == "protected"
    protected_runtime_adapters = {
        line.strip()
        for line in contracts["Concrete runtime adapters are assembled only by bootstrap"][
            "protected_modules"
        ].splitlines()
        if line.strip()
    }
    assert protected_runtime_adapters == {
        "drawingmachine.adapters.filesystem",
        "drawingmachine.adapters.hardware",
        "drawingmachine.adapters.persistence",
        "drawingmachine.adapters.system",
        "drawingmachine.adapters.workers",
    }
    experiment_contract = contracts["Production code does not depend on planning experiments"]
    assert experiment_contract["type"] == "protected"
    assert experiment_contract["protected_modules"].strip() == ("drawingmachine.domain.planning.experiments")
    assert experiment_contract["allowed_importers"].strip() == ("drawingmachine.domain.planning.experiments")
    assert "source_modules" not in experiment_contract


def test_import_linter_contracts_are_kept() -> None:
    executable = Path(sys.executable).with_name("lint-imports")
    result = subprocess.run(
        [str(executable), "--no-cache"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ci_runs_complete_final_stage2_gate() -> None:
    workflow = (_REPOSITORY_ROOT / ".github/workflows/python-tests.yml").read_text(encoding="utf-8")
    commands = {line.strip().removeprefix("run: ") for line in workflow.splitlines()}
    expected = {
        "python -m build",
        "python -m pytest -q",
        "python -m pytest --cov=drawingmachine --cov-branch --cov-report=json:coverage.json --cov-fail-under=90",
        "python tools/check_branch_coverage.py coverage.json",
        "python -m mypy --strict src/drawingmachine",
        "python -m ruff check --select E4,E7,E9,F,I,UP,B,SIM,RUF src/drawingmachine tests tools",
        "python -m ruff format --check src/drawingmachine tests tools",
        "lint-imports",
        "git diff --check",
    }
    assert expected <= commands, f"missing final Stage 2 CI commands: {sorted(expected - commands)}"
    assert "python -m pip install -e '.[dev]' wheel setuptools" in commands
    assert "python -m pip install --no-index --no-deps dist/*.whl" in workflow
    assert "      - main" in workflow
