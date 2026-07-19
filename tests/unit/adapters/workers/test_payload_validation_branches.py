from __future__ import annotations

from typing import Any

import pytest

from drawingmachine.adapters.workers.tasks import _payload_validation as validation
from drawingmachine.errors import DrawingMachineError
from drawingmachine.ports.workers import WorkerKind, WorkerTaskV1


@pytest.fixture
def task(tmp_path: Any) -> WorkerTaskV1:
    staging = tmp_path / "staging"
    staging.mkdir()
    return WorkerTaskV1(
        1,
        "task",
        "job",
        0,
        "attempt",
        WorkerKind.TEST_ECHO,
        (),
        {"application": "a" * 64},
        str(staging),
        {"mode": "echo", "nested": []},
    )


def test_exact_unknown_string_and_scalar_validation_branches(task: WorkerTaskV1) -> None:
    with pytest.raises(DrawingMachineError):
        validation._exact(task, {"a": 1}, frozenset({"b"}))
    with pytest.raises(DrawingMachineError):
        validation._no_unknown(task, {"a": 1}, frozenset())
    for value, kwargs in (
        ("\ud800", {}),
        ("x" * 5, {"maximum_bytes": 4}),
        ("line\n", {}),
    ):
        with pytest.raises(DrawingMachineError):
            validation._string(task, value, **kwargs)
    with pytest.raises(DrawingMachineError):
        validation._integer(task, 0, minimum=1, maximum=2)
    for value in ("bad", float("nan"), 3.0, 10**5000):
        with pytest.raises(DrawingMachineError):
            validation._number(task, value, minimum=0.0, maximum=2.0)
    with pytest.raises(DrawingMachineError):
        validation._boolean(task, 1)
    with pytest.raises(DrawingMachineError):
        validation._sequence(task, "bad")


def test_plain_json_and_normalization_branches(task: WorkerTaskV1) -> None:
    with pytest.raises(DrawingMachineError):
        validation._plain_json(task, float("nan"))
    assert validation._plain_json(task, {"a": [1, True]}) == {"a": [1, True]}
    with pytest.raises(DrawingMachineError):
        validation._plain_json(task, object())
    with pytest.raises(DrawingMachineError):
        validation._plain_object(task, [1])
    direct, normalized = validation._normalization_values(
        task,
        {
            "threshold": 100,
            "invert": True,
            "min_component_area_px": 2,
            "median_filter_size": 5,
            "color_delta_threshold": 3,
            "remove_small_components_px": 4,
        },
        direct=True,
    )
    assert direct is not None and direct.threshold == 100 and normalized.threshold == 100.0
    direct, normalized = validation._normalization_values(task, {"threshold": 120.5}, direct=False)
    assert direct is None and normalized.threshold == 120.5
    with pytest.raises(DrawingMachineError):
        validation._normalization_values(task, {"median_filter_size": 2}, direct=True)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://[bad",
        "ftp://example.test",
        "http:///missing",
        "http://user@example.test",
        "http://example.test?query=1",
        "http://example.test#fragment",
    ],
)
def test_provider_endpoint_rejects_each_authority_shape(task: WorkerTaskV1, endpoint: str) -> None:
    with pytest.raises(DrawingMachineError):
        validation._provider_endpoint(task, endpoint)


def test_validate_image_payload_semantic_and_mode_branches(task: WorkerTaskV1) -> None:
    def selected(payload: dict[str, object]) -> WorkerTaskV1:
        return WorkerTaskV1(
            1,
            "selected",
            "job",
            0,
            "attempt-selected",
            WorkerKind.IMAGE_PREPARE,
            (),
            {"application": "a" * 64},
            task.staging_dir,
            payload,
        )

    semantic = {
        "semantic_style_score": 0.1,
        "semantic_score_source": "router_multimodal_model",
        "semantic_rationale": ["ok"],
        "semantic_blockers": [],
    }
    validated = validation.validate_image_payload(
        selected(
            {
                "mode": "direct",
                "source_name": "input.png",
                "route_mode": "direct",
                "semantic_assessment": semantic,
                "normalization": {},
            }
        )
    )
    assert validated.semantic_assessment is not None
    with pytest.raises(DrawingMachineError):
        validation.validate_image_payload(
            selected(
                {
                    "mode": "direct",
                    "source_name": "input.png",
                    "route_mode": "direct",
                    "semantic_assessment": {**semantic, "semantic_score_source": "bad"},
                    "normalization": {},
                }
            )
        )
    with pytest.raises(DrawingMachineError):
        validation.validate_image_payload(task)
