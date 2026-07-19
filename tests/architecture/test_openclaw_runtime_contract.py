from __future__ import annotations

import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from drawingmachine.cli.parser import CliUsageError, build_parser
from drawingmachine.cli.render import _CHALLENGE_FIELDS, _EXECUTION_FIELDS, _MACHINE_DATA_FIELDS
from drawingmachine.cli.workflow import _read_json_object
from drawingmachine.domain.jobs.models import _JOB_FIELDS
from drawingmachine.domain.workflow.models import (
    _REVIEW_FIELDS,
    REVIEW_CHECK_NAMES,
    STATUS_TO_NEXT_STAGE,
    ProcessedImageReview,
)
from drawingmachine.domain.workflow.routing import validate_semantic_assessment
from drawingmachine.errors import DrawingMachineError
from drawingmachine.json_types import JsonObject
from drawingmachine.protocol.codec import _RESPONSE_FIELDS
from drawingmachine.service.runtime import GcodeCheckData, JobCancelData, JobStatusData, WorkflowRunData

_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ROOT = _ROOT / "src/drawingmachine/resources/openclaw/agents"
_DOCUMENTS = tuple(sorted(_AGENT_ROOT.glob("*/*.md")))
_RUNTIME_COMMANDS = frozenset(
    {
        "workflow.run",
        "job.status",
        "job.wait",
        "job.cancel",
        "gcode.check",
        "machine.status",
        "machine.prepare",
        "machine.home",
        "machine.zcal",
        "machine.zconfirm",
        "machine.stream",
    }
)
_ENVELOPE_PATHS = frozenset(_RESPONSE_FIELDS - {"protocol_version"})
_MOTION_COMMANDS = frozenset(
    {
        "machine.home",
        "machine.zcal",
        "machine.zconfirm",
        "machine.stream",
    }
)


def _data_paths(fields: frozenset[str]) -> frozenset[str]:
    return frozenset(f"data.{field}" for field in fields)


