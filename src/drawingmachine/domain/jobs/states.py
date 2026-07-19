from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "QUEUED"
    PREPARING_IMAGE = "PREPARING_IMAGE"
    IMAGE_READY = "IMAGE_READY"
    PLANNING_PATHS = "PLANNING_PATHS"
    PATH_PLAN_READY = "PATH_PLAN_READY"
    BUILDING_GCODE = "BUILDING_GCODE"
    VALIDATING_GCODE = "VALIDATING_GCODE"
    READY_TO_RUN = "READY_TO_RUN"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    COMPLETED = "COMPLETED"


class JobKind(StrEnum):
    OFFLINE_WORKFLOW = "OFFLINE_WORKFLOW"
    GCODE_CHECK = "GCODE_CHECK"


_OFFLINE_PROGRESSION = frozenset(
    {
        (JobState.QUEUED, JobState.PREPARING_IMAGE),
        (JobState.PREPARING_IMAGE, JobState.IMAGE_READY),
        (JobState.IMAGE_READY, JobState.PLANNING_PATHS),
        (JobState.PLANNING_PATHS, JobState.PATH_PLAN_READY),
        (JobState.PATH_PLAN_READY, JobState.BUILDING_GCODE),
        (JobState.BUILDING_GCODE, JobState.VALIDATING_GCODE),
        (JobState.VALIDATING_GCODE, JobState.READY_TO_RUN),
    }
)
_GCODE_CHECK_PROGRESSION = frozenset(
    {
        (JobState.QUEUED, JobState.VALIDATING_GCODE),
        (JobState.QUEUED, JobState.FAILED),
        (JobState.VALIDATING_GCODE, JobState.COMPLETED),
        (JobState.VALIDATING_GCODE, JobState.BLOCKED),
        (JobState.VALIDATING_GCODE, JobState.FAILED),
        (JobState.VALIDATING_GCODE, JobState.CANCELLED),
    }
)
_FAIL_CLOSED_RESULTS = frozenset({JobState.BLOCKED, JobState.FAILED, JobState.CANCELLED})
_TERMINAL_STATES = frozenset(
    {
        JobState.READY_TO_RUN,
        JobState.BLOCKED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.RECOVERY_REQUIRED,
        JobState.COMPLETED,
    }
)
_OFFLINE_JOB_STATES = frozenset(JobState)
_GCODE_CHECK_JOB_STATES = frozenset(
    {
        JobState.QUEUED,
        JobState.VALIDATING_GCODE,
        JobState.COMPLETED,
        JobState.BLOCKED,
        JobState.FAILED,
        JobState.CANCELLED,
    }
)


def _job_states(job_kind: JobKind) -> frozenset[JobState]:
    return _GCODE_CHECK_JOB_STATES if job_kind is JobKind.GCODE_CHECK else _OFFLINE_JOB_STATES


def allowed_transition(
    prior: JobState,
    result: JobState,
    blocker_code: str | None,
    *,
    job_kind: JobKind = JobKind.OFFLINE_WORKFLOW,
) -> bool:
    if type(prior) is not JobState or type(result) is not JobState or type(job_kind) is not JobKind:
        return False
    valid_states = _job_states(job_kind)
    if prior not in valid_states or result not in valid_states:
        return False
    if result is JobState.CANCELLED:
        return prior not in {
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.RECOVERY_REQUIRED,
            JobState.COMPLETED,
        }
    if job_kind is JobKind.GCODE_CHECK:
        return (prior, result) in _GCODE_CHECK_PROGRESSION
    if prior is JobState.BLOCKED:
        return blocker_code == "REVIEW_REQUIRED" and result in {JobState.IMAGE_READY, JobState.FAILED}
    if prior in _TERMINAL_STATES:
        return False
    if result in _FAIL_CLOSED_RESULTS:
        return True
    return (prior, result) in _OFFLINE_PROGRESSION
