from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from drawingmachine.domain.openclaw import (
    OpenClawCheckFileV1,
    OpenClawCheckResultV1,
    OpenClawCheckStatus,
    OpenClawDeploymentError,
    OpenClawInitialState,
    OpenClawInstallFileStatus,
    OpenClawInstallFileV1,
    OpenClawInstallResultV1,
    OpenClawRegistryCheckV1,
    OpenClawRegistryStatus,
)


def test_result_rows_are_closed_deeply_immutable_and_content_free() -> None:
    checked = OpenClawCheckFileV1(
        destination="agents/cnc-drawable-router/AGENTS.md",
        expected_sha256="a" * 64,
        observed_sha256=None,
        status=OpenClawCheckStatus.MISSING,
    )
    installed = OpenClawInstallFileV1(
        destination=checked.destination,
        initial_state=OpenClawInitialState.ABSENT,
        initial_sha256=None,
        status=OpenClawInstallFileStatus.INSTALLED,
        replaced=False,
        restored=False,
        cleanup_completed=True,
        error_code=None,
    )

    assert checked.to_json() == {
        "schema_version": 1,
        "destination": checked.destination,
        "expected_sha256": "a" * 64,
        "observed_sha256": None,
        "status": "MISSING",
    }
    assert installed.to_json()["status"] == "INSTALLED"
    assert "contents" not in checked.to_json() | installed.to_json()
    with pytest.raises(FrozenInstanceError):
        checked.status = OpenClawCheckStatus.MATCHES  # type: ignore[misc]


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: OpenClawCheckFileV1("../escape", "a" * 64, None, OpenClawCheckStatus.MISSING),
            "destination",
        ),
        (
            lambda: OpenClawInstallFileV1(
                "agents/x",
                OpenClawInitialState.ABSENT,
                "a" * 64,
                OpenClawInstallFileStatus.INSTALLED,
                False,
                False,
                True,
                None,
            ),
            "initial digest",
        ),
    ],
)
def test_result_rows_reject_contradictory_or_unsafe_values(factory: object, match: str) -> None:
    with pytest.raises(TypeError, match=match):
        factory()  # type: ignore[operator]


def test_complete_check_and_install_results_project_closed_json() -> None:
    destinations = (
        "agents/cnc-drawable-translator/AGENTS.md",
        "agents/cnc-drawable-translator/TOOLS.md",
        "agents/cnc-drawable-router/AGENTS.md",
        "agents/cnc-drawable-router/TOOLS.md",
    )
    checked = tuple(
        OpenClawCheckFileV1(destination, "a" * 64, "a" * 64, OpenClawCheckStatus.MATCHES)
        for destination in destinations
    )
    registry = OpenClawRegistryCheckV1("b" * 64, OpenClawRegistryStatus.MATCHES)
    check = OpenClawCheckResultV1(True, False, registry, checked)
    installed = tuple(
        OpenClawInstallFileV1(
            destination,
            OpenClawInitialState.PRESENT,
            "c" * 64,
            OpenClawInstallFileStatus.REPLACED,
            True,
            False,
            True,
            None,
        )
        for destination in destinations
    )
    result = OpenClawInstallResultV1(True, True, False, True, False, None, True, installed)

    assert check.to_json()["registry"] == registry.to_json()
    assert result.to_json()["globally_atomic"] is False
    assert result.to_json()["files"] == [row.to_json() for row in installed]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OpenClawCheckFileV1("agents/x/A\n", "a" * 64, None, OpenClawCheckStatus.MISSING),
        lambda: OpenClawCheckFileV1("agents/x/A", "bad", None, OpenClawCheckStatus.MISSING),
        lambda: OpenClawCheckFileV1("agents/x/A", "a" * 64, "bad", OpenClawCheckStatus.DIFFERS),
        lambda: OpenClawCheckFileV1("agents/x/A", "a" * 64, "a" * 64, OpenClawCheckStatus.MISSING),
        lambda: OpenClawRegistryCheckV1("bad", OpenClawRegistryStatus.MATCHES),
        lambda: OpenClawCheckResultV1(
            False, False, OpenClawRegistryCheckV1("a" * 64, OpenClawRegistryStatus.MATCHES), ()
        ),
        lambda: OpenClawCheckResultV1(
            False, False, OpenClawRegistryCheckV1("a" * 64, OpenClawRegistryStatus.UNSAFE), ()
        ),
        lambda: OpenClawInstallFileV1(
            "agents/x/A",
            OpenClawInitialState.PRESENT,
            None,
            OpenClawInstallFileStatus.FAILED,
            False,
            False,
            False,
            "bad-code",
        ),
        lambda: OpenClawInstallResultV1(True, False, False, False, False, None, True, ()),
        lambda: OpenClawInstallResultV1(False, False, False, True, True, None, True, ()),
        lambda: OpenClawInstallResultV1(False, False, False, True, False, True, True, ()),
        lambda: OpenClawInstallResultV1(False, False, False, True, False, None, True, (), globally_atomic=True),
    ],
)
def test_closed_result_models_reject_all_summary_contradictions(factory: object) -> None:
    with pytest.raises(TypeError):
        factory()  # type: ignore[operator]


