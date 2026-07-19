import hashlib
from pathlib import Path
from typing import BinaryIO

import pytest

from drawingmachine.config import LogLevel, XdgPaths, load_config
from drawingmachine.errors import DrawingMachineError, ErrorCategory


def test_load_config_returns_validated_bundle_and_digests(valid_xdg_paths: XdgPaths) -> None:
    bundle = load_config(valid_xdg_paths)

    assert bundle.application.schema_version == 1
    assert bundle.application.machine_profile == "default"
    assert bundle.application.provider_profile == "local-comfyui"
    assert bundle.application.log_level is LogLevel.INFO
    assert bundle.machine.schema_version == 1
    assert bundle.machine.profile["name"] == "default"
    planning = bundle.machine.profile["planning"]
    gcode = bundle.machine.profile["gcode"]
    assert isinstance(planning, dict)
    assert isinstance(gcode, dict)
    assert planning["threshold"] == 128
    assert gcode["safe_start"] is True
    assert bundle.provider.profile["name"] == "local-comfyui"
    assert bundle.provider.profile["workflow_template"] == "workflow.json"
    assert bundle.machine_path == valid_xdg_paths.config_dir / "machines/default.toml"
    assert bundle.provider_path == valid_xdg_paths.config_dir / "providers/local-comfyui.toml"
    assert bundle.digests == {
        "application": hashlib.sha256(valid_xdg_paths.config_file.read_bytes()).hexdigest(),
        "machine": hashlib.sha256(bundle.machine_path.read_bytes()).hexdigest(),
        "provider": hashlib.sha256(bundle.provider_path.read_bytes()).hexdigest(),
    }