_ACCEPTED_RESPONSE_PATHS = {
    "workflow.run": _ENVELOPE_PATHS | _data_paths(WorkflowRunData.__required_keys__),
    "job.status": _ENVELOPE_PATHS
    | _data_paths(JobStatusData.__required_keys__)
    | frozenset(f"data.job.{field}" for field in _JOB_FIELDS),
    "job.wait": _ENVELOPE_PATHS
    | _data_paths(JobStatusData.__required_keys__)
    | frozenset(f"data.job.{field}" for field in _JOB_FIELDS),
    "job.cancel": _ENVELOPE_PATHS | _data_paths(JobCancelData.__required_keys__),
    "gcode.check": _ENVELOPE_PATHS | _data_paths(GcodeCheckData.__required_keys__),
    "machine.status": _ENVELOPE_PATHS
    | _data_paths(frozenset(_MACHINE_DATA_FIELDS))
    | frozenset(f"data.execution.{field}" for field in _EXECUTION_FIELDS),
    "machine.prepare": _ENVELOPE_PATHS
    | _data_paths(frozenset(_MACHINE_DATA_FIELDS))
    | frozenset(f"data.execution.{field}" for field in _EXECUTION_FIELDS),
    **{
        command: _ENVELOPE_PATHS
        | _data_paths(frozenset(_MACHINE_DATA_FIELDS))
        | frozenset(f"data.execution.{field}" for field in _EXECUTION_FIELDS)
        | frozenset(f"data.challenge.{field}" for field in _CHALLENGE_FIELDS)
        for command in _MOTION_COMMANDS
    },
}
_COMMAND_DATA_PATHS = {
    "workflow.run": frozenset({"data.job_id", "data.revision", "data.state"}),
    "job.status": frozenset({"data.job.job_id", "data.job.revision", "data.job.state"}),
    "job.wait": frozenset({"data.job.job_id", "data.job.revision", "data.job.state"}),
    "job.cancel": frozenset({"data.job_id", "data.cancellation_requested"}),
    "gcode.check": frozenset({"data.job_id", "data.state", "data.static_result"}),
    "machine.prepare": frozenset(
        {
            "data.execution.execution_id",
            "data.execution.job_id",
            "data.execution.ready_revision",
            "data.execution.revision",
            "data.execution.phase",
        }
    ),
    "machine.status": frozenset({"data.accepting", "data.execution.phase", "data.progress"}),
    **{
        command: frozenset(
            {
                "data.challenge.challenge_id",
                "data.challenge.purpose",
                "data.challenge.evidence",
                "data.execution.phase",
                "data.execution.revision",
            }
        )
        for command in _MOTION_COMMANDS
    },
}
_COMMAND_RESPONSE_PATHS = {command: _ENVELOPE_PATHS | paths for command, paths in _COMMAND_DATA_PATHS.items()}
_FROZEN_JSON_PATHS = frozenset().union(*_COMMAND_RESPONSE_PATHS.values())
_COMMAND_SPECS = {
    ("Runtime", "WORKFLOW_SUBMIT_AUTO"): (
        "workflow.run",
        "drawingmachine --output json workflow run --job-name JOB_NAME --input-image IMAGE_PATH --route-mode auto --semantic-assessment-json ASSESSMENT_JSON",
        ("data.job_id", "data.revision", "data.state"),
    ),
    ("Runtime", "WORKFLOW_SUBMIT_DIRECT"): (
        "workflow.run",
        "drawingmachine --output json workflow run --job-name JOB_NAME --input-image IMAGE_PATH --route-mode direct",
        ("data.job_id", "data.revision", "data.state"),
    ),
    ("Runtime", "WORKFLOW_SUBMIT_IMAGE_EDIT"): (
        "workflow.run",
        "drawingmachine --output json workflow run --job-name JOB_NAME --input-image IMAGE_PATH --route-mode image-edit",
        ("data.job_id", "data.revision", "data.state"),
    ),
    ("Runtime", "WORKFLOW_CONTINUE_REVIEW"): (
        "workflow.run",
        "drawingmachine --output json workflow run --resume-job JOB_ID --review-json REVIEW_JSON",
        ("data.job_id", "data.revision", "data.state"),
    ),
    ("Runtime", "JOB_WAIT"): (
        "job.wait",
        "drawingmachine --output json job wait JOB_ID --timeout-seconds 300 --poll-interval-seconds 1",
        ("data.job.job_id", "data.job.revision", "data.job.state"),
    ),
    ("Runtime", "JOB_STATUS"): (
        "job.status",
        "drawingmachine --output json job status JOB_ID",
        ("data.job.job_id", "data.job.revision", "data.job.state"),
    ),
    ("Runtime", "JOB_CANCEL"): (
        "job.cancel",
        "drawingmachine --output json job cancel JOB_ID",
        ("data.job_id", "data.cancellation_requested"),
    ),
    ("Runtime", "GCODE_CHECK"): (
        "gcode.check",
        "drawingmachine --output json gcode check GCODE_PATH --timeout-seconds 300 --poll-interval-seconds 1",
        ("data.job_id", "data.state", "data.static_result"),
    ),
    ("Runtime", "MACHINE_PREPARE"): (
        "machine.prepare",
        "drawingmachine --output json machine prepare JOB_ID --job-revision 7",
        (
            "data.execution.execution_id",
            "data.execution.job_id",
            "data.execution.ready_revision",
            "data.execution.revision",
            "data.execution.phase",
        ),
    ),
    ("Runtime", "MACHINE_STATUS"): (
        "machine.status",
        "drawingmachine --output json machine status --execution-id EXECUTION_ID",
        ("data.accepting", "data.execution.phase", "data.progress"),
    ),
    **{
        ("Runtime", f"MACHINE_{name}_CHALLENGE"): (
            command,
            f"drawingmachine --output json machine {verb} EXECUTION_ID",
            (
                "data.challenge.challenge_id",
                "data.challenge.purpose",
                "data.challenge.evidence",
            ),
        )
        for name, command, verb in (
            ("HOME", "machine.home", "home"),
            ("ZCAL", "machine.zcal", "zcal"),
            ("ZCONFIRM", "machine.zconfirm", "zconfirm"),
            ("STREAM", "machine.stream", "stream"),
        )
    },
    **{
        ("Operator", f"MACHINE_{name}_APPROVAL"): (
            command,
            f"drawingmachine --output json machine {verb} EXECUTION_ID --approve CHALLENGE_ID",
            ("data.execution.phase", "data.execution.revision"),
        )
        for name, command, verb in (
            ("HOME", "machine.home", "home"),
            ("ZCAL", "machine.zcal", "zcal"),
            ("ZCONFIRM", "machine.zconfirm", "zconfirm"),
            ("STREAM", "machine.stream", "stream"),
        )
    },
}
_CANONICAL_SCHEMAS: dict[str, JsonObject] = {
    "SEMANTIC_ASSESSMENT_V1": cast(
        JsonObject,
        validate_semantic_assessment(
            {
                "semantic_style_score": 0.25,
                "semantic_score_source": "router_multimodal_model",
                "semantic_rationale": ["clear subject contour", "low detail density"],
                "semantic_blockers": [],
            }
        ),
    ),
    "PROCESSED_IMAGE_REVIEW_V1": {
        "schema": "processed_image_review_v1",
        "job_name": "JOB_NAME",
        "reviewed_at": "2026-07-14T00:00:00Z",
        "reviewer": "agent:cnc-drawable-translator",
        "processed_image": "PROCESSED_IMAGE_PATH",
        "handoff": None,
        "status": "PASS_TO_BUILD",
        "checks": {name: True for name in REVIEW_CHECK_NAMES},
        "issues": [],
        "revised_prompt": None,
        "next_allowed_stage": STATUS_TO_NEXT_STAGE["PASS_TO_BUILD"],
    },
}
_SCHEMA_METADATA = {
    "SEMANTIC_ASSESSMENT_V1": frozenset(
        {
            "Schema enum: SEMANTIC_SCORE_SOURCE => router_multimodal_model",
            "Schema constraint: SEMANTIC_STYLE_SCORE_0_TO_0_30",
            "Schema constraint: SEMANTIC_RATIONALE_STRING_ARRAY",
            "Schema constraint: SEMANTIC_BLOCKERS_STRING_ARRAY",
        }
    ),
    "PROCESSED_IMAGE_REVIEW_V1": frozenset(
        {
            "Schema enum: PROCESSED_IMAGE_REVIEW_STATUS => PASS_TO_BUILD, REVISE_PROMPT, REJECT_INPUT",
            "Schema enum: PROCESSED_IMAGE_REVIEW_NEXT_STAGE => build_drawable_job, repeat_image_edit, stop",
            "Schema constraint: PROCESSED_IMAGE_REVIEW_STATUS_STAGE_BINDING",
            "Schema constraint: PROCESSED_IMAGE_REVIEW_EXACT_EIGHT_BOOLEAN_CHECKS",
        }
    ),
}
_HEADINGS = frozenset(
    {
        "# CNC Drawable Contract",
        "# CNC Drawable Router Agent Contract",
        "# CNC Drawable Router Tool Contract",
        "# CNC Drawable Translator Agent Contract",
        "# CNC Drawable Translator Tool Contract",
        "## Identity",
        "## Inputs",
        "## Runtime",
        "## Machine",
        "## Operator handoff",
        "## Boundaries",
    }
)
_RECORD_VALUES = {
    "Agent": frozenset({"CNC_DRAWABLE_ROUTER", "CNC_DRAWABLE_TRANSLATOR"}),
    "Runtime input": frozenset(
        {
            "CALLER_READABLE_IMAGE_PATH",
            "CALLER_READABLE_GCODE_PATH",
            "SEMANTIC_ASSESSMENT_V1",
            "PROCESSED_IMAGE_REVIEW_V1",
            "CURRENT_JOB_REVISION",
        }
    ),
    "Runtime action": frozenset(
        {
            "INSPECT_INPUT_IMAGE",
            "SCORE_PEN_LINEWORK_SUITABILITY",
            "WRITE_SEMANTIC_ASSESSMENT_V1",
            "SEMANTIC_SCORE_RANGE_0_TO_0_30",
            "SEMANTIC_SCORE_USES_IMAGE_NOT_PROMPT",
            "CONTENT_CATEGORY_IS_NOT_A_BLOCKER",
            "SELECT_AUTO_ROUTE_BY_SEMANTIC_ASSESSMENT",
            "SELECT_DIRECT_ROUTE_ONLY_FOR_EXPLICIT_PRESERVATION",
            "SELECT_IMAGE_EDIT_ROUTE_ONLY_FOR_EXPLICIT_REDRAW",
            "SUBMIT_EXACTLY_ONE_WORKFLOW",
            "RETURN_JSON_TO_TRANSLATOR_AND_STOP",
            "VALIDATE_JSON_ENVELOPE",
            "CONSUME_DATA_ONLY_WHEN_OK",
            "REPORT_ERROR_WHEN_NOT_OK",
            "SPAWN_ONE_TEMPORARY_ROUTER",
            "CLOSE_ROUTER_AFTER_SUBMISSION",
            "INSPECT_PROCESSED_IMAGE",
            "WRITE_PROCESSED_IMAGE_REVIEW_V1",
            "REVIEW_PASS_TO_BUILD",
            "REVIEW_REVISE_PROMPT_REPEAT_IMAGE_EDIT",
            "REVIEW_REJECT_INPUT_STOP",
            "CONTINUE_SAME_JOB",
            "CANCEL_PRE_MACHINE_JOB_ON_USER_REQUEST",
            "CHECK_GCODE",
            "PREPARE_READY_JOB",
            "OBSERVE_MACHINE_STATUS",
            "REQUEST_NEXT_PHASE_CHALLENGE",
            "DISPLAY_CHALLENGE_EVIDENCE",
            "STOP_FOR_HUMAN_OPERATOR",
            "RETURN_TO_MACHINE_STATUS_AFTER_OPERATOR_ACTION",
            "REPORT_FAILURE_WITHOUT_ADVANCE",
        }
    ),
    "Operator handoff": frozenset(
        {
            "ROUTER_RETURNS_JSON_TO_TRANSLATOR",
            "DISPLAY_FIXED_CHALLENGE_AND_EVIDENCE",
            "HUMAN_RUNS_SEPARATE_APPROVAL_COMMAND",
            "REGISTRY_CUTOVER_IS_SEPARATE_OPERATOR_WORK",
        }
    ),
    "Schema enum": frozenset(
        line.removeprefix("Schema enum: ")
        for lines in _SCHEMA_METADATA.values()
        for line in lines
        if line.startswith("Schema enum: ")
    ),
    "Schema constraint": frozenset(
        line.removeprefix("Schema constraint: ")
        for lines in _SCHEMA_METADATA.values()
        for line in lines
        if line.startswith("Schema constraint: ")
    ),
    "Boundary": frozenset(
        {
            "UNIFIED_CLI_ONLY",
            "JSON_OUTPUT_ONLY",
            "NO_FILESYSTEM_HANDOFF",
            "NO_SELF_INSTALL",
            "NO_REGISTRY_WRITE",
            "NO_DIRECT_SOCKET_SERIAL_BRIDGE",
            "NO_DIRECT_PROVIDER_CALL",
            "NO_JOB_ARTIFACT_MUTATION",
            "NO_DUPLICATE_SUBMISSION",
            "ROUTER_SINGLE_USE",
            "HUMAN_APPROVAL_ONLY",
            "NO_APPROVAL_CONSUMPTION",
            "NO_PHASE_COMBINATION",
            "MACHINE_STATUS_ONLY_MONITOR",
            "NO_AUTOMATIC_RETRY",
            "STATE_PROVES_COMPLETION",
            "READY_JOB_ONLY",
            "CHALLENGE_REQUEST_IS_NOT_EXECUTION",
            "PATH_PASSED_DIRECTLY",
            "RUNTIME_DEPLOYMENT_FORBIDDEN",
            "REGISTRY_CUTOVER_OPERATOR_ONLY",
            "INSTALL_REQUIRES_SAFE_CHECK",
        }
    ),
}
_AGENT_RECORD_VALUES = {
    "cnc-drawable-router": {
        "Runtime input": frozenset({"CALLER_READABLE_IMAGE_PATH", "SEMANTIC_ASSESSMENT_V1"}),
        "Runtime action": frozenset(
            {
                "INSPECT_INPUT_IMAGE",
                "SCORE_PEN_LINEWORK_SUITABILITY",
                "WRITE_SEMANTIC_ASSESSMENT_V1",
                "SEMANTIC_SCORE_RANGE_0_TO_0_30",
                "SEMANTIC_SCORE_USES_IMAGE_NOT_PROMPT",
                "CONTENT_CATEGORY_IS_NOT_A_BLOCKER",
                "SELECT_AUTO_ROUTE_BY_SEMANTIC_ASSESSMENT",
                "SELECT_DIRECT_ROUTE_ONLY_FOR_EXPLICIT_PRESERVATION",
                "SELECT_IMAGE_EDIT_ROUTE_ONLY_FOR_EXPLICIT_REDRAW",
                "SUBMIT_EXACTLY_ONE_WORKFLOW",
                "RETURN_JSON_TO_TRANSLATOR_AND_STOP",
                "VALIDATE_JSON_ENVELOPE",
                "CONSUME_DATA_ONLY_WHEN_OK",
                "REPORT_ERROR_WHEN_NOT_OK",
            }
        ),
        "Operator handoff": frozenset({"ROUTER_RETURNS_JSON_TO_TRANSLATOR"}),
    },
    "cnc-drawable-translator": {
        "Runtime input": frozenset({"CALLER_READABLE_GCODE_PATH", "PROCESSED_IMAGE_REVIEW_V1", "CURRENT_JOB_REVISION"}),
        "Runtime action": _RECORD_VALUES["Runtime action"]
        - {
            "INSPECT_INPUT_IMAGE",
            "SCORE_PEN_LINEWORK_SUITABILITY",
            "WRITE_SEMANTIC_ASSESSMENT_V1",
            "SEMANTIC_SCORE_RANGE_0_TO_0_30",
            "SEMANTIC_SCORE_USES_IMAGE_NOT_PROMPT",
            "CONTENT_CATEGORY_IS_NOT_A_BLOCKER",
            "SELECT_AUTO_ROUTE_BY_SEMANTIC_ASSESSMENT",
            "SELECT_DIRECT_ROUTE_ONLY_FOR_EXPLICIT_PRESERVATION",
            "SELECT_IMAGE_EDIT_ROUTE_ONLY_FOR_EXPLICIT_REDRAW",
            "SUBMIT_EXACTLY_ONE_WORKFLOW",
            "RETURN_JSON_TO_TRANSLATOR_AND_STOP",
        },
        "Operator handoff": _RECORD_VALUES["Operator handoff"] - {"ROUTER_RETURNS_JSON_TO_TRANSLATOR"},
    },
}
_TRANSLATOR_COMMAND_IDS = frozenset(
    key
    for key in _COMMAND_SPECS
    if key[1] not in {"WORKFLOW_SUBMIT_AUTO", "WORKFLOW_SUBMIT_DIRECT", "WORKFLOW_SUBMIT_IMAGE_EDIT"}
)
_DOCUMENT_COMMAND_IDS = {
    ("cnc-drawable-router", "AGENTS.md"): frozenset(
        {
            ("Runtime", "WORKFLOW_SUBMIT_AUTO"),
            ("Runtime", "WORKFLOW_SUBMIT_DIRECT"),
            ("Runtime", "WORKFLOW_SUBMIT_IMAGE_EDIT"),
        }
    ),
    ("cnc-drawable-router", "TOOLS.md"): frozenset({("Runtime", "WORKFLOW_SUBMIT_AUTO")}),
    ("cnc-drawable-translator", "AGENTS.md"): _TRANSLATOR_COMMAND_IDS,
    ("cnc-drawable-translator", "TOOLS.md"): _TRANSLATOR_COMMAND_IDS,
}
_ROUTER_BOUNDARIES = frozenset(
    {
        "UNIFIED_CLI_ONLY",
        "JSON_OUTPUT_ONLY",
        "PATH_PASSED_DIRECTLY",
        "NO_FILESYSTEM_HANDOFF",
        "NO_DIRECT_SOCKET_SERIAL_BRIDGE",
        "NO_DIRECT_PROVIDER_CALL",
        "NO_JOB_ARTIFACT_MUTATION",
        "NO_DUPLICATE_SUBMISSION",
        "ROUTER_SINGLE_USE",
        "NO_APPROVAL_CONSUMPTION",
        "RUNTIME_DEPLOYMENT_FORBIDDEN",
    }
)
_TRANSLATOR_BOUNDARIES = frozenset(
    {
        "UNIFIED_CLI_ONLY",
        "JSON_OUTPUT_ONLY",
        "PATH_PASSED_DIRECTLY",
        "NO_FILESYSTEM_HANDOFF",
        "NO_DIRECT_SOCKET_SERIAL_BRIDGE",
        "NO_SELF_INSTALL",
        "NO_REGISTRY_WRITE",
        "RUNTIME_DEPLOYMENT_FORBIDDEN",
        "REGISTRY_CUTOVER_OPERATOR_ONLY",
        "INSTALL_REQUIRES_SAFE_CHECK",
        "HUMAN_APPROVAL_ONLY",
        "NO_APPROVAL_CONSUMPTION",
        "NO_PHASE_COMBINATION",
        "MACHINE_STATUS_ONLY_MONITOR",
        "NO_AUTOMATIC_RETRY",
        "STATE_PROVES_COMPLETION",
        "READY_JOB_ONLY",
        "CHALLENGE_REQUEST_IS_NOT_EXECUTION",
    }
)
_ROUTER_TOOL_ACTIONS = frozenset(
    {
        "INSPECT_INPUT_IMAGE",
        "SCORE_PEN_LINEWORK_SUITABILITY",
        "WRITE_SEMANTIC_ASSESSMENT_V1",
        "SEMANTIC_SCORE_RANGE_0_TO_0_30",
        "SEMANTIC_SCORE_USES_IMAGE_NOT_PROMPT",
        "CONTENT_CATEGORY_IS_NOT_A_BLOCKER",
        "SELECT_AUTO_ROUTE_BY_SEMANTIC_ASSESSMENT",
        "VALIDATE_JSON_ENVELOPE",
        "CONSUME_DATA_ONLY_WHEN_OK",
        "REPORT_ERROR_WHEN_NOT_OK",
        "RETURN_JSON_TO_TRANSLATOR_AND_STOP",
    }
)
_TRANSLATOR_TOOL_ACTIONS = frozenset(
    {
        "VALIDATE_JSON_ENVELOPE",
        "CONSUME_DATA_ONLY_WHEN_OK",
        "REPORT_ERROR_WHEN_NOT_OK",
        "INSPECT_PROCESSED_IMAGE",
        "WRITE_PROCESSED_IMAGE_REVIEW_V1",
        "REVIEW_PASS_TO_BUILD",
        "REVIEW_REVISE_PROMPT_REPEAT_IMAGE_EDIT",
        "REVIEW_REJECT_INPUT_STOP",
    }
)