def test_deployment_error_is_bounded_and_carries_recovery_fact() -> None:
    error = OpenClawDeploymentError("PARTIAL_INSTALL", "recovery required", recovery_required=True)
    assert error.code == "PARTIAL_INSTALL"
    assert error.recovery_required is True
    with pytest.raises(TypeError, match="canonical"):
        OpenClawDeploymentError("bad", "no")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OpenClawCheckFileV1("agents/x/A", "a" * 64, None, "MISSING"),  # type: ignore[arg-type]
        lambda: OpenClawRegistryCheckV1("a" * 64, "MATCHES"),  # type: ignore[arg-type]
        lambda: OpenClawCheckResultV1(1, False, OpenClawRegistryCheckV1("a" * 64, OpenClawRegistryStatus.MATCHES), ()),  # type: ignore[arg-type]
        lambda: OpenClawCheckResultV1(True, False, object(), ()),  # type: ignore[arg-type]
        lambda: OpenClawCheckResultV1(
            True, False, OpenClawRegistryCheckV1("a" * 64, OpenClawRegistryStatus.MATCHES), []
        ),  # type: ignore[arg-type]
        lambda: OpenClawInstallFileV1(
            "agents/x/A", "ABSENT", None, OpenClawInstallFileStatus.FAILED, False, False, True, None
        ),  # type: ignore[arg-type]
        lambda: OpenClawInstallFileV1(
            "agents/x/A", OpenClawInitialState.ABSENT, None, "FAILED", False, False, True, None
        ),  # type: ignore[arg-type]
        lambda: OpenClawInstallFileV1(
            "agents/x/A", OpenClawInitialState.ABSENT, None, OpenClawInstallFileStatus.FAILED, 0, False, True, None
        ),  # type: ignore[arg-type]
        lambda: OpenClawInstallFileV1(
            "agents/x/A", OpenClawInitialState.ABSENT, None, OpenClawInstallFileStatus.FAILED, False, False, True, "bad"
        ),
        lambda: OpenClawInstallResultV1(0, False, False, True, False, None, True, ()),  # type: ignore[arg-type]
        lambda: OpenClawInstallResultV1(False, False, False, True, True, "yes", True, ()),  # type: ignore[arg-type]
        lambda: OpenClawInstallResultV1(False, False, False, True, False, None, True, []),  # type: ignore[arg-type]
        lambda: OpenClawCheckFileV1("", "a" * 64, None, OpenClawCheckStatus.MISSING),
    ],
)
def test_exact_type_guards_reject_python_coercions(factory: object) -> None:
    with pytest.raises(TypeError):
        factory()  # type: ignore[operator]


def test_i2_install_dtos_reject_wrong_cardinality_and_status_flag_contradictions() -> None:
    row = OpenClawInstallFileV1(
        "agents/cnc-drawable-translator/AGENTS.md",
        OpenClawInitialState.ABSENT,
        None,
        OpenClawInstallFileStatus.INSTALLED,
        False,
        False,
        True,
        None,
    )
    with pytest.raises(TypeError, match="four ordered"):
        OpenClawInstallResultV1(True, True, False, True, False, None, True, (row,))
    with pytest.raises(TypeError, match="status"):
        OpenClawInstallFileV1(
            row.destination,
            OpenClawInitialState.ABSENT,
            None,
            OpenClawInstallFileStatus.NOT_ATTEMPTED,
            True,
            False,
            True,
            "INSTALL_FAILED",
        )


