# Current Machine Safety

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

Package C code completion is not live hardware acceptance. Tests prove typed behavior with fakes; a real machine still needs separately recorded deployment, electrical, mechanical, calibration, and per-action approval.

## Approval boundary

Only a READY_TO_RUN job at its immutable revision and exact G-code digest can be prepared. The machine profile fixes disjoint automation and operator principals. Automation may request a challenge, but automation cannot consume operator approval; JSON identity and PID are not identity authority.

HOME, ZCAL, ZCONFIRM, STREAM, and HOME_Z each require a distinct one-time approval. RECOVER has its own challenge bound to one disposition. Each challenge binds the execution/revision, exact action and prior phase, kernel requester/operator principal, service and machine-session epochs, job/revision, application/machine/provider digests, G-code digest, issue time, monotonic deadline, and durable status. Expired, consumed, superseded, wrong-action, wrong-phase, wrong-principal, old-epoch, changed-job/digest, malformed, or duplicate consumption fails before adapter I/O.

Request mode returns a typed purpose/evidence summary and performs no adapter action. Execute mode atomically consumes the exact challenge and commits the in-progress phase/audit before dispatch. A changed CLI process PID is audit data, not a changed principal.

## Motion milestones

Normal order is:

```text
PREPARING_SESSION -> AWAITING_HOME_APPROVAL -> HOMING
-> AWAITING_ZCAL_APPROVAL -> Z_CALIBRATING
-> AWAITING_ZCONFIRM_APPROVAL -> Z_CONFIRMING
-> AWAITING_STREAM_APPROVAL -> STREAMING
-> [AWAITING_HOME_Z_APPROVAL -> HOMING_Z] -> COMPLETED
```

The coordinator owns one active execution, one non-daemon thread, and one same-session typed FluidNC connection. Initial preflight is read-only. HOME must prove Idle and configured homed MPos/G54; ZCAL uses the fixed sequence; ZCONFIRM proves the calibrated pose; STREAM accepts only a completely validated immutable stream program. Optional HOME_Z is separately approved and is never implied by STREAM.

The durable milestone progresses from `NOT_STARTED` to `FIRST_WRITE_POSSIBLE` to `STREAM_CONFIRMED`. `FIRST_WRITE_POSSIBLE` is committed before the adapter call that may emit the first line. STREAM_CONFIRMED is irreversible: no recovery graph can stream the job again. Each in-progress phase is durable before I/O, so a crash or ambiguous controller result enters recovery rather than guessing success.

## Closed hardware surface

No raw hardware command surface exists. The port exposes semantic preflight, HOME, Z calibration/confirmation, validated stream, optional HOME_Z, and close only. There is no raw write/send, bridge, jog, unlock, offset mutation, arbitrary arm phrase, line resume, reconnect-and-continue, or configuration-supplied command.

The serial adapter is lazy: service startup/reconciliation cannot open a device. Unexpected reset/banner, transport loss, alarm, timeout, error, close, pose mismatch, or acknowledgement ambiguity closes the session and records recovery. It never auto-reopens.

## Separate live acceptance

Before any real connection, separately approve and verify real disjoint principals, socket policy, machine profile digest, serial device identity, controller/firmware, safe area, high-voltage state, homed coordinates, work offset, paper/tool/calibration conditions, and emergency-stop procedure. Then request and consume each action challenge individually. A prior milestone or approval never authorizes the next action, another job, or a recovery successor.

For recovery restrictions, see [recovery](recovery.md).