def _record_lines(prefix: str, values: frozenset[str]) -> frozenset[str]:
    return frozenset(f"{prefix}: {value}" for value in values)


def _command_record_lines(commands: frozenset[tuple[str, str]]) -> frozenset[str]:
    result: set[str] = set()
    for role, command_id in commands:
        result.add(f"{role} command: {command_id}")
        result.add(f"{role} result: {command_id} => {', '.join(_COMMAND_SPECS[(role, command_id)][2])}")
    return frozenset(result)


def _document_inventory(
    *,
    agent: str,
    inputs: frozenset[str],
    actions: frozenset[str],
    handoffs: frozenset[str],
    boundaries: frozenset[str],
    commands: frozenset[tuple[str, str]],
    schema: str,
) -> frozenset[str]:
    return (
        frozenset({f"Agent: {agent}", f"Schema: {schema}"})
        | _record_lines("Runtime input", inputs)
        | _record_lines("Runtime action", actions)
        | _record_lines("Operator handoff", handoffs)
        | _record_lines("Boundary", boundaries)
        | _command_record_lines(commands)
        | _SCHEMA_METADATA[schema]
    )


_DOCUMENT_RECORD_INVENTORY = {
    ("cnc-drawable-router", "AGENTS.md"): _document_inventory(
        agent="CNC_DRAWABLE_ROUTER",
        inputs=_AGENT_RECORD_VALUES["cnc-drawable-router"]["Runtime input"],
        actions=_AGENT_RECORD_VALUES["cnc-drawable-router"]["Runtime action"],
        handoffs=_AGENT_RECORD_VALUES["cnc-drawable-router"]["Operator handoff"],
        boundaries=_ROUTER_BOUNDARIES,
        commands=_DOCUMENT_COMMAND_IDS[("cnc-drawable-router", "AGENTS.md")],
        schema="SEMANTIC_ASSESSMENT_V1",
    ),
    ("cnc-drawable-router", "TOOLS.md"): _document_inventory(
        agent="CNC_DRAWABLE_ROUTER",
        inputs=_AGENT_RECORD_VALUES["cnc-drawable-router"]["Runtime input"],
        actions=_ROUTER_TOOL_ACTIONS,
        handoffs=_AGENT_RECORD_VALUES["cnc-drawable-router"]["Operator handoff"],
        boundaries=_ROUTER_BOUNDARIES,
        commands=_DOCUMENT_COMMAND_IDS[("cnc-drawable-router", "TOOLS.md")],
        schema="SEMANTIC_ASSESSMENT_V1",
    ),
    ("cnc-drawable-translator", "AGENTS.md"): _document_inventory(
        agent="CNC_DRAWABLE_TRANSLATOR",
        inputs=_AGENT_RECORD_VALUES["cnc-drawable-translator"]["Runtime input"],
        actions=_AGENT_RECORD_VALUES["cnc-drawable-translator"]["Runtime action"],
        handoffs=_AGENT_RECORD_VALUES["cnc-drawable-translator"]["Operator handoff"],
        boundaries=_TRANSLATOR_BOUNDARIES,
        commands=_DOCUMENT_COMMAND_IDS[("cnc-drawable-translator", "AGENTS.md")],
        schema="PROCESSED_IMAGE_REVIEW_V1",
    ),
    ("cnc-drawable-translator", "TOOLS.md"): _document_inventory(
        agent="CNC_DRAWABLE_TRANSLATOR",
        inputs=_AGENT_RECORD_VALUES["cnc-drawable-translator"]["Runtime input"],
        actions=_TRANSLATOR_TOOL_ACTIONS,
        handoffs=frozenset({"DISPLAY_FIXED_CHALLENGE_AND_EVIDENCE", "HUMAN_RUNS_SEPARATE_APPROVAL_COMMAND"}),
        boundaries=_TRANSLATOR_BOUNDARIES,
        commands=_DOCUMENT_COMMAND_IDS[("cnc-drawable-translator", "TOOLS.md")],
        schema="PROCESSED_IMAGE_REVIEW_V1",
    ),
}
_DOCUMENT_HEADINGS = {
    ("cnc-drawable-router", "AGENTS.md"): (
        "# CNC Drawable Router Agent Contract",
        "## Identity",
        "## Inputs",
        "## Runtime",
        "## Operator handoff",
        "## Boundaries",
    ),
    ("cnc-drawable-router", "TOOLS.md"): (
        "# CNC Drawable Router Tool Contract",
        "## Identity",
        "## Inputs",
        "## Runtime",
        "## Operator handoff",
        "## Boundaries",
    ),
    ("cnc-drawable-translator", "AGENTS.md"): (
        "# CNC Drawable Translator Agent Contract",
        "## Identity",
        "## Inputs",
        "## Runtime",
        "## Machine",
        "## Operator handoff",
        "## Boundaries",
    ),
    ("cnc-drawable-translator", "TOOLS.md"): (
        "# CNC Drawable Translator Tool Contract",
        "## Identity",
        "## Inputs",
        "## Runtime",
        "## Machine",
        "## Operator handoff",
        "## Boundaries",
    ),
}
_IDENTITY_ACTIONS = {
    ("cnc-drawable-router", "AGENTS.md"): _AGENT_RECORD_VALUES["cnc-drawable-router"]["Runtime action"],
    ("cnc-drawable-translator", "AGENTS.md"): frozenset(
        {
            "SPAWN_ONE_TEMPORARY_ROUTER",
            "CLOSE_ROUTER_AFTER_SUBMISSION",
            "REPORT_FAILURE_WITHOUT_ADVANCE",
            "VALIDATE_JSON_ENVELOPE",
            "CONSUME_DATA_ONLY_WHEN_OK",
            "REPORT_ERROR_WHEN_NOT_OK",
        }
    ),
    ("cnc-drawable-translator", "TOOLS.md"): frozenset(
        {"VALIDATE_JSON_ENVELOPE", "CONSUME_DATA_ONLY_WHEN_OK", "REPORT_ERROR_WHEN_NOT_OK"}
    ),
}
_INPUT_ACTIONS = {
    ("cnc-drawable-router", "TOOLS.md"): _ROUTER_TOOL_ACTIONS
    - {
        "VALIDATE_JSON_ENVELOPE",
        "CONSUME_DATA_ONLY_WHEN_OK",
        "REPORT_ERROR_WHEN_NOT_OK",
        "RETURN_JSON_TO_TRANSLATOR_AND_STOP",
    }
}
_MACHINE_ACTIONS = {
    ("cnc-drawable-translator", "AGENTS.md"): frozenset(
        {
            "PREPARE_READY_JOB",
            "OBSERVE_MACHINE_STATUS",
            "REQUEST_NEXT_PHASE_CHALLENGE",
            "DISPLAY_CHALLENGE_EVIDENCE",
            "STOP_FOR_HUMAN_OPERATOR",
        }
    )
}
_OPERATOR_ACTIONS = {
    ("cnc-drawable-translator", "AGENTS.md"): frozenset({"RETURN_TO_MACHINE_STATUS_AFTER_OPERATOR_ACTION"})
}