@pytest.mark.parametrize(
    ("status", "state", "replaced", "restored", "cleanup", "error"),
    [
        (OpenClawInstallFileStatus.UNCHANGED, OpenClawInitialState.PRESENT, True, False, True, None),
        (OpenClawInstallFileStatus.INSTALLED, OpenClawInitialState.PRESENT, False, False, True, None),
        (OpenClawInstallFileStatus.REPLACED, OpenClawInitialState.ABSENT, False, False, True, None),
        (OpenClawInstallFileStatus.RESTORED_PRESENT, OpenClawInitialState.PRESENT, False, True, True, "FAILED"),
        (OpenClawInstallFileStatus.RESTORED_ABSENT, OpenClawInitialState.ABSENT, False, False, True, "FAILED"),
        (OpenClawInstallFileStatus.FAILED, OpenClawInitialState.PRESENT, False, False, True, None),
        (OpenClawInstallFileStatus.CLEANUP_FAILED, OpenClawInitialState.PRESENT, False, False, True, "FAILED"),
        (OpenClawInstallFileStatus.FAILED, OpenClawInitialState.PRESENT, False, True, True, "FAILED"),
        (OpenClawInstallFileStatus.FAILED, OpenClawInitialState.ABSENT, True, False, True, "FAILED"),
    ],
)
def test_i2_each_file_status_rejects_its_own_contradictory_flags(
    status: OpenClawInstallFileStatus,
    state: OpenClawInitialState,
    replaced: bool,
    restored: bool,
    cleanup: bool,
    error: str | None,
) -> None:
    with pytest.raises(TypeError, match=r"status|failed"):
        OpenClawInstallFileV1(
            "agents/cnc-drawable-translator/AGENTS.md",
            state,
            "a" * 64 if state is OpenClawInitialState.PRESENT else None,
            status,
            replaced,
            restored,
            cleanup,
            error,
        )


def test_i2_result_rejects_each_global_contradiction() -> None:
    destinations = (
        "agents/cnc-drawable-translator/AGENTS.md",
        "agents/cnc-drawable-translator/TOOLS.md",
        "agents/cnc-drawable-router/AGENTS.md",
        "agents/cnc-drawable-router/TOOLS.md",
    )
    installed = tuple(
        OpenClawInstallFileV1(
            destination,
            OpenClawInitialState.ABSENT,
            None,
            OpenClawInstallFileStatus.INSTALLED,
            False,
            False,
            True,
            None,
        )
        for destination in destinations
    )
    unchanged = tuple(
        OpenClawInstallFileV1(
            destination,
            OpenClawInitialState.ABSENT,
            None,
            OpenClawInstallFileStatus.UNCHANGED,
            False,
            False,
            True,
            None,
        )
        for destination in destinations
    )
    not_attempted = tuple(
        OpenClawInstallFileV1(
            destination,
            OpenClawInitialState.ABSENT,
            None,
            OpenClawInstallFileStatus.NOT_ATTEMPTED,
            False,
            False,
            True,
            None,
        )
        for destination in destinations
    )
    restored = tuple(
        OpenClawInstallFileV1(
            destination,
            OpenClawInitialState.ABSENT,
            None,
            OpenClawInstallFileStatus.RESTORED_ABSENT,
            False,
            True,
            True,
            "INSTALL_FAILED",
        )
        for destination in destinations
    )
    invalid_results = (
        (True, True, False, False, False, None, True, installed),
        (True, True, False, True, False, None, True, (installed[0], *not_attempted[1:])),
        (True, False, False, True, False, None, True, installed),
        (True, True, False, True, True, True, True, installed),
        (False, True, False, True, True, True, False, restored),
        (False, False, False, True, False, None, True, unchanged),
    )
    for arguments in invalid_results:
        with pytest.raises(TypeError):
            OpenClawInstallResultV1(*arguments)  # type: ignore[arg-type]


def test_i2a_check_result_requires_four_ordered_rows_and_exact_status_digests() -> None:
    registry = OpenClawRegistryCheckV1("a" * 64, OpenClawRegistryStatus.MATCHES)
    destinations = (
        "agents/cnc-drawable-translator/AGENTS.md",
        "agents/cnc-drawable-translator/TOOLS.md",
        "agents/cnc-drawable-router/AGENTS.md",
        "agents/cnc-drawable-router/TOOLS.md",
    )
    matching = tuple(
        OpenClawCheckFileV1(destination, "b" * 64, "b" * 64, OpenClawCheckStatus.MATCHES)
        for destination in destinations
    )
    assert OpenClawCheckResultV1(True, False, registry, matching).in_sync is True
    with pytest.raises(TypeError, match="four ordered"):
        OpenClawCheckResultV1(True, False, registry, matching[:-1])
    with pytest.raises(TypeError, match="four ordered"):
        OpenClawCheckResultV1(True, False, registry, tuple(reversed(matching)))
    with pytest.raises(TypeError, match="summary"):
        OpenClawCheckResultV1(False, False, registry, matching)

    contradictions = (
        ("b" * 64, "c" * 64, OpenClawCheckStatus.MATCHES),
        ("b" * 64, "b" * 64, OpenClawCheckStatus.DIFFERS),
        ("b" * 64, None, OpenClawCheckStatus.DIFFERS),
        ("b" * 64, "b" * 64, OpenClawCheckStatus.UNSAFE),
    )
    for expected, observed, status in contradictions:
        with pytest.raises(TypeError, match="status"):
            OpenClawCheckFileV1(destinations[0], expected, observed, status)
