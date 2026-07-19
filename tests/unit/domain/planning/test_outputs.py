from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from drawingmachine.domain.planning.models import PathPlanConfig
from drawingmachine.domain.planning.outputs import GeneratedArtifact, render_planning_artifacts
from drawingmachine.domain.planning.service import PlanningResult, build_path_plan
from drawingmachine.json_types import JsonObject

GOLDEN_ROOT = Path(__file__).resolve().parents[3] / "fixtures/package_b/golden"
ARTIFACT_PATHS = {
    "path_plan": "path_plan.json",
    "binary_png": "binary.png",
    "skeleton_debug_png": "skeleton_debug.png",
    "preview_stroke_only_svg": "preview_stroke_only.svg",
    "preview_centerline_only_svg": "preview_centerline_only.svg",
    "preview_layers_svg": "preview_layers.svg",
    "preview_final_svg": "preview_final.svg",
    "report_md": "report.md",
}
MEDIA_TYPES = {
    "path_plan": "application/json",
    "binary_png": "image/png",
    "skeleton_debug_png": "image/png",
    "preview_stroke_only_svg": "image/svg+xml",
    "preview_centerline_only_svg": "image/svg+xml",
    "preview_layers_svg": "image/svg+xml",
    "preview_final_svg": "image/svg+xml",
    "report_md": "text/markdown; charset=utf-8",
}


def _planning_result(fixture_name: str = "outline") -> PlanningResult:
    with Image.open(GOLDEN_ROOT / f"{fixture_name}.png") as image:
        return build_path_plan(image, source_name=f"{fixture_name}.png", config=PathPlanConfig())


def _artifact_map(result: PlanningResult) -> dict[str, GeneratedArtifact]:
    artifacts = render_planning_artifacts(result)
    assert len({artifact.role for artifact in artifacts}) == len(artifacts)
    return {artifact.role: artifact for artifact in artifacts}


def _read_json_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def test_generated_artifact_is_exactly_frozen_and_slotted() -> None:
    assert [(field.name, field.type) for field in fields(GeneratedArtifact)] == [
        ("role", "str"),
        ("relative_path", "str"),
        ("media_type", "str"),
        ("content", "bytes"),
    ]
    assert GeneratedArtifact.__slots__ == ("role", "relative_path", "media_type", "content")

    artifact = GeneratedArtifact("path_plan", "path_plan.json", "application/json", b"{}")
    with pytest.raises(FrozenInstanceError):
        artifact.role = "changed"


def test_render_planning_artifacts_emits_complete_stable_manifest() -> None:
    artifacts = render_planning_artifacts(_planning_result())

    assert tuple(artifact.role for artifact in artifacts) == tuple(ARTIFACT_PATHS)
    assert {artifact.role: artifact.relative_path for artifact in artifacts} == ARTIFACT_PATHS
    assert {artifact.role: artifact.media_type for artifact in artifacts} == MEDIA_TYPES
    assert all(not Path(artifact.relative_path).is_absolute() for artifact in artifacts)
    assert all(".." not in Path(artifact.relative_path).parts for artifact in artifacts)


def test_path_plan_bytes_match_golden_with_worker_relative_artifact_paths() -> None:
    artifacts = _artifact_map(_planning_result())
    actual = json.loads(artifacts["path_plan"].content)
    expected = _read_json_object(GOLDEN_ROOT / "expected/outline.path_plan.json")
    expected["input"] = "outline.png"
    expected["artifacts"] = ARTIFACT_PATHS

    assert actual == expected
    assert artifacts["path_plan"].content == json.dumps(
        expected,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def test_svg_and_report_bytes_match_frozen_stage1_digests() -> None:
    result = _planning_result("filled_region")
    artifacts = _artifact_map(result)
    expected = {
        "preview_stroke_only_svg": "dd309c5e3ff7f33ecbbb4e7c0c1e77db7933e4f278d9233548f4c5f7aa7fe880",
        "preview_centerline_only_svg": "bea82b1ea850db235e0f20cf1f3699eb611d3751e5188ca0dc75690e94b63a20",
        "preview_layers_svg": "176f38f1b18a84b39fa6d45bffe64738aa148319b7eb9bc90ca4740c194f2219",
        "preview_final_svg": "8502d0466e7172f6e3d56292f09cba2cdc6744a61ebcdf5ffa31f4250ec54e92",
        "report_md": "aa045684dd5731128764762fed0e8671bb3a0cacf43f83d4e5dffe669b748bc0",
    }
    assert {role: hashlib.sha256(artifacts[role].content).hexdigest() for role in expected} == expected
    assert all(artifacts[role].content.endswith(b"\n") for role in expected)


def test_binary_and_skeleton_png_pixels_match_planning_result() -> None:
    result = _planning_result("branch_loop")
    artifacts = _artifact_map(result)
    source_size = result.plan["metrics"]["source_size_px"]
    assert isinstance(source_size, list)
    width, height = cast(list[int], source_size)

    with Image.open(BytesIO(artifacts["binary_png"].content)) as binary:
        assert binary.mode == "L"
        assert binary.size == (width, height)
        assert frozenset((x, y) for y in range(height) for x in range(width) if binary.getpixel((x, y)) == 0) == (
            result.foreground
        )
    with Image.open(BytesIO(artifacts["skeleton_debug_png"].content)) as skeleton:
        assert skeleton.mode == "L"
        assert skeleton.size == (width, height)
        assert frozenset((x, y) for y in range(height) for x in range(width) if skeleton.getpixel((x, y)) == 0) == (
            result.skeleton
        )


def test_rendering_is_deterministic_and_does_not_write_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _planning_result()
    monkeypatch.chdir(tmp_path)

    first = render_planning_artifacts(result)
    second = render_planning_artifacts(result)

    assert first == second
    assert tuple(tmp_path.iterdir()) == ()
