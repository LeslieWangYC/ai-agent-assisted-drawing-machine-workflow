from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from drawingmachine.domain.fluidnc.gates import (
    GateDisposition,
    SessionObservationPhase,
    classify_session_observation,
    evaluate_post_home,
    evaluate_preflight,
)
from drawingmachine.domain.fluidnc.responses import ResponseKind, classify_command_response
from drawingmachine.domain.fluidnc.status import parse_preflight_snapshot

GOLDEN_ROOT = Path("tests/fixtures/package_c/golden")
FROZEN_MANIFEST_SHA256 = "2dd9ae1480a146eb4a292b86ddf060450f2f1d4ad468364beb7f947187fcdd47"


def test_hardware_golden_manifest_matches_every_tracked_fixture() -> None:
    manifest = json.loads((GOLDEN_ROOT / "manifest.json").read_text(encoding="utf-8"))
    actual = {
        str(path.relative_to(GOLDEN_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(GOLDEN_ROOT.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    assert manifest == {"schema_version": 1, "files": actual}


def test_package_c_golden_manifest_is_byte_immutable() -> None:
    assert hashlib.sha256((GOLDEN_ROOT / "manifest.json").read_bytes()).hexdigest() == FROZEN_MANIFEST_SHA256


def test_corpus_records_legacy_deviations_without_approving_them_for_package_c() -> None:
    preflight = json.loads((GOLDEN_ROOT / "preflight.json").read_text(encoding="utf-8"))
    deviations = json.loads((GOLDEN_ROOT / "deviations.json").read_text(encoding="utf-8"))

    assert preflight["missing_g54_legacy_gate"]["allow_stream"] is True
    assert preflight["post_home_without_mpos_legacy_gate"]["allow_stream"] is True
    assert deviations == {
        "missing_g54": {"legacy": "warning", "package_c": "blocker"},
        "post_home_mpos": {"legacy": "not_proven", "package_c": "required"},
    }


def test_corpus_captures_startup_homed_and_calibrated_pose_gate_evidence() -> None:
    preflight = json.loads((GOLDEN_ROOT / "preflight.json").read_text(encoding="utf-8"))

    assert preflight["startup_homed_legacy_gate"] == {"allow_stream": True, "blockers": [], "warnings": []}
    assert preflight["startup_unhomed_legacy_gate"]["allow_stream"] is False
    assert preflight["startup_missing_mpos_legacy_gate"]["allow_stream"] is False
    assert preflight["calibrated_work_pose_evidence"] == {"MPos": [192.0, 192.0, 192.0], "WPos": [96.0, 96.0, 3.5]}


def test_corpus_captures_line_ack_alarm_error_and_timeout_stop_behavior() -> None:
    stream = json.loads((GOLDEN_ROOT / "stream.json").read_text(encoding="utf-8"))

    assert stream["line_ack_transcript"][-1]["classification"] == "error"
    assert stream["alarm_stop_transcript"][-1]["classification"] == "alarm"
    assert stream["timeout_stop_transcript"][-1]["classification"] == "timeout"
    assert stream["alarm_stop_transcript"][-1]["stops_before_next_command"] is True
    assert stream["timeout_stop_transcript"][-1]["stops_before_next_command"] is True


@pytest.mark.parametrize("fixture_name", ("responses.json", "reports.json", "zcal.json", "stream.json", "reset.json"))
def test_hardware_corpus_has_each_required_fixed_evidence_family(fixture_name: str) -> None:
    payload = json.loads((GOLDEN_ROOT / fixture_name).read_text(encoding="utf-8"))
    assert payload


def test_package_c_pure_behavior_is_differentially_bound_to_c1_evidence() -> None:
    responses = json.loads((GOLDEN_ROOT / "responses.json").read_text(encoding="utf-8"))["preflight"]
    snapshot = parse_preflight_snapshot(
        status_line=responses["status_line"],
        parser_state=tuple(responses["parser_state"]),
        offset_lines=tuple(responses["offsets"]),
        errors=tuple(responses["errors"]),
        missing_ok_commands=tuple(responses["missing_ok_commands"]),
    )
    assert snapshot.to_json() == responses
    assert evaluate_preflight(snapshot, require_g54=True).allow_stream

    stream = json.loads((GOLDEN_ROOT / "stream.json").read_text(encoding="utf-8"))
    for item in stream["line_ack_transcript"]:
        actual = classify_command_response(tuple(item["response"]), expect_ok=True, timed_out=False)
        assert actual.kind.value == item["classification"]
        assert actual.acknowledged is item["acknowledged"]


def test_package_c_applies_only_the_two_approved_legacy_safety_corrections() -> None:
    deviations = json.loads((GOLDEN_ROOT / "deviations.json").read_text(encoding="utf-8"))
    missing_g54 = parse_preflight_snapshot(
        status_line="<Idle|MPos:192,192,192|WPos:96,96,3.5|WCO:96,96,188.5>",
        parser_state=("[GC:G0 G54 G17]", "ok"),
        offset_lines=("ok",),
        errors=(),
        missing_ok_commands=(),
    )
    no_mpos = parse_preflight_snapshot(
        status_line="<Idle|WPos:96,96,3.5|WCO:96,96,188.5>",
        parser_state=("[GC:G0 G54 G17]", "ok"),
        offset_lines=("[G54:96,96,188.5]", "ok"),
        errors=(),
        missing_ok_commands=(),
    )
    assert deviations["missing_g54"] == {"legacy": "warning", "package_c": "blocker"}
    assert not evaluate_preflight(missing_g54, require_g54=True).allow_stream
    assert deviations["post_home_mpos"] == {"legacy": "not_proven", "package_c": "required"}
    assert not evaluate_post_home(no_mpos, expected_mpos=(192.0, 192.0, 192.0), tolerance_mm=1.0).allow_stream


def test_package_c_reset_differential_has_opposite_fresh_and_late_meanings() -> None:
    reset = json.loads((GOLDEN_ROOT / "reset.json").read_text(encoding="utf-8"))
    snapshot = parse_preflight_snapshot(
        status_line="<Idle|MPos:0,0,0|WPos:0,0,0|WCO:0,0,0>",
        parser_state=("[GC:G0 G54 G17]", "ok"),
        offset_lines=("[G54:0,0,0]", "ok"),
        errors=(),
        missing_ok_commands=(),
    )
    evidence = tuple(reset["fresh_session"]["evidence"])
    assert (
        classify_session_observation(evidence, snapshot.status, SessionObservationPhase.FRESH_STABILIZATION).disposition
        is GateDisposition.AWAIT_HOME_ONLY
    )
    assert (
        classify_session_observation(evidence, snapshot.status, SessionObservationPhase.STABILIZED).disposition
        is GateDisposition.RECOVERY_REQUIRED
    )


def test_package_c_timeout_golden_is_a_typed_first_failure_stop() -> None:
    timeout = json.loads((GOLDEN_ROOT / "stream.json").read_text(encoding="utf-8"))["timeout_stop_transcript"][0]
    actual = classify_command_response(tuple(timeout["response"]), expect_ok=True, timed_out=True)
    assert actual.kind is ResponseKind.TIMEOUT
    assert actual.stop_before_next_command is timeout["stops_before_next_command"]
