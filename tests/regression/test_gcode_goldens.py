from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from drawingmachine.domain.gcode.build import build_gcode
from drawingmachine.domain.gcode.models import MachineBuildProfile, StaticCheckOptions
from drawingmachine.domain.gcode.preview import render_preview
from drawingmachine.domain.gcode.readiness import evaluate_readiness
from drawingmachine.domain.gcode.send_plan import build_send_plan
from drawingmachine.domain.gcode.static_check import analyze_gcode
from drawingmachine.json_types import JsonObject

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "fixtures/package_b/golden"


def read_json_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def test_gcode_build_matches_stage1_text_selected_plan_and_report_exactly() -> None:
    plan = read_json_object(GOLDEN_ROOT / "expected/filled_region.path_plan.json")
    result = build_gcode(plan, MachineBuildProfile.stage1_defaults())

    assert result.gcode == (GOLDEN_ROOT / "expected/drawing.gcode").read_text(encoding="utf-8")
    assert result.selected_plan == read_json_object(GOLDEN_ROOT / "expected/selected_path_plan.json")
    assert result.report.startswith("# G-code Candidate Report\n")
    assert "- Path mode: `stroke`" in result.report
    assert "- This file includes pen-down Z moves" in result.report
    notes = result.report.split("## Notes\n\n", 1)[1].splitlines()
    assert notes[:4] == [
        "- This report is static analysis only.",
        "- Do not send the generated file to a controller without separate machine validation.",
        "- This file includes pen-down Z moves and should be treated as a pen-contact drawing candidate.",
        "- `preview` mode may retrace parts of the skeleton because it prioritizes a visually continuous centerline base.",
    ]


def test_preview_svg_and_report_match_stage1_exactly() -> None:
    gcode = (GOLDEN_ROOT / "expected/drawing.gcode").read_text(encoding="utf-8")
    result = render_preview(gcode, MachineBuildProfile.stage1_defaults())
    expected_report = read_json_object(GOLDEN_ROOT / "expected/gcode_preview.json")
    expected_report["input"] = "drawing.gcode"
    expected_report["svg"] = "gcode_preview.svg"

    assert result.svg == (GOLDEN_ROOT / "expected/gcode_preview.svg").read_text(encoding="utf-8")
    assert result.report == expected_report


def test_static_json_matches_stage1_exactly_after_input_path_normalization() -> None:
    gcode = (GOLDEN_ROOT / "expected/drawing.gcode").read_text(encoding="utf-8")
    options = StaticCheckOptions.stage1_defaults(input_name="<FIXTURE_ROOT>/expected/drawing.gcode")
    result = analyze_gcode(gcode, options)

    assert result.to_json() == read_json_object(GOLDEN_ROOT / "expected/gcode_static.json")


def test_send_plan_matches_stage1_exactly() -> None:
    gcode = (GOLDEN_ROOT / "expected/drawing.gcode").read_text(encoding="utf-8")

    assert build_send_plan(gcode).to_json() == read_json_object(GOLDEN_ROOT / "expected/send_plan.json")


def test_readiness_matches_stage1_exactly_for_matching_digest() -> None:
    gcode = (GOLDEN_ROOT / "expected/drawing.gcode").read_text(encoding="utf-8")
    static_result = analyze_gcode(gcode, StaticCheckOptions.stage1_defaults(input_name="drawing.gcode"))
    send_plan = build_send_plan(gcode)
    result = evaluate_readiness(
        static_result=static_result,
        send_plan=send_plan,
        expected_gcode_sha256=hashlib.sha256(gcode.encode()).hexdigest(),
    )

    assert result.to_json() == read_json_object(GOLDEN_ROOT / "expected/readiness.json")
