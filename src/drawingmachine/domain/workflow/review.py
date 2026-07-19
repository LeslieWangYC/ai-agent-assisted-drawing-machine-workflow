from drawingmachine.domain.workflow.models import ProcessedImageReview
from drawingmachine.json_types import JsonObject


def parse_review(value: JsonObject) -> ProcessedImageReview:
    return ProcessedImageReview.from_json(value)


__all__ = ["parse_review"]
