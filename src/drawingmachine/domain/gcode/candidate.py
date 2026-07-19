from __future__ import annotations

from dataclasses import dataclass

from drawingmachine.domain.gcode.models import (
    MachineBuildProfile,
    PreviewResult,
    ReadinessResult,
    SendPlan,
    StaticCheckOptions,
    StaticCheckResult,
    validate_machine_build_profile,
)
from drawingmachine.domain.gcode.preview import render_preview
from drawingmachine.domain.gcode.readiness import evaluate_readiness
from drawingmachine.domain.gcode.send_plan import build_send_plan
from drawingmachine.domain.gcode.static_check import analyze_gcode


@dataclass(frozen=True, slots=True)
class CandidateCheckResult:
    static_result: StaticCheckResult
    send_plan: SendPlan
    readiness: ReadinessResult
    preview: PreviewResult


def check_candidate(
    gcode: str,
    *,
    profile: MachineBuildProfile,
    expected_gcode_sha256: str,
    input_name: str = "drawing.gcode",
) -> CandidateCheckResult:
    validate_machine_build_profile(profile)
    options = StaticCheckOptions(
        input_name=input_name,
        canvas_width_mm=profile.hardware_canvas_width_mm,
        canvas_height_mm=profile.hardware_canvas_height_mm,
        machine_width_mm=profile.machine_width_mm,
        machine_height_mm=profile.machine_height_mm,
        canvas_bounds_mode="span",
        pen_up_z=profile.pen_up_z,
        pen_down_z=profile.pen_down_z,
        start_x=profile.paper_center_x,
        start_y=profile.paper_center_y,
        start_z=profile.pen_up_z,
        z_tolerance=0.01,
        max_feed=profile.max_feed,
        max_pen_down_travel_mm=0.05,
        warn_pen_lifts=500,
        warn_short_path_mm=0.5,
    )
    static_result = analyze_gcode(gcode, options)
    send_plan = build_send_plan(gcode)
    readiness = evaluate_readiness(
        static_result=static_result,
        send_plan=send_plan,
        expected_gcode_sha256=expected_gcode_sha256,
    )
    preview = render_preview(gcode, profile)
    return CandidateCheckResult(static_result, send_plan, readiness, preview)


__all__ = ["CandidateCheckResult", "check_candidate"]
