from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import fields
from pathlib import Path
from typing import cast

from PIL import Image

from drawingmachine.domain.planning.experiments.graph_clean import (
    GraphCleanConfig,
    build_graph_clean_stroke_records,
    status,
)
from drawingmachine.domain.planning.models import PathPlanConfig
from drawingmachine.domain.planning.service import build_path_plan

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_ROOT = REPOSITORY_ROOT / "src/drawingmachine"
GOLDEN_ROOT = REPOSITORY_ROOT / "tests/fixtures/package_b/golden"


def _imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(imports)


def test_graph_clean_matches_frozen_stage1_fixture_digest_and_is_experimental() -> None:
    config = PathPlanConfig()
    graph_config = GraphCleanConfig()
    with Image.open(GOLDEN_ROOT / "branch_loop.png") as image:
        result = build_path_plan(image, source_name="branch_loop.png", config=config)
    canvas = result.plan["canvas"]
    metrics = result.plan["metrics"]
    source_size = metrics["source_size_px"]
    assert isinstance(canvas, dict)
    assert isinstance(source_size, list)
    width, height = cast(list[int], source_size)

    actual = build_graph_clean_stroke_records(
        result.skeleton,
        width,
        height,
        cast(float, canvas["mm_per_px"]),
        cast(float, canvas["offset_x_mm"]),
        cast(float, canvas["offset_y_mm"]),
        config,
        graph_config,
    )
    assert status == "experimental_not_default"
    encoded = json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "61db6d98fc4ab5e29bf99040ba8bf9134c070994d1ee4e1c38ee9f59cdaec904"


def test_graph_clean_config_preserves_exact_three_fields_and_defaults() -> None:
    assert [(field.name, field.default) for field in fields(GraphCleanConfig)] == [
        ("spur_length_mm", 1.0),
        ("min_main_edge_length_mm", 2.0),
        ("through_angle_deg", 150.0),
    ]


def test_production_modules_do_not_import_graph_clean_experiments() -> None:
    experiment_root = PACKAGE_ROOT / "domain/planning/experiments"
    candidates = (path for path in PACKAGE_ROOT.rglob("*.py") if experiment_root not in path.parents)

    assert [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in candidates
        if any(
            imported.startswith("drawingmachine.domain.planning.experiments") for imported in _imported_modules(path)
        )
    ] == []


def test_worker_task_registry_neither_imports_nor_names_graph_clean() -> None:
    worker_root = PACKAGE_ROOT / "adapters/workers"
    registry_files = tuple(worker_root.glob("*registry*.py"))
    candidates = registry_files or tuple(worker_root.glob("*.py"))

    for path in candidates:
        source = path.read_text(encoding="utf-8").lower()
        assert "graph_clean" not in source
        assert "graph clean" not in source
        assert all(
            not imported.startswith("drawingmachine.domain.planning.experiments")
            for imported in _imported_modules(path)
        )
