import pytest

from drawingmachine.domain.jobs import JobKind, JobState, allowed_transition


def test_job_states_have_stable_wire_values() -> None:
    assert tuple(state.value for state in JobState) == (
        "QUEUED",
        "PREPARING_IMAGE",
        "IMAGE_READY",
        "PLANNING_PATHS",
        "PATH_PLAN_READY",
        "BUILDING_GCODE",
        "VALIDATING_GCODE",
        "READY_TO_RUN",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
        "RECOVERY_REQUIRED",
        "COMPLETED",
    )
    assert tuple(kind.value for kind in JobKind) == ("OFFLINE_WORKFLOW", "GCODE_CHECK")


@pytest.mark.parametrize(
    ("prior", "result"),
    [
        (JobState.QUEUED, JobState.PREPARING_IMAGE),
        (JobState.PREPARING_IMAGE, JobState.IMAGE_READY),
        (JobState.IMAGE_READY, JobState.PLANNING_PATHS),
        (JobState.PLANNING_PATHS, JobState.PATH_PLAN_READY),
        (JobState.PATH_PLAN_READY, JobState.BUILDING_GCODE),
        (JobState.BUILDING_GCODE, JobState.VALIDATING_GCODE),
        (JobState.VALIDATING_GCODE, JobState.READY_TO_RUN),
    ],
)
def test_normal_progression(prior: JobState, result: JobState) -> None:
    assert allowed_transition(prior, result, blocker_code=None)


def test_gcode_check_progression_requires_explicit_job_kind() -> None:
    assert allowed_transition(
        JobState.QUEUED,
        JobState.VALIDATING_GCODE,
        blocker_code=None,
        job_kind=JobKind.GCODE_CHECK,
    )
    for result in (JobState.COMPLETED, JobState.BLOCKED, JobState.FAILED, JobState.CANCELLED):
        assert allowed_transition(
            JobState.VALIDATING_GCODE,
            result,
            blocker_code=None,
            job_kind=JobKind.GCODE_CHECK,
        )


def test_gcode_check_queued_state_cannot_take_blocked_shortcut() -> None:
    assert not allowed_transition(
        JobState.QUEUED,
        JobState.BLOCKED,
        blocker_code=None,
        job_kind=JobKind.GCODE_CHECK,
    )


def test_gcode_check_queued_state_can_fail_closed_for_restart_or_post_ack_import_failure() -> None:
    assert allowed_transition(
        JobState.QUEUED,
        JobState.FAILED,
        blocker_code=None,
        job_kind=JobKind.GCODE_CHECK,
    )


def test_gcode_check_blocked_and_completed_states_cannot_fail() -> None:
    for prior in (JobState.BLOCKED, JobState.COMPLETED):
        assert not allowed_transition(
            prior,
            JobState.FAILED,
            blocker_code=None,
            job_kind=JobKind.GCODE_CHECK,
        )


@pytest.mark.parametrize(
    ("prior", "result"),
    [
        (JobState.QUEUED, JobState.VALIDATING_GCODE),
        (JobState.VALIDATING_GCODE, JobState.COMPLETED),
    ],
)
def test_gcode_check_progression_is_not_legal_for_default_offline_jobs(
    prior: JobState,
    result: JobState,
) -> None:
    assert not allowed_transition(prior, result, blocker_code=None)


@pytest.mark.parametrize(
    ("prior", "result"),
    [
        (JobState.QUEUED, JobState.IMAGE_READY),
        (JobState.PREPARING_IMAGE, JobState.PLANNING_PATHS),
        (JobState.IMAGE_READY, JobState.PATH_PLAN_READY),
        (JobState.PATH_PLAN_READY, JobState.VALIDATING_GCODE),
    ],
)
def test_offline_workflow_cannot_skip_stages(prior: JobState, result: JobState) -> None:
    assert not allowed_transition(prior, result, blocker_code=None)


@pytest.mark.parametrize(
    "prior",
    [
        JobState.READY_TO_RUN,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.RECOVERY_REQUIRED,
        JobState.COMPLETED,
    ],
)
def test_terminal_states_cannot_be_left(prior: JobState) -> None:
    assert not allowed_transition(prior, JobState.QUEUED, blocker_code=None)


