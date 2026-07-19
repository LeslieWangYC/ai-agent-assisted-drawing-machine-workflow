# Current CLI Reference

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

This table is checked against the parser installed from a fresh wheel. Put the global option before the command group.

## Global output

`--output {text,json}` selects output. JSON mode writes one closed JSON object on stdout, a trailing newline, and no success diagnostics on stderr. The object has exact protocol/schema versions, success flag, command, request ID, data, and nullable error. OpenClaw automation must always use JSON mode.

## Installed command surface

Command | Optional flags shown by installed help
--- | ---
`drawingmachine config check` | `none`
`drawingmachine service run` | `none`
`drawingmachine service status` | `none`
`drawingmachine workflow run` | `--input-image --job-name --prompt --prompt-file --resume-job --review-json --route-mode --semantic-assessment-json`
`drawingmachine job status` | `none`
`drawingmachine job wait` | `--poll-interval-seconds --timeout-seconds`
`drawingmachine job cancel` | `none`
`drawingmachine gcode check` | `--poll-interval-seconds --timeout-seconds`
`drawingmachine machine status` | `--execution-id`
`drawingmachine machine prepare` | `--job-revision`
`drawingmachine machine home` | `--approve`
`drawingmachine machine zcal` | `--approve`
`drawingmachine machine zconfirm` | `--approve`
`drawingmachine machine stream` | `--approve`
`drawingmachine machine home-z` | `--approve`
`drawingmachine machine recover` | `--approve --disposition`
`drawingmachine openclaw check` | `--openclaw-root`
`drawingmachine openclaw install` | `--openclaw-root`

Required positionals and choices remain part of installed help: workflow route mode is `auto|direct|image-edit`; job commands take a job ID; G-code check takes a path; machine prepare takes a job ID and required revision; action commands take an execution ID; recovery requires `release|restart-sequence|safe-home`; OpenClaw requires exactly one canonical absolute root.

## Workflow contracts

- Initial submission requires `--job-name`, `--input-image`, and `--route-mode`.
- `auto` additionally requires `--semantic-assessment-json`.
- Resume requires both `--resume-job` and `--review-json` and rejects every initial-submission argument.
- `--prompt` and `--prompt-file` are mutually exclusive.
- `--review-json` without `--resume-job` is invalid. Resume reads that JSON locally; it does not stage the review file.

## Offline examples

These examples use ordinary caller-readable local paths; the unchanged public flags trigger automatic private staging, request publication, and service claim without exposing a protocol path field:

```text
drawingmachine --output json workflow run --job-name example --input-image ./input.png --route-mode direct
drawingmachine --output json gcode check ./candidate.gcode
drawingmachine --output json job status <job-id>
```

They require a separately provisioned service to succeed. Running them during code completion is permitted only against the guarded temporary test service.

## Machine approvals

Omitting `--approve` requests an opaque one-time challenge and performs no motion. Supplying the exact returned challenge asks the service to consume it for the same action, execution/revision, principal, epochs, configuration digests, G-code digest, and deadline. Status never returns a consumable challenge. See [machine safety](machine-safety.md).

## OpenClaw commands

`openclaw check` is operator-only and zero-write; `openclaw install` is operator-only and service-command-only. Both send the canonical root through the socket; the CLI contains no deployment writer. See [OpenClaw operation](openclaw-operation.md).
