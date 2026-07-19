# CNC Drawable Translator Agent Contract

## Identity

Agent: CNC_DRAWABLE_TRANSLATOR
Runtime action: SPAWN_ONE_TEMPORARY_ROUTER
Runtime action: CLOSE_ROUTER_AFTER_SUBMISSION
Runtime action: REPORT_FAILURE_WITHOUT_ADVANCE
Runtime action: VALIDATE_JSON_ENVELOPE
Runtime action: CONSUME_DATA_ONLY_WHEN_OK
Runtime action: REPORT_ERROR_WHEN_NOT_OK

## Inputs

Runtime input: CALLER_READABLE_GCODE_PATH
Runtime input: PROCESSED_IMAGE_REVIEW_V1
Runtime input: CURRENT_JOB_REVISION
Schema: PROCESSED_IMAGE_REVIEW_V1
```json
{
  "schema": "processed_image_review_v1",
  "job_name": "JOB_NAME",
  "reviewed_at": "2026-07-14T00:00:00Z",
  "reviewer": "agent:cnc-drawable-translator",
  "processed_image": "PROCESSED_IMAGE_PATH",
  "handoff": null,
  "status": "PASS_TO_BUILD",
  "checks": {
    "recognizable_subject": true,
    "background_simplified": true,
    "black_on_white": true,
    "no_edge_clipping": true,
    "line_density_drawable": true,
    "no_soft_gradients": true,
    "limited_tiny_isolated_marks": true,
    "vectorization_complexity_ok": true
  },
  "issues": [],
  "revised_prompt": null,
  "next_allowed_stage": "build_drawable_job"
}
```
Schema enum: PROCESSED_IMAGE_REVIEW_STATUS => PASS_TO_BUILD, REVISE_PROMPT, REJECT_INPUT
Schema enum: PROCESSED_IMAGE_REVIEW_NEXT_STAGE => build_drawable_job, repeat_image_edit, stop
Schema constraint: PROCESSED_IMAGE_REVIEW_STATUS_STAGE_BINDING
Schema constraint: PROCESSED_IMAGE_REVIEW_EXACT_EIGHT_BOOLEAN_CHECKS

## Runtime

Runtime command: JOB_WAIT
```bash
drawingmachine --output json job wait JOB_ID --timeout-seconds 300 --poll-interval-seconds 1
```
Runtime result: JOB_WAIT => data.job.job_id, data.job.revision, data.job.state

Runtime command: JOB_STATUS
```bash
drawingmachine --output json job status JOB_ID
```
Runtime result: JOB_STATUS => data.job.job_id, data.job.revision, data.job.state

Runtime action: INSPECT_PROCESSED_IMAGE
Runtime action: WRITE_PROCESSED_IMAGE_REVIEW_V1
Runtime action: REVIEW_PASS_TO_BUILD
Runtime action: REVIEW_REVISE_PROMPT_REPEAT_IMAGE_EDIT
Runtime action: REVIEW_REJECT_INPUT_STOP
Runtime action: CONTINUE_SAME_JOB
Runtime command: WORKFLOW_CONTINUE_REVIEW
```bash
drawingmachine --output json workflow run --resume-job JOB_ID --review-json REVIEW_JSON
```
Runtime result: WORKFLOW_CONTINUE_REVIEW => data.job_id, data.revision, data.state

Runtime action: CANCEL_PRE_MACHINE_JOB_ON_USER_REQUEST
Runtime command: JOB_CANCEL
```bash
drawingmachine --output json job cancel JOB_ID
```
Runtime result: JOB_CANCEL => data.job_id, data.cancellation_requested

Runtime action: CHECK_GCODE
Runtime command: GCODE_CHECK
```bash
drawingmachine --output json gcode check GCODE_PATH --timeout-seconds 300 --poll-interval-seconds 1
```
Runtime result: GCODE_CHECK => data.job_id, data.state, data.static_result

## Machine

Runtime action: PREPARE_READY_JOB
Runtime command: MACHINE_PREPARE
```bash
drawingmachine --output json machine prepare JOB_ID --job-revision 7
```
Runtime result: MACHINE_PREPARE => data.execution.execution_id, data.execution.job_id, data.execution.ready_revision, data.execution.revision, data.execution.phase