def test_only_review_required_blocker_can_continue() -> None:
    assert allowed_transition(JobState.BLOCKED, JobState.IMAGE_READY, blocker_code="REVIEW_REQUIRED")
    assert not allowed_transition(JobState.BLOCKED, JobState.IMAGE_READY, blocker_code="STATIC_CHECK_FAILED")
    assert not allowed_transition(JobState.BLOCKED, JobState.IMAGE_READY, blocker_code=None)
    assert not allowed_transition(
        JobState.BLOCKED,
        JobState.IMAGE_READY,
        blocker_code="REVIEW_REQUIRED",
        job_kind=JobKind.GCODE_CHECK,
    )


def test_review_blocked_offline_job_can_fail_closed_when_persisted_artifacts_are_damaged() -> None:
    assert allowed_transition(
        JobState.BLOCKED,
        JobState.FAILED,
        blocker_code="REVIEW_REQUIRED",
        job_kind=JobKind.OFFLINE_WORKFLOW,
    )
    assert not allowed_transition(
        JobState.BLOCKED,
        JobState.FAILED,
        blocker_code="STATIC_CHECK_FAILED",
        job_kind=JobKind.OFFLINE_WORKFLOW,
    )
    assert not allowed_transition(
        JobState.BLOCKED,
        JobState.FAILED,
        blocker_code="REVIEW_REQUIRED",
        job_kind=JobKind.GCODE_CHECK,
    )


@pytest.mark.parametrize("prior", list(JobState))
@pytest.mark.parametrize("kind", list(JobKind))
def test_package_b_never_produces_recovery_required(prior: JobState, kind: JobKind) -> None:
    assert not allowed_transition(prior, JobState.RECOVERY_REQUIRED, blocker_code=None, job_kind=kind)


def test_success_terminals_are_job_kind_specific() -> None:
    assert not allowed_transition(
        JobState.VALIDATING_GCODE,
        JobState.READY_TO_RUN,
        blocker_code=None,
        job_kind=JobKind.GCODE_CHECK,
    )
    assert not allowed_transition(
        JobState.VALIDATING_GCODE,
        JobState.COMPLETED,
        blocker_code=None,
        job_kind=JobKind.OFFLINE_WORKFLOW,
    )


def test_allowed_transition_is_exhaustively_isolated_by_job_kind() -> None:
    offline_progression = {
        (JobState.QUEUED, JobState.PREPARING_IMAGE),
        (JobState.PREPARING_IMAGE, JobState.IMAGE_READY),
        (JobState.IMAGE_READY, JobState.PLANNING_PATHS),
        (JobState.PLANNING_PATHS, JobState.PATH_PLAN_READY),
        (JobState.PATH_PLAN_READY, JobState.BUILDING_GCODE),
        (JobState.BUILDING_GCODE, JobState.VALIDATING_GCODE),
        (JobState.VALIDATING_GCODE, JobState.READY_TO_RUN),
        (JobState.BLOCKED, JobState.IMAGE_READY),
    }
    offline_active = {
        JobState.QUEUED,
        JobState.PREPARING_IMAGE,
        JobState.IMAGE_READY,
        JobState.PLANNING_PATHS,
        JobState.PATH_PLAN_READY,
        JobState.BUILDING_GCODE,
        JobState.VALIDATING_GCODE,
    }
    offline_expected = offline_progression | {
        (prior, result)
        for prior in offline_active
        for result in (JobState.BLOCKED, JobState.FAILED, JobState.CANCELLED)
    }
    offline_expected.update(
        {
            (JobState.BLOCKED, JobState.FAILED),
            (JobState.BLOCKED, JobState.CANCELLED),
            (JobState.READY_TO_RUN, JobState.CANCELLED),
        }
    )
    gcode_expected = {
        (JobState.QUEUED, JobState.VALIDATING_GCODE),
        (JobState.QUEUED, JobState.FAILED),
        (JobState.VALIDATING_GCODE, JobState.COMPLETED),
        (JobState.VALIDATING_GCODE, JobState.BLOCKED),
        (JobState.VALIDATING_GCODE, JobState.FAILED),
        (JobState.VALIDATING_GCODE, JobState.CANCELLED),
        (JobState.QUEUED, JobState.CANCELLED),
        (JobState.BLOCKED, JobState.CANCELLED),
    }

    for kind, expected in (
        (JobKind.OFFLINE_WORKFLOW, offline_expected),
        (JobKind.GCODE_CHECK, gcode_expected),
    ):
        for prior in JobState:
            for result in JobState:
                blocker_code = "REVIEW_REQUIRED" if prior is JobState.BLOCKED else None
                assert allowed_transition(prior, result, blocker_code, job_kind=kind) is (
                    (prior, result) in expected
                ), f"unexpected {kind.value} transition: {prior.value} -> {result.value}"
