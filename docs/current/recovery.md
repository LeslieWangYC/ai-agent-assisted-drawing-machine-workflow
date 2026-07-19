# Current Recovery

## Status and authority

Offline code-completion guidance only. Live cutover, account/group/ACL, user-unit, OpenClaw registry, serial, and hardware actions require separate authorization.

Recovery preserves uncertainty and authority; it is not a shortcut back to execution. No automatic retry or resume is authorized.

## Staging recovery

Input staging uses bounded leases, stable payload identity/size/SHA, atomic publication, service-side schema-v4 admission, and private claimed storage. Reconciliation runs at startup, shutdown, periodically, and before/after commands. It compares active requests, durable admissions, prepare/drop bundles, private observations, boot ID, monotonic deadlines, and filesystem identity.

An orphaned partial-frame staging lease is reconciled or blocks its role. Safe expired/aborted material is removed; uncertain, corrupt, colliding, or identity-drifted material is quarantined or produces durable recovery evidence. A role with unresolved evidence returns `STAGING_RECOVERY_REQUIRED`; it is never silently accepted, replayed, or exposed through caller XDG data. Cleanup failure remains visible and blocks authority.

## Job and service recovery

On startup the service migrates through schema 5, acquires its single-instance lease, reconciles staging, jobs, machine ownership, OpenClaw/runtime policy, then accepts commands. Durable request IDs make exact replay return the prior result; changed arguments conflict. Worker crash/timeout/cancellation and incomplete artifacts fail or block the job according to its durable state. Review continuation requires explicit review input and the same job; it is not automatic resume.

A client timeout does not prove that a committed request failed. Query the durable job/machine status by ID, inspect the closed error/recovery evidence, and choose only an allowed explicit action. Do not delete SQLite rows, edit artifacts, copy a replacement into managed roots, or restart in hope of repeating work.

## Machine recovery

Any crash in an in-progress phase, session reset/loss, ambiguous acknowledgement, controller alarm/error/timeout, service shutdown, or post-milestone failure closes the owned session and persists `RECOVERY_REQUIRED` while retaining the global machine latch.

Recovery intent is determined by the durable stream milestone:

- `PRE_STREAM_RESTART`: before the first stream write; an explicitly approved `restart-sequence` may transfer the latch to one successor that starts again at HOME.
- `STREAM_AMBIGUOUS_RELEASE_ONLY`: from first stream write until final acknowledgement and Idle are proven; only an explicitly approved non-motion `release` is legal. No successor can STREAM.
- `POST_STREAM_SAFE_HOME`: STREAM_CONFIRMED is irreversible; an explicitly approved `safe-home` successor may perform one full HOME and complete, but cannot enter ZCAL, ZCONFIRM, or STREAM. `release` remains a non-motion option.

Every recovery challenge binds the exact disposition. Release opens no serial connection, preserves evidence, retires the owner, and leaves the job recovery-required. A service restart reconstructs the same intent from durable milestone/evidence; it never changes the intent or dispatches motion.

## Operator procedure

Offline code-completion may inspect temporary service JSON and test each state with fake transports. A later live recovery requires separate authorization to assess physical/controller state, choose `release`, `restart-sequence`, or `safe-home`, request the matching challenge, review its evidence, and consume it as the configured operator. If state is contradictory or physical outcome cannot be bounded, stop and retain the latch.

See [machine safety](machine-safety.md) and [CLI reference](cli-reference.md).
