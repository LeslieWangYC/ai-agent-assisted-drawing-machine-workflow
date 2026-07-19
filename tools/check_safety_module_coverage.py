from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

_DEFAULT_MINIMUM = 100


def _minimum(value: str) -> int:
    try:
        minimum = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("minimum must be an integer from 0 to 100") from error
    if str(minimum) != value or not 0 <= minimum <= 100:
        raise argparse.ArgumentTypeError("minimum must be an integer from 0 to 100")
    return minimum


def _manifest(path: Path) -> tuple[str, ...]:
    entries = tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not entries or len(entries) != len(set(entries)):
        raise ValueError("safety manifest must contain unique module paths")
    for entry in entries:
        if not entry.startswith("src/drawingmachine/") or not entry.endswith(".py"):
            raise ValueError(f"invalid safety module path: {entry}")
    return entries


def _percentage(summary: object, module: str) -> float:
    if not isinstance(summary, Mapping):
        raise ValueError(f"coverage summary missing for {module}")
    value = cast(Mapping[str, object], summary).get("percent_branches_covered")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"branch percentage missing for {module}")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 100.0:
        raise ValueError(f"invalid branch percentage for {module}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce per-module Package C safety coverage")
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("manifest", nargs="?", type=Path)
    parser.add_argument(
        "--manifest",
        dest="manifest_option",
        type=Path,
    )
    parser.add_argument("--minimum", type=_minimum, default=_DEFAULT_MINIMUM)
    args = parser.parse_args(argv)
    try:
        if args.manifest is not None and args.manifest_option is not None:
            raise ValueError("manifest must be supplied either positionally or with --manifest")
        manifest = args.manifest or args.manifest_option or Path("tests/architecture/package_c_safety_modules.txt")
        payload = cast(object, json.loads(args.coverage_json.read_text(encoding="utf-8")))
        if not isinstance(payload, Mapping) or not isinstance(payload.get("files"), Mapping):
            raise ValueError("coverage JSON must contain a files mapping")
        files = cast(Mapping[str, object], payload["files"])
        failures: list[str] = []
        for module in _manifest(manifest):
            record = files.get(module)
            if not isinstance(record, Mapping):
                failures.append(f"{module}: absent from coverage JSON")
                continue
            percentage = _percentage(record.get("summary"), module)
            print(f"{module}: {percentage:.2f}% branch coverage")
            if percentage + 1e-9 < args.minimum:
                failures.append(f"{module}: requires {args.minimum}% branch coverage")
        if failures:
            for failure in failures:
                print(failure, file=sys.stderr)
            return 1
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"Invalid safety coverage input: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