def _record_section(document_key: tuple[str, str], line: str) -> str | None:
    if line.startswith("Agent: "):
        return "Identity"
    if line.startswith(("Runtime input: ", "Schema: ", "Schema enum: ", "Schema constraint: ")):
        return "Inputs"
    if line.startswith("Boundary: "):
        return "Boundaries"
    if line.startswith(("Operator command: ", "Operator result: ", "Operator handoff: ")):
        return "Operator handoff"
    command = re.fullmatch(r"Runtime (?:command|result): (MACHINE_[A-Z0-9_]+)(?: => .+)?", line)
    if command is not None:
        return "Machine"
    if line.startswith(("Runtime command: ", "Runtime result: ")):
        return "Runtime"
    if line.startswith("Runtime action: "):
        action = line.removeprefix("Runtime action: ")
        for section, actions in (
            ("Identity", _IDENTITY_ACTIONS.get(document_key, frozenset())),
            ("Inputs", _INPUT_ACTIONS.get(document_key, frozenset())),
            ("Machine", _MACHINE_ACTIONS.get(document_key, frozenset())),
            ("Operator handoff", _OPERATOR_ACTIONS.get(document_key, frozenset())),
        ):
            if action in actions:
                return section
        return "Runtime"
    return None


_DOCUMENT_SECTION_INVENTORY = {
    document_key: frozenset((_record_section(document_key, line), line) for line in records)
    for document_key, records in _DOCUMENT_RECORD_INVENTORY.items()
}


