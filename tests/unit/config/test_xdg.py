from pathlib import Path

import pytest

from drawingmachine.config import XdgPaths, resolve_xdg_paths
from drawingmachine.errors import DrawingMachineError, ErrorCategory


def test_xdg_paths_are_checkout_independent(tmp_path: Path) -> None:
    paths = resolve_xdg_paths(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path / "home",
    )

    assert paths.config_file == tmp_path / "config/drawingmachine/config.toml"
    assert paths.database_file == tmp_path / "state/drawingmachine/drawingmachine.db"
    assert paths.socket_file == tmp_path / "run/drawingmachine/service.sock"


def test_xdg_paths_expose_all_runtime_locations(tmp_path: Path) -> None:
    paths = resolve_xdg_paths(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path / "home",
    )

    assert paths.logs_dir == tmp_path / "state/drawingmachine/logs"
    assert paths.pid_file == tmp_path / "run/drawingmachine/service.pid"
    assert paths.jobs_dir == tmp_path / "data/drawingmachine/jobs"


def test_xdg_paths_use_home_defaults(tmp_path: Path) -> None:
    paths = resolve_xdg_paths(
        {"XDG_RUNTIME_DIR": str(tmp_path / "run")},
        home=tmp_path / "home",
    )

    assert paths.config_dir == tmp_path / "home/.config/drawingmachine"
    assert paths.state_dir == tmp_path / "home/.local/state/drawingmachine"
    assert paths.data_dir == tmp_path / "home/.local/share/drawingmachine"


def test_relative_non_runtime_xdg_roots_use_home_defaults(tmp_path: Path) -> None:
    paths = resolve_xdg_paths(
        {
            "XDG_CONFIG_HOME": "relative-config",
            "XDG_STATE_HOME": "relative-state",
            "XDG_DATA_HOME": "relative-data",
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path / "home",
    )

    assert paths.config_dir == tmp_path / "home/.config/drawingmachine"
    assert paths.state_dir == tmp_path / "home/.local/state/drawingmachine"
    assert paths.data_dir == tmp_path / "home/.local/share/drawingmachine"


@pytest.mark.parametrize("runtime", [None, "", "relative/run"])
def test_xdg_runtime_dir_must_be_present_and_absolute(tmp_path: Path, runtime: str | None) -> None:
    environment = {} if runtime is None else {"XDG_RUNTIME_DIR": runtime}

    with pytest.raises(DrawingMachineError, match="XDG_RUNTIME_DIR") as raised:
        resolve_xdg_paths(environment, home=tmp_path / "home")

    assert raised.value.payload.category is ErrorCategory.CONFIGURATION
    assert raised.value.payload.retryable is False


def test_xdg_paths_model_is_frozen(tmp_path: Path) -> None:
    paths = XdgPaths(tmp_path, tmp_path, tmp_path, tmp_path)

    with pytest.raises(AttributeError):
        paths.config_dir = tmp_path / "other"  # type: ignore[misc]
