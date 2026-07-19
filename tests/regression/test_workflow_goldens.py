import json
from pathlib import Path

from PIL import Image

from drawingmachine.domain.workflow import (
    ComplexityLimits,
    NormalizationConfig,
    RouteContext,
    analyze_complexity,
    decide_route,
    normalize_image,
)

GOLDEN_ROOT = Path("tests/fixtures/package_b/golden")
EXPECTED = GOLDEN_ROOT / "expected"


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_route_golden_is_exact() -> None:
    with Image.open(GOLDEN_ROOT / "outline.png") as image:
        result = decide_route(
            image,
            read_json(GOLDEN_ROOT / "route_semantic.json"),
            context=RouteContext(
                job_name="package-b-golden",
                input_image="<FIXTURE_ROOT>/outline.png",
                decided_at="<CAPTURED_AT>",
            ),
        )

    assert result.to_json() == read_json(EXPECTED / "route.json")


def test_normalized_png_and_report_goldens_are_exact() -> None:
    with Image.open(GOLDEN_ROOT / "complex_gradient.png") as image:
        result = normalize_image(
            image,
            NormalizationConfig(
                created_at="<CAPTURED_AT>",
                input_path="<FIXTURE_ROOT>/complex_gradient.png",
                output_path="<FIXTURE_ROOT>/expected/normalized.png",
            ),
        )

    assert result.png_bytes == (EXPECTED / "normalized.png").read_bytes()
    assert result.to_json() == read_json(EXPECTED / "normalize_report.json")


def test_complexity_golden_is_exact() -> None:
    with Image.open(EXPECTED / "normalized.png") as image:
        result = analyze_complexity(
            image,
            ComplexityLimits(
                created_at="<CAPTURED_AT>",
                input_path="<FIXTURE_ROOT>/expected/normalized.png",
            ),
        )

    assert result.to_json() == read_json(EXPECTED / "complexity.json")
