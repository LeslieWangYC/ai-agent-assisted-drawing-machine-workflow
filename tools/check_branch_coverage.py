from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

_MINIMUM_BRANCH_PERCENT = 90


def _minimum(value: str) -> int:
    try:
        minimum = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("minimum must be an integer from 0 to 100") from error
    if str(minimum) != value or not 0 <= minimum <= 100:
        raise argparse.ArgumentTypeError("minimum must be an integer from 0 to 100")
    return minimum


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _percentage(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("percent_branches_covered must be numeric")
    percentage = float(value)
    if not math.isfinite(percentage):
        raise ValueError("percent_branches_covered must be finite")
    return percentage


def _branch_totals(path: Path) -> tuple[int, int, float]:
    payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(payload, dict) or not isinstance(payload.get("totals"), dict):
        raise ValueError("coverage JSON must contain totals")
    totals = cast(dict[str, object], payload["totals"])
    covered = _integer(totals.get("covered_branches"), "covered_branches")
    total = _integer(totals.get("num_branches"), "num_branches")
    if total == 0 or covered > total:
        raise ValueError("branch totals are inconsistent")
    reported = _percentage(totals.get("percent_branches_covered"))
    calculated = covered * 100.0 / total
    if not math.isclose(reported, calculated, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("reported branch percentage does not match branch totals")
    return covered, total, reported


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce Package A branch coverage")
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("--minimum", type=_minimum, default=_MINIMUM_BRANCH_PERCENT)
    args = parser.parse_args(argv)
    try:
        covered, total, percentage = _branch_totals(args.coverage_json)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"Invalid coverage JSON: {error}", file=sys.stderr)
        return 2

    print(f"Branch coverage: {covered}/{total} = {percentage:.2f}% (required: {args.minimum:.2f}%)")
    return 0 if covered * 100 >= total * args.minimum else 1


if __name__ == "__main__":
    raise SystemExit(main())