def test_digests_are_bound_to_the_exact_toml_bytes_that_were_parsed(
    valid_xdg_paths: XdgPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.config.loader as loader_module

    machine_path = valid_xdg_paths.config_dir / "machines/default.toml"
    paths = (valid_xdg_paths.config_file, machine_path, valid_xdg_paths.config_dir / "providers/local-comfyui.toml")
    original_bytes = {path: path.read_bytes() for path in paths}
    replacement = b'schema_version = 1\n[profile]\nname = "replacement"\n'
    read_counts = {path: 0 for path in paths}
    real_read_bytes = Path.read_bytes
    real_toml_load = loader_module.tomllib.load

    def replacing_read_bytes(path: Path) -> bytes:
        contents = real_read_bytes(path)
        if path in read_counts:
            read_counts[path] += 1
        if path == machine_path:
            machine_path.write_bytes(replacement)
        return contents

    def replace_after_stream_parse(handle: BinaryIO) -> dict[str, object]:
        parsed = real_toml_load(handle)
        if Path(handle.name) == machine_path:
            machine_path.write_bytes(replacement)
        return parsed

    monkeypatch.setattr(Path, "read_bytes", replacing_read_bytes)
    monkeypatch.setattr(loader_module.tomllib, "load", replace_after_stream_parse)

    bundle = load_config(valid_xdg_paths)

    assert bundle.machine.profile["name"] == "default"
    planning = bundle.machine.profile["planning"]
    assert isinstance(planning, dict)
    assert planning["canvas_width_mm"] == 120.0
    assert bundle.digests["machine"] == hashlib.sha256(original_bytes[machine_path]).hexdigest()
    assert read_counts == {path: 1 for path in paths}
    assert real_read_bytes(machine_path) == replacement


def test_unknown_application_key_is_rejected(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.config_file.write_text(
        valid_xdg_paths.config_file.read_text(encoding="utf-8") + "surprise = true\n",
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="unknown keys"):
        load_config(valid_xdg_paths)


def test_missing_application_key_is_rejected(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.config_file.write_text(
        'schema_version = 1\nmachine_profile = "default"\nlog_level = "INFO"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="missing keys"):
        load_config(valid_xdg_paths)


@pytest.mark.parametrize("schema_version", ["true", '"1"', "1.0"])
def test_application_schema_version_requires_an_integer(
    valid_xdg_paths: XdgPaths,
    schema_version: str,
) -> None:
    valid_xdg_paths.config_file.write_text(
        f"schema_version = {schema_version}\n"
        'machine_profile = "default"\nprovider_profile = "local-comfyui"\nlog_level = "INFO"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="schema_version must be an integer"):
        load_config(valid_xdg_paths)


def test_unsupported_application_schema_version_is_rejected(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.config_file.write_text(
        'schema_version = 2\nmachine_profile = "default"\nprovider_profile = "local-comfyui"\nlog_level = "INFO"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="unsupported schema_version"):
        load_config(valid_xdg_paths)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("machine_profile", "42"),
        ("provider_profile", "false"),
        ("log_level", "9"),
    ],
)
def test_application_string_fields_reject_other_types(
    valid_xdg_paths: XdgPaths,
    key: str,
    value: str,
) -> None:
    values = {
        "machine_profile": '"default"',
        "provider_profile": '"local-comfyui"',
        "log_level": '"INFO"',
    }
    values[key] = value
    valid_xdg_paths.config_file.write_text(
        "schema_version = 1\n"
        f"machine_profile = {values['machine_profile']}\n"
        f"provider_profile = {values['provider_profile']}\n"
        f"log_level = {values['log_level']}\n",
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match=f"{key} must be a string"):
        load_config(valid_xdg_paths)


def test_unknown_log_level_is_rejected(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.config_file.write_text(
        'schema_version = 1\nmachine_profile = "default"\nprovider_profile = "local-comfyui"\nlog_level = "VERBOSE"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="unsupported log_level"):
        load_config(valid_xdg_paths)


@pytest.mark.parametrize(
    ("field", "profile_name"),
    [
        ("machine_profile", "../outside"),
        ("machine_profile", "nested/default"),
        ("provider_profile", "/absolute/profile"),
    ],
)
def test_profile_paths_cannot_escape_profile_directories(
    valid_xdg_paths: XdgPaths,
    field: str,
    profile_name: str,
) -> None:
    values = {"machine_profile": "default", "provider_profile": "local-comfyui"}
    values[field] = profile_name
    valid_xdg_paths.config_file.write_text(
        "schema_version = 1\n"
        f'machine_profile = "{values["machine_profile"]}"\n'
        f'provider_profile = "{values["provider_profile"]}"\n'
        'log_level = "INFO"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="invalid profile name"):
        load_config(valid_xdg_paths)


def test_profile_symlink_cannot_escape_profile_directory(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.toml"
    outside.write_text('schema_version = 1\n[profile]\nname = "outside"\n', encoding="utf-8")
    machine_path = valid_xdg_paths.config_dir / "machines/default.toml"
    machine_path.unlink()
    machine_path.symlink_to(outside)

    with pytest.raises(DrawingMachineError, match="escapes machines directory"):
        load_config(valid_xdg_paths)


def test_symlinked_machines_directory_cannot_escape_config_tree(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    _replace_profile_directory_with_outside_symlink(valid_xdg_paths, tmp_path, "machines", "default")

    with pytest.raises(DrawingMachineError, match="escapes machines directory"):
        load_config(valid_xdg_paths)


def test_symlinked_providers_directory_cannot_escape_config_tree(
    valid_xdg_paths: XdgPaths,
    tmp_path: Path,
) -> None:
    _replace_profile_directory_with_outside_symlink(valid_xdg_paths, tmp_path, "providers", "local-comfyui")

    with pytest.raises(DrawingMachineError, match="escapes providers directory"):
        load_config(valid_xdg_paths)


def _replace_profile_directory_with_outside_symlink(
    paths: XdgPaths,
    tmp_path: Path,
    directory: str,
    profile_name: str,
) -> None:
    profile_dir = paths.config_dir / directory
    (profile_dir / f"{profile_name}.toml").unlink()
    (profile_dir / "workflow.json").unlink(missing_ok=True)
    profile_dir.rmdir()
    outside_dir = tmp_path / f"outside-{directory}"
    outside_dir.mkdir()
    (outside_dir / f"{profile_name}.toml").write_text(
        'schema_version = 1\n[profile]\nname = "outside"\n',
        encoding="utf-8",
    )
    profile_dir.symlink_to(outside_dir, target_is_directory=True)


@pytest.mark.parametrize("profile_kind", ["machines", "providers"])
def test_profile_envelope_rejects_unknown_keys(
    valid_xdg_paths: XdgPaths,
    profile_kind: str,
) -> None:
    profile_name = "default" if profile_kind == "machines" else "local-comfyui"
    profile_path = valid_xdg_paths.config_dir / profile_kind / f"{profile_name}.toml"
    profile_path.write_text(
        'schema_version = 1\nsurprise = true\n[profile]\nname = "profile"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="unknown keys"):
        load_config(valid_xdg_paths)


@pytest.mark.parametrize("profile_kind", ["machines", "providers"])
def test_profile_envelope_schema_version_rejects_booleans(
    valid_xdg_paths: XdgPaths,
    profile_kind: str,
) -> None:
    profile_name = "default" if profile_kind == "machines" else "local-comfyui"
    profile_path = valid_xdg_paths.config_dir / profile_kind / f"{profile_name}.toml"
    profile_path.write_text(
        'schema_version = true\n[profile]\nname = "profile"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="schema_version must be an integer"):
        load_config(valid_xdg_paths)


def test_profile_envelope_requires_profile_table(valid_xdg_paths: XdgPaths) -> None:
    (valid_xdg_paths.config_dir / "machines/default.toml").write_text(
        'schema_version = 1\nprofile = "default"\n',
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="profile must be a table"):
        load_config(valid_xdg_paths)


def test_profile_envelope_rejects_non_json_values(valid_xdg_paths: XdgPaths) -> None:
    (valid_xdg_paths.config_dir / "machines/default.toml").write_text(
        "schema_version = 1\n[profile]\ncreated_at = 2026-07-10T12:00:00Z\n",
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="JSON-compatible"):
        load_config(valid_xdg_paths)


@pytest.mark.parametrize("value", ["nan", "+inf", "-inf"])
def test_profile_envelope_rejects_non_finite_floats(
    valid_xdg_paths: XdgPaths,
    value: str,
) -> None:
    (valid_xdg_paths.config_dir / "machines/default.toml").write_text(
        f"schema_version = 1\n[profile]\ntolerance = {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(DrawingMachineError, match="finite"):
        load_config(valid_xdg_paths)


def test_profile_envelope_preserves_finite_floats(valid_xdg_paths: XdgPaths) -> None:
    (valid_xdg_paths.config_dir / "machines/default.toml").write_text(
        "schema_version = 1\n[profile]\ntolerance = 0.125\n",
        encoding="utf-8",
    )

    assert load_config(valid_xdg_paths).machine.profile["tolerance"] == 0.125


def test_profile_envelope_preserves_nested_json_lists_and_tables(valid_xdg_paths: XdgPaths) -> None:
    (valid_xdg_paths.config_dir / "machines/default.toml").write_text(
        'schema_version = 1\n[profile]\nvalues = [1, { name = "nested" }]\nsettings = { enabled = true }\n',
        encoding="utf-8",
    )

    assert load_config(valid_xdg_paths).machine.profile == {
        "values": [1, {"name": "nested"}],
        "settings": {"enabled": True},
    }


def test_profile_path_must_resolve_to_a_file(valid_xdg_paths: XdgPaths) -> None:
    machine_path = valid_xdg_paths.config_dir / "machines/default.toml"
    machine_path.unlink()
    machine_path.mkdir()

    with pytest.raises(DrawingMachineError, match="profile path is not a file"):
        load_config(valid_xdg_paths)


def test_invalid_toml_is_reported_as_configuration_error(valid_xdg_paths: XdgPaths) -> None:
    valid_xdg_paths.config_file.write_text("schema_version = [\n", encoding="utf-8")

    with pytest.raises(DrawingMachineError, match="could not load TOML") as raised:
        load_config(valid_xdg_paths)

    assert raised.value.payload.category is ErrorCategory.CONFIGURATION
    assert raised.value.payload.retryable is False


def test_missing_profile_file_is_reported_as_configuration_error(valid_xdg_paths: XdgPaths) -> None:
    (valid_xdg_paths.config_dir / "machines/default.toml").unlink()

    with pytest.raises(DrawingMachineError, match="profile file does not exist") as raised:
        load_config(valid_xdg_paths)

    assert raised.value.payload.category is ErrorCategory.CONFIGURATION
