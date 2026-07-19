# CNC Drawable Router Agent Contract

## Identity

Agent: CNC_DRAWABLE_ROUTER
Runtime action: INSPECT_INPUT_IMAGE
Runtime action: SCORE_PEN_LINEWORK_SUITABILITY
Runtime action: WRITE_SEMANTIC_ASSESSMENT_V1
Runtime action: SEMANTIC_SCORE_RANGE_0_TO_0_30
Runtime action: SEMANTIC_SCORE_USES_IMAGE_NOT_PROMPT
Runtime action: CONTENT_CATEGORY_IS_NOT_A_BLOCKER
Runtime action: SELECT_AUTO_ROUTE_BY_SEMANTIC_ASSESSMENT
Runtime action: SELECT_DIRECT_ROUTE_ONLY_FOR_EXPLICIT_PRESERVATION
Runtime action: SELECT_IMAGE_EDIT_ROUTE_ONLY_FOR_EXPLICIT_REDRAW
Runtime action: SUBMIT_EXACTLY_ONE_WORKFLOW
Runtime action: RETURN_JSON_TO_TRANSLATOR_AND_STOP
Runtime action: VALIDATE_JSON_ENVELOPE
Runtime action: CONSUME_DATA_ONLY_WHEN_OK
Runtime action: REPORT_ERROR_WHEN_NOT_OK

## Inputs

Runtime input: CALLER_READABLE_IMAGE_PATH
Runtime input: SEMANTIC_ASSESSMENT_V1
Schema: SEMANTIC_ASSESSMENT_V1
```json
{
  "semantic_style_score": 0.25,
  "semantic_score_source": "router_multimodal_model",
  "semantic_rationale": [
    "clear subject contour",
    "low detail density"
  ],
  "semantic_blockers": []
}
```
Schema enum: SEMANTIC_SCORE_SOURCE => router_multimodal_model
Schema constraint: SEMANTIC_STYLE_SCORE_0_TO_0_30
Schema constraint: SEMANTIC_RATIONALE_STRING_ARRAY
Schema constraint: SEMANTIC_BLOCKERS_STRING_ARRAY

## Runtime

Runtime command: WORKFLOW_SUBMIT_AUTO
```bash
drawingmachine --output json workflow run --job-name JOB_NAME --input-image IMAGE_PATH --route-mode auto --semantic-assessment-json ASSESSMENT_JSON
```
Runtime result: WORKFLOW_SUBMIT_AUTO => data.job_id, data.revision, data.state

Runtime command: WORKFLOW_SUBMIT_DIRECT
```bash
drawingmachine --output json workflow run --job-name JOB_NAME --input-image IMAGE_PATH --route-mode direct
```
Runtime result: WORKFLOW_SUBMIT_DIRECT => data.job_id, data.revision, data.state

Runtime command: WORKFLOW_SUBMIT_IMAGE_EDIT
```bash
drawingmachine --output json workflow run --job-name JOB_NAME --input-image IMAGE_PATH --route-mode image-edit
```
Runtime result: WORKFLOW_SUBMIT_IMAGE_EDIT => data.job_id, data.revision, data.state

## Operator handoff

Operator handoff: ROUTER_RETURNS_JSON_TO_TRANSLATOR

## Boundaries

Boundary: UNIFIED_CLI_ONLY
Boundary: JSON_OUTPUT_ONLY
Boundary: PATH_PASSED_DIRECTLY
Boundary: NO_FILESYSTEM_HANDOFF
Boundary: NO_DIRECT_SOCKET_SERIAL_BRIDGE
Boundary: NO_DIRECT_PROVIDER_CALL
Boundary: NO_JOB_ARTIFACT_MUTATION
Boundary: NO_DUPLICATE_SUBMISSION
Boundary: ROUTER_SINGLE_USE
Boundary: NO_APPROVAL_CONSUMPTION
Boundary: RUNTIME_DEPLOYMENT_FORBIDDEN