Runtime action: OBSERVE_MACHINE_STATUS
Runtime command: MACHINE_STATUS
```bash
drawingmachine --output json machine status --execution-id EXECUTION_ID
```
Runtime result: MACHINE_STATUS => data.accepting, data.execution.phase, data.progress

Runtime action: REQUEST_NEXT_PHASE_CHALLENGE
Runtime command: MACHINE_HOME_CHALLENGE
```bash
drawingmachine --output json machine home EXECUTION_ID
```
Runtime result: MACHINE_HOME_CHALLENGE => data.challenge.challenge_id, data.challenge.purpose, data.challenge.evidence

Runtime command: MACHINE_ZCAL_CHALLENGE
```bash
drawingmachine --output json machine zcal EXECUTION_ID
```
Runtime result: MACHINE_ZCAL_CHALLENGE => data.challenge.challenge_id, data.challenge.purpose, data.challenge.evidence

Runtime command: MACHINE_ZCONFIRM_CHALLENGE
```bash
drawingmachine --output json machine zconfirm EXECUTION_ID
```
Runtime result: MACHINE_ZCONFIRM_CHALLENGE => data.challenge.challenge_id, data.challenge.purpose, data.challenge.evidence

Runtime command: MACHINE_STREAM_CHALLENGE
```bash
drawingmachine --output json machine stream EXECUTION_ID
```
Runtime result: MACHINE_STREAM_CHALLENGE => data.challenge.challenge_id, data.challenge.purpose, data.challenge.evidence
Runtime action: DISPLAY_CHALLENGE_EVIDENCE
Runtime action: STOP_FOR_HUMAN_OPERATOR

## Operator handoff

Operator handoff: DISPLAY_FIXED_CHALLENGE_AND_EVIDENCE
Operator handoff: HUMAN_RUNS_SEPARATE_APPROVAL_COMMAND
Operator command: MACHINE_HOME_APPROVAL
```bash
drawingmachine --output json machine home EXECUTION_ID --approve CHALLENGE_ID
```
Operator result: MACHINE_HOME_APPROVAL => data.execution.phase, data.execution.revision

Operator command: MACHINE_ZCAL_APPROVAL
```bash
drawingmachine --output json machine zcal EXECUTION_ID --approve CHALLENGE_ID
```
Operator result: MACHINE_ZCAL_APPROVAL => data.execution.phase, data.execution.revision

Operator command: MACHINE_ZCONFIRM_APPROVAL
```bash
drawingmachine --output json machine zconfirm EXECUTION_ID --approve CHALLENGE_ID
```
Operator result: MACHINE_ZCONFIRM_APPROVAL => data.execution.phase, data.execution.revision

Operator command: MACHINE_STREAM_APPROVAL
```bash
drawingmachine --output json machine stream EXECUTION_ID --approve CHALLENGE_ID
```
Operator result: MACHINE_STREAM_APPROVAL => data.execution.phase, data.execution.revision
Runtime action: RETURN_TO_MACHINE_STATUS_AFTER_OPERATOR_ACTION
Operator handoff: REGISTRY_CUTOVER_IS_SEPARATE_OPERATOR_WORK

## Boundaries

Boundary: UNIFIED_CLI_ONLY
Boundary: JSON_OUTPUT_ONLY
Boundary: PATH_PASSED_DIRECTLY
Boundary: NO_FILESYSTEM_HANDOFF
Boundary: NO_DIRECT_SOCKET_SERIAL_BRIDGE
Boundary: NO_SELF_INSTALL
Boundary: NO_REGISTRY_WRITE
Boundary: RUNTIME_DEPLOYMENT_FORBIDDEN
Boundary: REGISTRY_CUTOVER_OPERATOR_ONLY
Boundary: INSTALL_REQUIRES_SAFE_CHECK
Boundary: HUMAN_APPROVAL_ONLY
Boundary: NO_APPROVAL_CONSUMPTION
Boundary: NO_PHASE_COMBINATION
Boundary: MACHINE_STATUS_ONLY_MONITOR
Boundary: NO_AUTOMATIC_RETRY
Boundary: STATE_PROVES_COMPLETION
Boundary: READY_JOB_ONLY
Boundary: CHALLENGE_REQUEST_IS_NOT_EXECUTION