@dataclass(frozen=True)
class CommandExample:
    path: Path
    section: str
    line: int
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class FieldContract:
    command: str
    line: int
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ParsedDocument:
    path: Path
    text: str
    commands: tuple[CommandExample, ...]
    inline_references: tuple[str, ...]
    json_paths: tuple[str, ...]
    json_objects: tuple[JsonObject, ...]
    field_contracts: tuple[FieldContract, ...]


def _logical_commands(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    commands: list[tuple[int, str]] = []
    start = 0
    parts: list[str] = []
    for line, raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not parts:
            start = line
        continued = stripped.endswith("\\")
        parts.append(stripped[:-1].strip() if continued else stripped)
        if not continued:
            commands.append((start, " ".join(parts)))
            parts = []
    if parts:
        commands.append((start, " ".join(parts)))
    return commands


def _parse_text(path: Path, text: str) -> ParsedDocument:
    section = ""
    fence_language: str | None = None
    fence_lines: list[tuple[int, str]] = []
    commands: list[CommandExample] = []
    json_objects: list[JsonObject] = []
    for number, line in enumerate(text.splitlines(), 1):
        if fence_language is None and line.startswith("#"):
            section = line.lstrip("#").strip()
        if line.startswith("```"):
            if fence_language is None:
                fence_language = line[3:].strip().lower()
                fence_lines = []
            else:
                if fence_language in {"bash", "sh", "shell", "console"}:
                    for start, command in _logical_commands(fence_lines):
                        commands.append(CommandExample(path, section, start, tuple(shlex.split(command))))
                elif fence_language == "json":
                    value = json.loads("\n".join(line for _, line in fence_lines))
                    assert isinstance(value, dict)
                    json_objects.append(cast(JsonObject, value))
                fence_language = None
            continue
        if fence_language is not None:
            fence_lines.append((number, line))
    inline = tuple(match.group(1) for match in re.finditer(r"`([^`\n]+)`", text))
    field_contracts: list[FieldContract] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.fullmatch(r"(Runtime|Operator) result: ([A-Z0-9_]+) => (.+)", line)
        if match is None:
            continue
        spec = _COMMAND_SPECS.get((match.group(1), match.group(2)))
        if spec is None:
            continue
        paths = tuple(match.group(3).split(", "))
        field_contracts.append(FieldContract(spec[0], number, paths))
    inline_paths = tuple(reference.removeprefix("json:") for reference in inline if reference.startswith("json:"))
    json_paths = inline_paths + tuple(path for contract in field_contracts for path in contract.paths)
    return ParsedDocument(
        path,
        text,
        tuple(commands),
        inline,
        json_paths,
        tuple(json_objects),
        tuple(field_contracts),
    )


def _parse(path: Path) -> ParsedDocument:
    return _parse_text(path, path.read_text(encoding="utf-8"))


def _command_name(tokens: tuple[str, ...]) -> str:
    return ".".join(tokens[3:5]) if len(tokens) >= 5 else ""


def _field_contract_violations(document: ParsedDocument) -> tuple[str, ...]:
    violations: list[str] = []
    contracted_paths: set[str] = set()
    for contract in document.field_contracts:
        accepted = _COMMAND_RESPONSE_PATHS.get(contract.command)
        if accepted is None:
            violations.append(f"line {contract.line}: unknown command {contract.command!r}")
            continue
        if not contract.paths:
            violations.append(f"line {contract.line}: empty response contract")
        contracted_paths.update(contract.paths)
        violations.extend(
            f"line {contract.line}: {contract.command} cannot return {path}"
            for path in contract.paths
            if path not in accepted
        )
    for example in document.commands:
        command = _command_name(example.tokens)
        adjacent = [
            contract
            for contract in document.field_contracts
            if contract.command == command and 0 < contract.line - example.line <= 4
        ]
        if not adjacent:
            violations.append(f"line {example.line}: {command} has no adjacent response contract")
    violations.extend(
        f"unbound documented field {path}"
        for path in document.json_paths
        if path.startswith("data.") and path not in contracted_paths
    )
    return tuple(violations)


def _decode_json_object(text: str) -> JsonObject:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def exact_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    value = json.loads(text, parse_constant=reject_constant, object_pairs_hook=exact_pairs)
    if not isinstance(value, dict):
        raise ValueError("schema example must be a JSON object")
    return cast(JsonObject, value)


def _schema_violations(schema_id: str, text: str) -> tuple[str, ...]:
    expected = _CANONICAL_SCHEMAS.get(schema_id)
    if expected is None:
        return (f"unknown schema {schema_id!r}",)
    try:
        value = _decode_json_object(text)
        if schema_id == "SEMANTIC_ASSESSMENT_V1":
            normalized = validate_semantic_assessment(value)
            if set(value) != set(normalized) or value != normalized:
                raise ValueError("semantic assessment is not the exact accepted object")
        else:
            review = ProcessedImageReview.from_json(value)
            if set(value) != _REVIEW_FIELDS or review.to_json() != value:
                raise ValueError("processed image review is not the exact accepted object")
        if value != expected:
            raise ValueError("schema example does not match the frozen canonical object")
    except (DrawingMachineError, ValueError, RecursionError):
        return (f"schema {schema_id} example is invalid",)
    return ()


def _closed_contract_violations(path: Path, text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    violations: list[str] = []
    document_key = (path.parent.name, path.name)
    expected_commands = _DOCUMENT_COMMAND_IDS.get(document_key)
    expected_records = _DOCUMENT_RECORD_INVENTORY.get(document_key)
    expected_headings = _DOCUMENT_HEADINGS.get(document_key)
    expected_section_records = _DOCUMENT_SECTION_INVENTORY.get(document_key)
    seen_commands: list[tuple[str, str]] = []
    seen_records: list[str] = []
    seen_headings: list[str] = []
    seen_section_records: list[tuple[str | None, str]] = []
    section: str | None = None

    def bind_record(line: str, number: int) -> None:
        expected_section = _record_section(document_key, line)
        seen_section_records.append((section, line))
        if expected_section is not None and section != expected_section:
            violations.append(f"line {number}: record is not allowed in section {section!r}")

    index = 0
    while index < len(lines):
        line = lines[index]
        number = index + 1
        if not line:
            index += 1
            continue
        if line.startswith("#"):
            seen_headings.append(line)
            if line not in _HEADINGS:
                violations.append(f"line {number}: unknown heading")
            section = line.removeprefix("## ") if line.startswith("## ") else None
            index += 1
            continue
        command_match = re.fullmatch(r"(Runtime|Operator) command: ([A-Z0-9_]+)", line)
        if command_match is not None:
            role, command_id = command_match.groups()
            command_key = (role, command_id)
            seen_commands.append(command_key)
            seen_records.append(line)
            bind_record(line, number)
            spec = _COMMAND_SPECS.get((role, command_id))
            if spec is None:
                violations.append(f"line {number}: unknown {role.lower()} command")
                index += 1
                continue
            if expected_commands is not None and command_key not in expected_commands:
                violations.append(f"line {number}: command is not allowed for this document")
            result = f"{role} result: {command_id} => {', '.join(spec[2])}"
            expected = (line, "```bash", spec[1], "```", result)
            actual = tuple(lines[index : index + len(expected)])
            if actual != expected:
                violations.append(f"line {number}: command block or result mismatch")
            if any(item.startswith("#") for item in actual[1:]):
                violations.append(f"line {number}: command block crosses a section boundary")
            if len(actual) == len(expected):
                seen_records.append(actual[-1])
                bind_record(actual[-1], number + len(expected) - 1)
            index += len(expected)
            continue
        if re.match(r"(?:Runtime|Operator) result:", line):
            violations.append(f"line {number}: result is not bound to a command block")
            index += 1
            continue
        schema_match = re.fullmatch(r"Schema: ([A-Z0-9_]+)", line)
        if schema_match is not None:
            schema_id = schema_match.group(1)
            seen_records.append(line)
            bind_record(line, number)
            if index + 1 >= len(lines) or lines[index + 1] != "```json":
                violations.append(f"line {number}: schema must be followed by a json fence")
                index += 1
                continue
            try:
                closing = lines.index("```", index + 2)
            except ValueError:
                violations.append(f"line {number}: schema json fence is not closed")
                break
            if any(item.startswith("#") for item in lines[index + 2 : closing]):
                violations.append(f"line {number}: schema block crosses a section boundary")
            violations.extend(_schema_violations(schema_id, "\n".join(lines[index + 2 : closing])))
            index = closing + 1
            continue
        record = line.split(": ", 1)
        if len(record) != 2 or record[0] not in _RECORD_VALUES:
            violations.append(f"line {number}: unclassified content")
        elif record[1] not in _RECORD_VALUES[record[0]]:
            violations.append(f"line {number}: unknown {record[0].lower()} value")
        else:
            seen_records.append(line)
            bind_record(line, number)
            agent_values = _AGENT_RECORD_VALUES.get(path.parent.name, {}).get(record[0])
            if agent_values is not None and record[1] not in agent_values:
                violations.append(f"line {number}: {record[0].lower()} is not allowed for this agent")
        index += 1

    expected_agent = {
        "cnc-drawable-router": "Agent: CNC_DRAWABLE_ROUTER",
        "cnc-drawable-translator": "Agent: CNC_DRAWABLE_TRANSLATOR",
    }.get(path.parent.name)
    if expected_agent is not None and lines.count(expected_agent) != 1:
        violations.append("agent identity is missing or duplicated")
    if expected_commands is not None and frozenset(seen_commands) != expected_commands:
        violations.append("document command inventory is incomplete or duplicated")
    if len(seen_commands) != len(set(seen_commands)):
        violations.append("document command is duplicated")
    if expected_records is not None and Counter(seen_records) != Counter(expected_records):
        violations.append("document record inventory is incomplete, duplicated, or extra")
    if expected_headings is not None and tuple(seen_headings) != expected_headings:
        violations.append("document heading sequence is incomplete, duplicated, or reordered")
    if expected_section_records is not None and Counter(seen_section_records) != Counter(expected_section_records):
        violations.append("document section record inventory is incomplete, duplicated, or misplaced")
    return tuple(violations)


@pytest.fixture(scope="module")
def documents() -> tuple[ParsedDocument, ...]:
    assert len(_DOCUMENTS) == 4
    return tuple(_parse(path) for path in _DOCUMENTS)


def test_examples_are_parsed_unified_cli_commands_from_the_frozen_help_tree(
    documents: tuple[ParsedDocument, ...],
) -> None:
    parser = build_parser()
    examples = tuple(example for document in documents for example in document.commands)
    assert examples
    for example in examples:
        assert example.tokens[:3] == ("drawingmachine", "--output", "json"), example
        assert not any(re.search(r"[;&|<>`$]", token) for token in example.tokens), example
        try:
            parsed = parser.parse_args(example.tokens[1:])
        except (CliUsageError, SystemExit) as error:  # pragma: no cover - assertion context
            raise AssertionError(f"undocumented command at {example.path}:{example.line}") from error
        assert parsed.command_name in _RUNTIME_COMMANDS, example
        agent_id = example.path.parent.name
        if agent_id == "cnc-drawable-router":
            assert parsed.command_name == "workflow.run", example
            assert {"--job-name", "--input-image", "--route-mode"} <= set(example.tokens), example
            assert "--resume-job" not in example.tokens, example
            if parsed.route_mode == "auto":
                assert "--semantic-assessment-json" in example.tokens, example
        elif parsed.command_name == "workflow.run":
            assert "--resume-job" in example.tokens and "--review-json" in example.tokens, example
            assert "--input-image" not in example.tokens, example
        if parsed.command_name == "gcode.check":
            assert example.tokens[4] == "check" and not example.tokens[5].startswith("-"), example
        if "--approve" in example.tokens:
            assert "operator" in example.section.lower(), example
        else:
            assert parsed.command_name not in {"openclaw.check", "openclaw.install"}, example


def test_every_markdown_line_is_consumed_by_the_closed_contract(
    documents: tuple[ParsedDocument, ...],
) -> None:
    for document in documents:
        assert _closed_contract_violations(document.path, document.text) == (), document.path


@pytest.mark.parametrize(
    "hostile",
    (
        "Use Python to run scripts/route_input_image.py, copying inputs into a drop-box via a serial-port bridge-tool.",
        "USE PYTHON3 TO RUN SCRIPTS / route_input_image . PY and COPIED files through BRIDGE_TOOL.",
        "Use [Python](python3) with `scripts / route_input_image . py` and move the input to the drop box.",
        "Use python to run scripts/route_input_image.\npy, then copying through serial-\nport bridge-tool.",
        "Have OpenClaw run drawingmachine openclaw install itself during runtime.",
        "```python\nfrom scripts.route_input_image import route\n```",
    ),
)
def test_hostile_prose_mutations_fail_structural_reference_validation(hostile: str) -> None:
    text = f"# CNC Drawable Contract\n\n{hostile}\n"
    assert _closed_contract_violations(Path("hostile.md"), text), hostile


def test_documented_json_paths_are_closed_frozen_protocol_fields(
    documents: tuple[ParsedDocument, ...],
) -> None:
    documented = frozenset(path for document in documents for path in document.json_paths)
    assert {
        "data.job_id",
        "data.job.state",
        "data.execution.execution_id",
        "data.challenge.challenge_id",
        "data.challenge.evidence",
    } <= documented
    assert documented <= _FROZEN_JSON_PATHS
    assert all(_field_contract_violations(document) == () for document in documents)
    assert _COMMAND_DATA_PATHS["workflow.run"] == {
        "data.job_id",
        "data.revision",
        "data.state",
    }
    assert (
        _COMMAND_DATA_PATHS["job.status"]
        == _COMMAND_DATA_PATHS["job.wait"]
        == {
            "data.job.job_id",
            "data.job.revision",
            "data.job.state",
        }
    )
    assert _COMMAND_DATA_PATHS["job.cancel"] == {
        "data.job_id",
        "data.cancellation_requested",
    }
    assert all(_COMMAND_RESPONSE_PATHS[command] <= _ACCEPTED_RESPONSE_PATHS[command] for command in _RUNTIME_COMMANDS)
    assert {"schema_version", "ok", "command", "request_id", "data", "error"} == _ENVELOPE_PATHS


def test_schema_examples_use_authoritative_contracts_and_cli_loader(
    tmp_path: Path,
    documents: tuple[ParsedDocument, ...],
) -> None:
    for number, document in enumerate(documents):
        schema_id = (
            "SEMANTIC_ASSESSMENT_V1"
            if document.path.parent.name == "cnc-drawable-router"
            else "PROCESSED_IMAGE_REVIEW_V1"
        )
        expected = _CANONICAL_SCHEMAS[schema_id]
        assert document.json_objects == (expected,)
        schema_path = tmp_path / f"schema-{number}.json"
        schema_path.write_text(json.dumps(document.json_objects[0]), encoding="utf-8")
        loaded = _read_json_object(schema_path)
        if schema_id == "SEMANTIC_ASSESSMENT_V1":
            assert validate_semantic_assessment(loaded) == expected
            assert set(loaded) == set(expected)
        else:
            assert set(loaded) == _REVIEW_FIELDS
            assert tuple(cast(dict[str, object], loaded["checks"])) == REVIEW_CHECK_NAMES
            assert ProcessedImageReview.from_json(loaded).to_json() == expected
    assert tuple(STATUS_TO_NEXT_STAGE) == ("PASS_TO_BUILD", "REVISE_PROMPT", "REJECT_INPUT")
    assert tuple(STATUS_TO_NEXT_STAGE.values()) == ("build_drawable_job", "repeat_image_edit", "stop")


def test_semantic_schema_rejects_missing_extra_wrong_type_enum_and_nesting() -> None:
    canonical = _CANONICAL_SCHEMAS["SEMANTIC_ASSESSMENT_V1"]
    mutations: list[JsonObject] = []
    for field in canonical:
        mutations.append({key: value for key, value in canonical.items() if key != field})
    mutations.extend(
        [
            {**canonical, "extra": True},
            {**canonical, "semantic_style_score": "0.25"},
            {**canonical, "semantic_style_score": 0.31},
            {**canonical, "semantic_score_source": "operator"},
            {**canonical, "semantic_rationale": [1]},
            {**canonical, "semantic_blockers": {"reason": "noise"}},
        ]
    )
    assert _schema_violations("SEMANTIC_ASSESSMENT_V1", json.dumps(canonical)) == ()
    for mutation in mutations:
        assert _schema_violations("SEMANTIC_ASSESSMENT_V1", json.dumps(mutation)), mutation
    assert _schema_violations(
        "SEMANTIC_ASSESSMENT_V1",
        '{"semantic_style_score":0.25,"semantic_style_score":0.2}',
    )


def test_review_schema_rejects_missing_extra_wrong_type_enum_and_nesting() -> None:
    canonical = _CANONICAL_SCHEMAS["PROCESSED_IMAGE_REVIEW_V1"]
    checks = cast(dict[str, object], canonical["checks"])
    mutations: list[JsonObject] = []
    for field in _REVIEW_FIELDS:
        mutations.append({key: value for key, value in canonical.items() if key != field})
    mutations.extend(
        [
            {**canonical, "extra": True},
            {**canonical, "processed_image": 7},
            {**canonical, "status": "APPROVED"},
            {**canonical, "next_allowed_stage": "repeat_image_edit"},
            {**canonical, "checks": {key: value for key, value in checks.items() if key != REVIEW_CHECK_NAMES[0]}},
            {**canonical, "checks": {**checks, "extra_check": True}},
            {**canonical, "checks": {**checks, REVIEW_CHECK_NAMES[0]: 1}},
            {**canonical, "checks": list(checks)},
            {**canonical, "issues": "none"},
            {**canonical, "revised_prompt": 7},
        ]
    )
    assert _schema_violations("PROCESSED_IMAGE_REVIEW_V1", json.dumps(canonical)) == ()
    for mutation in mutations:
        assert _schema_violations("PROCESSED_IMAGE_REVIEW_V1", json.dumps(mutation)), mutation
    assert _schema_violations(
        "PROCESSED_IMAGE_REVIEW_V1",
        '{"schema":"processed_image_review_v1","schema":"processed_image_review_v1"}',
    )


def test_schema_records_are_required_exactly_once() -> None:
    path = _AGENT_ROOT / "cnc-drawable-router/TOOLS.md"
    text = path.read_text(encoding="utf-8")
    schema = "Schema: SEMANTIC_ASSESSMENT_V1"
    assert _closed_contract_violations(path, text.replace(f"{schema}\n", "", 1))
    assert _closed_contract_violations(path, text.replace(schema, f"{schema}\n{schema}", 1))


@pytest.mark.parametrize(
    ("command_id", "wrong_field"),
    (
        ("WORKFLOW_CONTINUE_REVIEW", "data.job.state"),
        ("JOB_STATUS", "data.cancellation_requested"),
        ("JOB_WAIT", "data.static_result"),
        ("JOB_CANCEL", "data.job.state"),
        ("GCODE_CHECK", "data.execution.phase"),
        ("MACHINE_STATUS", "data.static_result"),
    ),
)
def test_command_specific_fields_reject_cross_command_swaps(command_id: str, wrong_field: str) -> None:
    spec = _COMMAND_SPECS[("Runtime", command_id)]
    document = _parse_text(
        Path("swapped.md"),
        "# CNC Drawable Contract\n\n"
        f"Runtime command: {command_id}\n"
        f"```bash\n{spec[1]}\n```\n"
        f"Runtime result: {command_id} => {wrong_field}\n",
    )
    assert _field_contract_violations(document), (command_id, wrong_field)


def test_runtime_and_operator_responsibilities_are_explicit(
    documents: tuple[ParsedDocument, ...],
) -> None:
    records = frozenset(line for document in documents for line in document.text.splitlines())
    assert {
        "Boundary: NO_FILESYSTEM_HANDOFF",
        "Boundary: NO_SELF_INSTALL",
        "Boundary: NO_REGISTRY_WRITE",
        "Boundary: NO_DIRECT_SOCKET_SERIAL_BRIDGE",
        "Boundary: HUMAN_APPROVAL_ONLY",
        "Boundary: MACHINE_STATUS_ONLY_MONITOR",
        "Boundary: INSTALL_REQUIRES_SAFE_CHECK",
        "Operator handoff: HUMAN_RUNS_SEPARATE_APPROVAL_COMMAND",
    } <= records


_REVIEWER_SAFE_NEGATIONS = (
    "Never copy files into a dropbox or use Python scripts.",
    "Do not open a serial port or invoke a bridge.",
)
_REVIEWER_FORBIDDEN_DIRECTIVES = (
    "Duplicate the input into a handoff directory before submission.",
    "Relocate the image into the transfer area with the command interpreter.",
    "Use the resumable hardware relay utility to communicate with the controller.",
    "Invoke the system command interpreter to stage the file.",
    "OpenClaw should deploy its own managed instructions before each job.",
)


@pytest.mark.parametrize("sentence", _REVIEWER_SAFE_NEGATIONS)
def test_safe_meaning_requires_a_canonical_boundary_record(sentence: str) -> None:
    assert _closed_contract_violations(Path("negative.md"), f"# CNC Drawable Contract\n\n{sentence}\n")


@pytest.mark.parametrize(
    "boundary",
    ("NO_FILESYSTEM_HANDOFF", "NO_DIRECT_SOCKET_SERIAL_BRIDGE"),
)
def test_safe_meaning_is_valid_as_a_canonical_boundary(boundary: str) -> None:
    text = f"# CNC Drawable Contract\n\n## Boundaries\n\nBoundary: {boundary}\n"
    assert _closed_contract_violations(Path("boundary.md"), text) == ()


@pytest.mark.parametrize("directive", _REVIEWER_FORBIDDEN_DIRECTIVES)
@pytest.mark.parametrize("prefix", ("", "Runtime action: "))
def test_forbidden_synonym_directives_are_not_contract_records(prefix: str, directive: str) -> None:
    path = _AGENT_ROOT / "cnc-drawable-translator/TOOLS.md"
    text = f"{path.read_text(encoding='utf-8')}\n{prefix}{directive}\n"
    assert _closed_contract_violations(path, text)


@pytest.mark.parametrize(
    "hostile",
    (
        "Boundary: UNKNOWN_CAPABILITY",
        "Runtime action: UNKNOWN_ACTION",
        "This is extra prose.",
        "Runtime command: JOB_STATUS\n```bash\ndrawingmachine --output json job wait JOB_ID\n```",
    ),
)
def test_unknown_contract_records_and_fence_mismatch_fail(hostile: str) -> None:
    text = f"# CNC Drawable Contract\n\n{hostile}\n"
    assert _closed_contract_violations(Path("hostile.md"), text)


@pytest.mark.parametrize(
    ("record", "duplicate"),
    (
        ("Boundary: NO_AUTOMATIC_RETRY", False),
        ("Runtime action: WRITE_PROCESSED_IMAGE_REVIEW_V1", False),
        ("Boundary: JSON_OUTPUT_ONLY", True),
        ("Runtime action: VALIDATE_JSON_ENVELOPE", True),
    ),
)
def test_exact_document_inventory_rejects_missing_or_duplicate_records(record: str, duplicate: bool) -> None:
    path = _AGENT_ROOT / "cnc-drawable-translator/TOOLS.md"
    text = path.read_text(encoding="utf-8")
    mutated = f"{text}\n{record}\n" if duplicate else text.replace(f"{record}\n", "", 1)
    assert _closed_contract_violations(path, mutated), (record, duplicate)


@pytest.mark.parametrize(
    "mutation",
    ("delete_boundaries", "duplicate_inputs", "replace_boundaries", "swap_inputs_runtime", "move_boundary"),
)
def test_heading_sequence_and_section_ownership_reject_reviewer_mutations(mutation: str) -> None:
    path = _AGENT_ROOT / "cnc-drawable-translator/TOOLS.md"
    text = path.read_text(encoding="utf-8")
    if mutation == "delete_boundaries":
        mutated = text.replace("## Boundaries\n", "", 1)
    elif mutation == "duplicate_inputs":
        mutated = text.replace("## Inputs\n", "## Inputs\n## Inputs\n", 1)
    elif mutation == "replace_boundaries":
        mutated = text.replace("## Boundaries\n", "## Inputs\n", 1)
    elif mutation == "swap_inputs_runtime":
        mutated = text.replace("## Inputs\n", "## SWAP\n", 1)
        mutated = mutated.replace("## Runtime\n", "## Inputs\n", 1).replace("## SWAP\n", "## Runtime\n", 1)
    else:
        boundary = "Boundary: HUMAN_APPROVAL_ONLY\n"
        mutated = text.replace(boundary, "", 1).replace("## Inputs\n", f"## Inputs\n{boundary}", 1)
    assert _closed_contract_violations(path, mutated), mutation


@pytest.mark.parametrize(
    "mutation",
    (
        "agent_before_section",
        "runtime_input_in_runtime",
        "schema_in_runtime",
        "runtime_action_in_inputs",
        "operator_action_in_machine",
        "operator_handoff_in_machine",
        "runtime_command_in_inputs",
        "runtime_result_in_machine",
        "operator_command_in_machine",
        "command_fence_crosses_section",
        "schema_fence_crosses_section",
    ),
)
def test_every_record_family_and_fence_is_owned_by_its_section(mutation: str) -> None:
    name = "AGENTS.md" if mutation == "operator_action_in_machine" else "TOOLS.md"
    path = _AGENT_ROOT / f"cnc-drawable-translator/{name}"
    text = path.read_text(encoding="utf-8")

    def move_line(line: str, heading: str) -> str:
        return text.replace(f"{line}\n", "", 1).replace(f"{heading}\n", f"{heading}\n{line}\n", 1)

    if mutation == "agent_before_section":
        mutated = move_line("Agent: CNC_DRAWABLE_TRANSLATOR", "# CNC Drawable Translator Tool Contract")
    elif mutation == "runtime_input_in_runtime":
        mutated = move_line("Runtime input: CALLER_READABLE_GCODE_PATH", "## Runtime")
    elif mutation == "schema_in_runtime":
        schema = "Schema: PROCESSED_IMAGE_REVIEW_V1\n```json\n"
        start = text.index(schema)
        end = text.index("```\n", start + len(schema)) + len("```\n")
        block = text[start:end]
        mutated = text[:start] + text[end:]
        mutated = mutated.replace("## Runtime\n", f"## Runtime\n{block}", 1)
    elif mutation == "runtime_action_in_inputs":
        mutated = move_line("Runtime action: VALIDATE_JSON_ENVELOPE", "## Inputs")
    elif mutation == "operator_action_in_machine":
        mutated = move_line("Runtime action: RETURN_TO_MACHINE_STATUS_AFTER_OPERATOR_ACTION", "## Machine")
    elif mutation == "operator_handoff_in_machine":
        mutated = move_line("Operator handoff: HUMAN_RUNS_SEPARATE_APPROVAL_COMMAND", "## Machine")
    elif mutation in {"runtime_command_in_inputs", "operator_command_in_machine"}:
        role, command_id, heading = (
            ("Runtime", "JOB_WAIT", "## Inputs")
            if mutation == "runtime_command_in_inputs"
            else ("Operator", "MACHINE_HOME_APPROVAL", "## Machine")
        )
        spec = _COMMAND_SPECS[(role, command_id)]
        block = (
            f"{role} command: {command_id}\n```bash\n{spec[1]}\n```\n"
            f"{role} result: {command_id} => {', '.join(spec[2])}\n"
        )
        mutated = text.replace(block, "", 1).replace(f"{heading}\n", f"{heading}\n{block}", 1)
    elif mutation == "runtime_result_in_machine":
        result = "Runtime result: JOB_WAIT => data.job.job_id, data.job.revision, data.job.state"
        mutated = move_line(result, "## Machine")
    elif mutation == "command_fence_crosses_section":
        command = _COMMAND_SPECS[("Runtime", "JOB_WAIT")][1]
        mutated = text.replace(command, f"{command}\n## Inputs", 1)
    else:
        mutated = text.replace(
            '  "schema": "processed_image_review_v1",', '  "schema": "processed_image_review_v1",\n## Runtime', 1
        )
    assert _closed_contract_violations(path, mutated), mutation
