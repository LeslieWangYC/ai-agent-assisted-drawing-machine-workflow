import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest

from drawingmachine.adapters.filesystem.hashing import sha256_file
from drawingmachine.adapters.filesystem.json_files import atomic_write_json, read_json_object
from drawingmachine.json_types import JsonObject


def test_atomic_write_replaces_without_fixed_tmp_name(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"value": 1})
    atomic_write_json(target, {"value": 2})
    assert read_json_object(target) == {"value": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_sha256_file_returns_digest_for_file_contents(tmp_path: Path) -> None:
    source = tmp_path / "drawing.gcode"
    source.write_bytes(b"G0 X0 Y0\\nG1 X10 Y10\\n")

    assert sha256_file(source) == hashlib.sha256(source.read_bytes()).hexdigest()


def test_read_json_object_rejects_non_object_root(tmp_path: Path) -> None:
    source = tmp_path / "items.json"
    source.write_text(json.dumps(["one", "two"]), encoding="utf-8")

    with pytest.raises(ValueError):
        read_json_object(source)


def test_atomic_write_creates_missing_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "state" / "current.json"

    atomic_write_json(target, {"job": {"id": "job-1"}})

    assert read_json_object(target) == {"job": {"id": "job-1"}}


def test_atomic_write_supports_direct_concurrent_writers(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    values = tuple(range(8))
    barrier = Barrier(len(values))

    def write_value(value: int) -> None:
        barrier.wait()
        atomic_write_json(target, {"value": value})

    with ThreadPoolExecutor(max_workers=len(values)) as executor:
        futures = [executor.submit(write_value, value) for value in values]
        for future in futures:
            future.result()

    assert read_json_object(target)["value"] in values
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def test_atomic_write_preserves_unicode_bytes(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    atomic_write_json(target, {"label": "\u7ed8\u56fe"})

    assert read_json_object(target) == {"label": "\u7ed8\u56fe"}
    assert "\u7ed8\u56fe".encode() in target.read_bytes()
    assert b"\\u7ed8" not in target.read_bytes()


def test_atomic_write_rejects_nan_without_replacing_target(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"value": "stable"})

    with pytest.raises(ValueError, match="Out of range float values"):
        atomic_write_json(target, {"value": float("nan")})

    assert read_json_object(target) == {"value": "stable"}
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def test_atomic_write_serialization_failure_cleans_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"value": "stable"})
    invalid = cast(JsonObject, {"value": object()})

    with pytest.raises(TypeError, match="not JSON serializable"):
        atomic_write_json(target, invalid)

    assert read_json_object(target) == {"value": "stable"}
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def test_atomic_write_replacement_failure_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import drawingmachine.adapters.filesystem.json_files as json_files

    target = tmp_path / "state.json"
    atomic_write_json(target, {"value": "stable"})

    def fail_replace(*args: object) -> None:
        del args
        raise OSError("forced replacement failure")

    monkeypatch.setattr(json_files.os, "replace", fail_replace)

    with pytest.raises(OSError, match="forced replacement failure"):
        atomic_write_json(target, {"value": "replacement"})

    assert read_json_object(target) == {"value": "stable"}
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
