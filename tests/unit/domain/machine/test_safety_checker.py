from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_safety_checker_accepts_plan_positional_manifest_and_minimum(tmp_path: Path) -> None:
    module = "src/drawingmachine/domain/machine/models.py"
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(f"{module}\n", encoding="utf-8")
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps({"files": {module: {"summary": {"percent_branches_covered": 100.0}}}}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "tools/check_safety_module_coverage.py",
            str(coverage),
            str(manifest),
            "--minimum",
            "100",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
