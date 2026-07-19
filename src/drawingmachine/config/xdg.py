import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from drawingmachine.errors import DrawingMachineError, ErrorCategory, ErrorPayload

_APPLICATION_DIRECTORY = "drawingmachine"


@dataclass(frozen=True, slots=True)
class XdgPaths:
    config_dir: Path
    state_dir: Path
    data_dir: Path
    runtime_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def service_access_file(self) -> Path:
        return self.config_dir / "service-access.toml"

    @property
    def database_file(self) -> Path:
        return self.state_dir / "drawingmachine.db"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def socket_file(self) -> Path:
        return self.runtime_dir / "service.sock"

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "service.pid"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def resolve_xdg_paths(
    environment: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> XdgPaths:
    env = os.environ if environment is None else environment
    home_dir = Path.home() if home is None else home
    runtime_value = env.get("XDG_RUNTIME_DIR")
    if not runtime_value or not Path(runtime_value).is_absolute():
        raise DrawingMachineError(
            ErrorPayload(
                code="CONFIG_INVALID",
                category=ErrorCategory.CONFIGURATION,
                message="XDG_RUNTIME_DIR must be present and absolute",
                retryable=False,
                details={"field": "XDG_RUNTIME_DIR"},
            )
        )

    config_root = _xdg_root(env, "XDG_CONFIG_HOME", home_dir / ".config")
    state_root = _xdg_root(env, "XDG_STATE_HOME", home_dir / ".local/state")
    data_root = _xdg_root(env, "XDG_DATA_HOME", home_dir / ".local/share")
    runtime_root = Path(runtime_value)
    return XdgPaths(
        config_dir=config_root / _APPLICATION_DIRECTORY,
        state_dir=state_root / _APPLICATION_DIRECTORY,
        data_dir=data_root / _APPLICATION_DIRECTORY,
        runtime_dir=runtime_root / _APPLICATION_DIRECTORY,
    )


def _xdg_root(environment: Mapping[str, str], variable: str, default: Path) -> Path:
    value = environment.get(variable)
    if value and Path(value).is_absolute():
        return Path(value)
    return default
