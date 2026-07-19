from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CHECKER = _REPOSITORY_ROOT / "tools/check_branch_coverage.py"


def _run_checker(coverage_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_CHECKER), str(coverage_path), *extra],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_coverage(path: Path, covered: object, total: object, percentage: object) -> None:
    path.write_text(
        json.dumps(
            {
                "totals": {
                    "covered_branches": covered,
                    "num_branches": total,
                    "percent_branches_covered": percentage,
                }
            }
        ),
        encoding="utf-8",
    )


def test_branch_checker_enforces_ninety_percent_from_coverage_json(tmp_path: Path) -> None:
    passing = tmp_path / "passing.json"
    _write_coverage(passing, 9, 10, 90.0)
    failing = tmp_path / "failing.json"
    _write_coverage(failing, 8, 10, 80.0)

    passed = _run_checker(passing)
    failed = _run_checker(failing)

    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert passed.stdout == "Branch coverage: 9/10 = 90.00% (required: 90.00%)\n"
    assert passed.stderr == ""
    assert failed.returncode == 1
    assert failed.stdout == "Branch coverage: 8/10 = 80.00% (required: 90.00%)\n"
    assert failed.stderr == ""


def test_branch_checker_rejects_exact_ratio_below_threshold_when_float_rounds_to_ninety(tmp_path: Path) -> None:
    coverage = tmp_path / "rounded.json"
    covered = 899_999_999_999_999_999
    total = 1_000_000_000_000_000_000
    assert covered * 100 < total * 90
    assert covered * 100.0 / total == 90.0
    _write_coverage(coverage, covered, total, 90.0)

    result = _run_checker(coverage)

    assert result.returncode == 1
    assert result.stdout == ("Branch coverage: 899999999999999999/1000000000000000000 = 90.00% (required: 90.00%)\n")
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("covered", "total", "percentage"),
    [
        (0, 0, 0.0),
        (11, 10, 110.0),
        (-1, 10, -10.0),
        (True, 10, 10.0),
        (9, "10", 90.0),
    ],
    ids=["zero-total", "covered-over-total", "negative-covered", "boolean-covered", "non-integer-total"],
)
def test_branch_checker_rejects_zero_or_invalid_totals(
    tmp_path: Path,
    covered: object,
    total: object,
    percentage: object,
) -> None:
    coverage = tmp_path / "invalid.json"
    _write_coverage(coverage, covered, total, percentage)

    result = _run_checker(coverage)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("Invalid coverage JSON:")


def test_branch_checker_accepts_current_valid_coverage_ratio(tmp_path: Path) -> None:
    coverage = tmp_path / "current.json"
    _write_coverage(coverage, 286, 316, 90.50632911392405)

    result = _run_checker(coverage)

    assert result.returncode == 0
    assert result.stdout == "Branch coverage: 286/316 = 90.51% (required: 90.00%)\n"
    assert result.stderr == ""


def test_branch_checker_accepts_explicit_one_hundred_percent(tmp_path: Path) -> None:
    coverage = tmp_path / "complete.json"
    _write_coverage(coverage, 17, 17, 100.0)

    result = _run_checker(coverage, "--minimum", "100")

    assert result.returncode == 0
    assert result.stdout == "Branch coverage: 17/17 = 100.00% (required: 100.00%)\n"
    assert result.stderr == ""


def test_branch_checker_uses_exact_integer_cross_multiplication_for_explicit_minimum(tmp_path: Path) -> None:
    coverage = tmp_path / "rounded.json"
    covered = 999_999_999_999_999_999
    total = 1_000_000_000_000_000_000
    _write_coverage(coverage, covered, total, 100.0)

    result = _run_checker(coverage, "--minimum", "100")

    assert result.returncode == 1


@pytest.mark.parametrize("minimum", ["-1", "101", "1.5", "true"])
def test_branch_checker_rejects_invalid_minimum(minimum: str, tmp_path: Path) -> None:
    coverage = tmp_path / "valid.json"
    _write_coverage(coverage, 9, 10, 90.0)

    result = _run_checker(coverage, "--minimum", minimum)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "minimum" in result.stderr.lower()
