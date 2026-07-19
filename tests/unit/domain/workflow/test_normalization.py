import io

from PIL import Image

from drawingmachine.domain.workflow import DirectImageConfig, NormalizationConfig, normalize_image, prepare_direct_image


def test_normalize_image_returns_deterministic_black_white_png() -> None:
    image = Image.new("RGB", (4, 2), "white")
    image.putpixel((0, 0), (0, 0, 0))
    image.putpixel((1, 0), (255, 0, 0))
    config = NormalizationConfig(remove_small_components_px=0)

    first = normalize_image(image, config)
    second = normalize_image(image, config)

    assert first.png_bytes == second.png_bytes
    decoded = Image.open(io.BytesIO(first.png_bytes))
    assert decoded.mode == "L"
    colors = decoded.getcolors()
    assert colors is not None
    assert {value for _count, value in colors} <= {0, 255}
    assert first.report["input_colored_pixels"] == 1


def test_prepare_direct_image_preserves_legacy_component_filtering() -> None:
    image = Image.new("L", (8, 8), 255)
    image.putpixel((1, 1), 0)
    image.paste(0, (3, 3, 7, 7))

    prepared = prepare_direct_image(
        image,
        DirectImageConfig(threshold=128, invert=False, min_component_area_px=8, median_filter_size=1),
    )

    decoded = Image.open(io.BytesIO(prepared.png_bytes))
    assert decoded.getpixel((1, 1)) == 255
    assert decoded.getpixel((4, 4)) == 0
    assert prepared.report["components_before"] == 2
    assert prepared.report["components_after"] == 1
    assert prepared.report["small_component_pixels_removed"] == 1
