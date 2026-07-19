import json
import os
import tempfile
from pathlib import Path
from typing import cast

from drawingmachine.json_types import JsonObject


def read_json_object(path: Path) -> JsonObject:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return cast(JsonObject, payload)


def atomic_write_json(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
