CREATE TABLE machine_executions (
    execution_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    job_id TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE RESTRICT,
    ready_revision INTEGER NOT NULL CHECK (ready_revision >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    phase TEXT NOT NULL CHECK (phase IN (
        'PREPARING_SESSION', 'AWAITING_HOME_APPROVAL', 'HOMING',
        'AWAITING_ZCAL_APPROVAL', 'Z_CALIBRATING',
        'AWAITING_ZCONFIRM_APPROVAL', 'Z_CONFIRMING',
        'AWAITING_STREAM_APPROVAL', 'STREAMING',
        'AWAITING_HOME_Z_APPROVAL', 'HOMING_Z', 'COMPLETED',
        'RECOVERY_REQUIRED'
    )),
    service_epoch TEXT NOT NULL,
    machine_session_epoch TEXT NOT NULL UNIQUE,
    gcode_sha256 TEXT NOT NULL CHECK (length(gcode_sha256) = 64),
    application_version TEXT NOT NULL,
    application_digest TEXT NOT NULL CHECK (length(application_digest) = 64),
    machine_digest TEXT NOT NULL CHECK (length(machine_digest) = 64),
    provider_digest TEXT NOT NULL CHECK (length(provider_digest) = 64),
    stream_milestone TEXT NOT NULL CHECK (stream_milestone IN (
        'NOT_STARTED', 'FIRST_WRITE_POSSIBLE', 'STREAM_CONFIRMED'
    )),
    recovery_intent TEXT CHECK (recovery_intent IS NULL OR recovery_intent IN (
        'PRE_STREAM_RESTART', 'STREAM_AMBIGUOUS_RELEASE_ONLY', 'POST_STREAM_SAFE_HOME'
    )),
    predecessor_execution_id TEXT REFERENCES machine_executions (execution_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    recovery_evidence_json TEXT,
    retirement_disposition TEXT CHECK (retirement_disposition IS NULL OR retirement_disposition IN (
        'COMPLETED', 'RELEASED', 'TRANSFERRED'
    )),
    retired_at TEXT,
    successor_execution_id TEXT REFERENCES machine_executions (execution_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    outcome_kind TEXT CHECK (outcome_kind IS NULL OR outcome_kind IN ('COMPLETED', 'RECOVERY_REQUIRED')),
    outcome_json TEXT,
    outcome_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (predecessor_execution_id IS NULL OR predecessor_execution_id != execution_id),
    CHECK ((recovery_evidence_json IS NULL) = (phase != 'RECOVERY_REQUIRED')),
    CHECK ((retired_at IS NULL) = (retirement_disposition IS NULL)),
    CHECK ((retirement_disposition = 'TRANSFERRED') = (successor_execution_id IS NOT NULL)),
    CHECK ((outcome_kind IS NULL) = (outcome_json IS NULL)),
    CHECK ((outcome_kind IS NULL) = (outcome_at IS NULL)),
    UNIQUE (execution_id, machine_session_epoch)
);

CREATE UNIQUE INDEX machine_executions_one_global_active_owner
ON machine_executions ((1))
WHERE retired_at IS NULL AND phase != 'COMPLETED';

CREATE INDEX machine_executions_job_id ON machine_executions (job_id);

CREATE TABLE machine_sessions (
    execution_id TEXT PRIMARY KEY,
    machine_session_epoch TEXT NOT NULL UNIQUE,
    snapshot_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (execution_id, machine_session_epoch)
        REFERENCES machine_executions (execution_id, machine_session_epoch) ON DELETE RESTRICT
);

CREATE TABLE machine_approval_challenges (
    challenge_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES machine_executions (execution_id) ON DELETE RESTRICT,
    execution_revision INTEGER NOT NULL CHECK (execution_revision >= 0),
    action TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'CONSUMED', 'SUPERSEDED', 'EXPIRED')),
    challenge_json TEXT NOT NULL,
    requester_uid INTEGER NOT NULL CHECK (requester_uid >= 0),
    requester_gid INTEGER NOT NULL CHECK (requester_gid >= 0),
    requester_pid INTEGER NOT NULL CHECK (requester_pid > 0),
    operator_uid INTEGER NOT NULL CHECK (operator_uid >= 0),
    operator_gid INTEGER NOT NULL CHECK (operator_gid >= 0),
    consumer_uid INTEGER CHECK (consumer_uid IS NULL OR consumer_uid >= 0),
    consumer_gid INTEGER CHECK (consumer_gid IS NULL OR consumer_gid >= 0),
    consumer_pid INTEGER CHECK (consumer_pid IS NULL OR consumer_pid > 0),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status_changed_at TEXT,
    CHECK ((status = 'PENDING') = (status_changed_at IS NULL)),
    CHECK ((status = 'CONSUMED') = (consumer_uid IS NOT NULL)),
    CHECK ((consumer_uid IS NULL) = (consumer_gid IS NULL)),
    CHECK ((consumer_uid IS NULL) = (consumer_pid IS NULL)),
    CHECK (json_valid(challenge_json)),
    CHECK (json_extract(challenge_json, '$.challenge_id') = challenge_id),
    CHECK (json_extract(challenge_json, '$.binding.execution_id') = execution_id),
    CHECK (json_extract(challenge_json, '$.binding.execution_revision') = execution_revision),
    CHECK (json_extract(challenge_json, '$.binding.action') = action),
    CHECK (json_extract(challenge_json, '$.status') = status),
    CHECK (json_extract(challenge_json, '$.requester.uid') = requester_uid),
    CHECK (json_extract(challenge_json, '$.requester.gid') = requester_gid),
    CHECK (json_extract(challenge_json, '$.requester.pid') = requester_pid),
    CHECK (json_extract(challenge_json, '$.operator_principal.uid') = operator_uid),
    CHECK (json_extract(challenge_json, '$.operator_principal.gid') = operator_gid)
);

CREATE UNIQUE INDEX machine_approval_one_pending_action
ON machine_approval_challenges (execution_id, action)
WHERE status = 'PENDING';

CREATE TRIGGER machine_approval_current_execution_revision
BEFORE INSERT ON machine_approval_challenges
WHEN NOT EXISTS (
    SELECT 1 FROM machine_executions
    WHERE execution_id = NEW.execution_id
      AND revision = NEW.execution_revision
      AND service_epoch = json_extract(NEW.challenge_json, '$.binding.service_epoch')
      AND machine_session_epoch = json_extract(NEW.challenge_json, '$.binding.machine_session_epoch')
      AND job_id = json_extract(NEW.challenge_json, '$.binding.job_id')
      AND ready_revision = json_extract(NEW.challenge_json, '$.binding.ready_revision')
      AND application_digest = json_extract(NEW.challenge_json, '$.binding.application_digest')
      AND machine_digest = json_extract(NEW.challenge_json, '$.binding.machine_digest')
      AND provider_digest = json_extract(NEW.challenge_json, '$.binding.provider_digest')
      AND gcode_sha256 = json_extract(NEW.challenge_json, '$.binding.gcode_sha256')
      AND phase = json_extract(NEW.challenge_json, '$.binding.required_prior_phase')
      AND retired_at IS NULL
      AND phase != 'COMPLETED'
)
BEGIN
    SELECT RAISE(ABORT, 'machine approval must bind the current active execution revision');
END;

CREATE TABLE machine_events (
    event_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES machine_executions (execution_id) ON DELETE RESTRICT,
    execution_revision INTEGER NOT NULL CHECK (execution_revision >= 0),
    event_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE machine_progress_events (
    progress_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES machine_executions (execution_id) ON DELETE RESTRICT,
    execution_revision INTEGER NOT NULL CHECK (execution_revision >= 0),
    progress_json TEXT NOT NULL,
    last_event_at TEXT NOT NULL
);

CREATE TABLE machine_request_results (
    principal_uid INTEGER NOT NULL CHECK (principal_uid >= 0),
    principal_gid INTEGER NOT NULL CHECK (principal_gid >= 0),
    request_id TEXT NOT NULL,
    requester_pid INTEGER NOT NULL CHECK (requester_pid > 0),
    command TEXT NOT NULL,
    argument_digest TEXT NOT NULL CHECK (length(argument_digest) = 64),
    execution_id TEXT REFERENCES machine_executions (execution_id) ON DELETE RESTRICT,
    action TEXT,
    execution_revision INTEGER CHECK (execution_revision IS NULL OR execution_revision >= 0),
    service_epoch TEXT,
    machine_session_epoch TEXT,
    recovery_disposition TEXT CHECK (recovery_disposition IS NULL OR recovery_disposition IN (
        'RELEASE', 'RESTART_SEQUENCE', 'SAFE_HOME'
    )),
    recovery_intent TEXT CHECK (recovery_intent IS NULL OR recovery_intent IN (
        'PRE_STREAM_RESTART', 'STREAM_AMBIGUOUS_RELEASE_ONLY', 'POST_STREAM_SAFE_HOME'
    )),
    predecessor_execution_id TEXT REFERENCES machine_executions (execution_id) ON DELETE RESTRICT,
    admission_id TEXT UNIQUE,
    dispatch_nonce TEXT,
    response_json TEXT,
    reserved_at TEXT NOT NULL,
    completed_at TEXT,
    claimed_at TEXT,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    PRIMARY KEY (principal_uid, principal_gid, request_id),
    CHECK ((admission_id IS NULL) = (dispatch_nonce IS NULL)),
    CHECK ((execution_id IS NULL) = (action IS NULL)),
    CHECK ((execution_id IS NULL) = (execution_revision IS NULL)),
    CHECK ((execution_id IS NULL) = (service_epoch IS NULL)),
    CHECK ((execution_id IS NULL) = (machine_session_epoch IS NULL)),
    CHECK (admission_id IS NULL OR execution_id IS NOT NULL),
    CHECK (
        CASE WHEN action = 'RECOVER'
            THEN recovery_disposition IS NOT NULL AND recovery_intent IS NOT NULL
            ELSE recovery_disposition IS NULL
                 AND recovery_intent IS NULL
                 AND predecessor_execution_id IS NULL
        END
    ),
    CHECK (
        recovery_disposition IS NULL
        OR recovery_disposition = 'RELEASE'
        OR (recovery_disposition = 'RESTART_SEQUENCE' AND recovery_intent = 'PRE_STREAM_RESTART')
        OR (recovery_disposition = 'SAFE_HOME' AND recovery_intent = 'POST_STREAM_SAFE_HOME')
    ),
    CHECK (
        admission_id IS NULL
        OR action != 'RECOVER'
        OR (predecessor_execution_id IS NOT NULL AND recovery_disposition != 'RELEASE')
    ),
    CHECK ((response_json IS NULL) = (completed_at IS NULL)),
    CHECK (claimed_at IS NULL OR admission_id IS NOT NULL),
    CHECK ((invalidated_at IS NULL) = (invalidation_reason IS NULL)),
    CHECK (claimed_at IS NULL OR invalidated_at IS NULL)
);

CREATE TRIGGER machine_executions_revision_step
BEFORE UPDATE ON machine_executions
WHEN NEW.revision != OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'machine execution revision must advance by exactly one');
END;

CREATE TRIGGER machine_executions_initial_job_authority
BEFORE INSERT ON machine_executions
WHEN NEW.predecessor_execution_id IS NULL
 AND NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE job_id = NEW.job_id
      AND state = 'READY_TO_RUN'
      AND revision = NEW.ready_revision
 )
BEGIN
    SELECT RAISE(ABORT, 'initial machine execution requires READY_TO_RUN job authority');
END;

CREATE TRIGGER machine_executions_successor_job_authority
BEFORE INSERT ON machine_executions
WHEN NEW.predecessor_execution_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE job_id = NEW.job_id
      AND state = 'RECOVERY_REQUIRED'
      AND NEW.ready_revision < revision
 )
BEGIN
    SELECT RAISE(ABORT, 'successor machine execution requires RECOVERY_REQUIRED job authority');
END;

CREATE TRIGGER machine_executions_successor_frozen_authority
BEFORE INSERT ON machine_executions
WHEN NEW.predecessor_execution_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM machine_executions AS predecessor
    WHERE predecessor.execution_id = NEW.predecessor_execution_id
      AND predecessor.phase = 'RECOVERY_REQUIRED'
      AND predecessor.retirement_disposition = 'TRANSFERRED'
      AND predecessor.retired_at IS NOT NULL
      AND predecessor.successor_execution_id = NEW.execution_id
      AND predecessor.job_id = NEW.job_id
      AND predecessor.ready_revision = NEW.ready_revision
      AND predecessor.gcode_sha256 = NEW.gcode_sha256
      AND predecessor.application_version = NEW.application_version
      AND predecessor.application_digest = NEW.application_digest
      AND predecessor.machine_digest = NEW.machine_digest
      AND predecessor.provider_digest = NEW.provider_digest
      AND predecessor.stream_milestone = NEW.stream_milestone
      AND predecessor.recovery_intent = NEW.recovery_intent
      AND predecessor.recovery_intent IN ('PRE_STREAM_RESTART', 'POST_STREAM_SAFE_HOME')
      AND predecessor.machine_session_epoch != NEW.machine_session_epoch
      AND NEW.phase = 'PREPARING_SESSION'
      AND NEW.revision = 0
 )
BEGIN
    SELECT RAISE(ABORT, 'successor machine execution must preserve predecessor frozen authority');
END;

CREATE TRIGGER machine_execution_opened_session_required
BEFORE UPDATE ON machine_executions
WHEN OLD.phase = 'PREPARING_SESSION'
 AND NEW.phase = 'AWAITING_HOME_APPROVAL'
 AND NOT EXISTS (
    SELECT 1 FROM machine_sessions
    WHERE execution_id = OLD.execution_id
      AND machine_session_epoch = OLD.machine_session_epoch
 )
BEGIN
    SELECT RAISE(ABORT, 'PREPARING_SESSION transition requires one observed machine session');
END;

CREATE TRIGGER machine_executions_permanent_bindings
BEFORE UPDATE ON machine_executions
WHEN NEW.execution_id IS NOT OLD.execution_id
 OR NEW.schema_version IS NOT OLD.schema_version
 OR NEW.job_id IS NOT OLD.job_id
 OR NEW.ready_revision IS NOT OLD.ready_revision
 OR NEW.service_epoch IS NOT OLD.service_epoch
 OR NEW.machine_session_epoch IS NOT OLD.machine_session_epoch
 OR NEW.gcode_sha256 IS NOT OLD.gcode_sha256
 OR NEW.application_version IS NOT OLD.application_version
 OR NEW.application_digest IS NOT OLD.application_digest
 OR NEW.machine_digest IS NOT OLD.machine_digest
 OR NEW.provider_digest IS NOT OLD.provider_digest
 OR NEW.predecessor_execution_id IS NOT OLD.predecessor_execution_id
 OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'machine execution permanent binding is immutable');
END;

CREATE TRIGGER machine_executions_milestone_monotonic
BEFORE UPDATE ON machine_executions
WHEN CASE NEW.stream_milestone
        WHEN 'NOT_STARTED' THEN 0
        WHEN 'FIRST_WRITE_POSSIBLE' THEN 1
        WHEN 'STREAM_CONFIRMED' THEN 2
     END
   < CASE OLD.stream_milestone
        WHEN 'NOT_STARTED' THEN 0
        WHEN 'FIRST_WRITE_POSSIBLE' THEN 1
        WHEN 'STREAM_CONFIRMED' THEN 2
     END
BEGIN
    SELECT RAISE(ABORT, 'machine stream milestone cannot regress');
END;

CREATE TRIGGER machine_executions_evidence_immutable
BEFORE UPDATE ON machine_executions
WHEN (OLD.recovery_evidence_json IS NOT NULL AND NEW.recovery_evidence_json IS NOT OLD.recovery_evidence_json)
 OR (OLD.recovery_intent IS NOT NULL AND NEW.recovery_intent IS NOT OLD.recovery_intent)
 OR (OLD.retired_at IS NOT NULL AND (
        NEW.retired_at IS NOT OLD.retired_at
     OR NEW.retirement_disposition IS NOT OLD.retirement_disposition
     OR NEW.successor_execution_id IS NOT OLD.successor_execution_id
 ))
 OR (OLD.outcome_json IS NOT NULL AND (
        NEW.outcome_kind IS NOT OLD.outcome_kind
     OR NEW.outcome_json IS NOT OLD.outcome_json
     OR NEW.outcome_at IS NOT OLD.outcome_at
 ))
BEGIN
    SELECT RAISE(ABORT, 'machine execution evidence is immutable');
END;

CREATE TRIGGER machine_executions_reject_delete
BEFORE DELETE ON machine_executions
BEGIN
    SELECT RAISE(ABORT, 'machine executions cannot be deleted');
END;

CREATE TRIGGER machine_sessions_reject_insert_conflict
BEFORE INSERT ON machine_sessions
WHEN EXISTS (SELECT 1 FROM machine_sessions WHERE execution_id = NEW.execution_id)
BEGIN
    SELECT RAISE(ABORT, 'machine sessions are append-only');
END;

CREATE TRIGGER machine_sessions_reject_update
BEFORE UPDATE ON machine_sessions
BEGIN
    SELECT RAISE(ABORT, 'machine sessions are append-only');
END;

CREATE TRIGGER machine_sessions_reject_delete
BEFORE DELETE ON machine_sessions
BEGIN
    SELECT RAISE(ABORT, 'machine sessions are append-only');
END;

CREATE TRIGGER machine_approval_authority_immutable
BEFORE UPDATE ON machine_approval_challenges
WHEN NEW.challenge_id IS NOT OLD.challenge_id
 OR NEW.execution_id IS NOT OLD.execution_id
 OR NEW.execution_revision IS NOT OLD.execution_revision
 OR NEW.action IS NOT OLD.action
 OR NEW.requester_uid IS NOT OLD.requester_uid
 OR NEW.requester_gid IS NOT OLD.requester_gid
 OR NEW.requester_pid IS NOT OLD.requester_pid
 OR NEW.operator_uid IS NOT OLD.operator_uid
 OR NEW.operator_gid IS NOT OLD.operator_gid
 OR NEW.issued_at IS NOT OLD.issued_at
 OR NEW.expires_at IS NOT OLD.expires_at
 OR json_remove(NEW.challenge_json, '$.status', '$.status_changed_at', '$.consumer')
      IS NOT json_remove(OLD.challenge_json, '$.status', '$.status_changed_at', '$.consumer')
BEGIN
    SELECT RAISE(ABORT, 'machine approval authority is immutable');
END;

CREATE TRIGGER machine_approval_current_authority_on_update
BEFORE UPDATE ON machine_approval_challenges
WHEN OLD.status = 'PENDING'
 AND NEW.status = 'CONSUMED'
 AND NOT EXISTS (
    SELECT 1 FROM machine_executions
    WHERE execution_id = OLD.execution_id
      AND revision = OLD.execution_revision
      AND service_epoch = json_extract(OLD.challenge_json, '$.binding.service_epoch')
      AND machine_session_epoch = json_extract(OLD.challenge_json, '$.binding.machine_session_epoch')
      AND phase = json_extract(OLD.challenge_json, '$.binding.required_prior_phase')
      AND retired_at IS NULL
      AND phase != 'COMPLETED'
 )
BEGIN
    SELECT RAISE(ABORT, 'machine approval authority is stale');
END;

CREATE TRIGGER machine_approval_one_time_decision
BEFORE UPDATE ON machine_approval_challenges
WHEN OLD.status != 'PENDING' OR NEW.status = 'PENDING'
BEGIN
    SELECT RAISE(ABORT, 'machine approval decision is one-time');
END;

CREATE TRIGGER machine_approval_reject_delete
BEFORE DELETE ON machine_approval_challenges
BEGIN
    SELECT RAISE(ABORT, 'machine approval challenges cannot be deleted');
END;

CREATE TRIGGER machine_events_reject_insert_conflict
BEFORE INSERT ON machine_events
WHEN EXISTS (SELECT 1 FROM machine_events WHERE event_id = NEW.event_id)
BEGIN
    SELECT RAISE(ABORT, 'machine events are append-only');
END;

CREATE TRIGGER machine_events_reject_update
BEFORE UPDATE ON machine_events
BEGIN
    SELECT RAISE(ABORT, 'machine events are append-only');
END;

CREATE TRIGGER machine_events_reject_delete
BEFORE DELETE ON machine_events
BEGIN
    SELECT RAISE(ABORT, 'machine events are append-only');
END;

CREATE TRIGGER machine_progress_reject_insert_conflict
BEFORE INSERT ON machine_progress_events
WHEN EXISTS (SELECT 1 FROM machine_progress_events WHERE progress_id = NEW.progress_id)
BEGIN
    SELECT RAISE(ABORT, 'machine progress is append-only');
END;

CREATE TRIGGER machine_progress_requires_current_stream
BEFORE INSERT ON machine_progress_events
WHEN NOT EXISTS (
    SELECT 1 FROM machine_executions
    WHERE execution_id = NEW.execution_id
      AND revision = NEW.execution_revision
      AND phase = 'STREAMING'
      AND retired_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'machine progress requires current STREAMING authority');
END;

CREATE TRIGGER machine_progress_reject_update
BEFORE UPDATE ON machine_progress_events
BEGIN
    SELECT RAISE(ABORT, 'machine progress is append-only');
END;

CREATE TRIGGER machine_progress_reject_delete
BEFORE DELETE ON machine_progress_events
BEGIN
    SELECT RAISE(ABORT, 'machine progress is append-only');
END;

CREATE TRIGGER machine_requests_authority_immutable
BEFORE UPDATE ON machine_request_results
WHEN NEW.principal_uid IS NOT OLD.principal_uid
 OR NEW.principal_gid IS NOT OLD.principal_gid
 OR NEW.request_id IS NOT OLD.request_id
 OR NEW.requester_pid IS NOT OLD.requester_pid
 OR NEW.command IS NOT OLD.command
 OR NEW.argument_digest IS NOT OLD.argument_digest
 OR NEW.execution_id IS NOT OLD.execution_id
 OR NEW.action IS NOT OLD.action
 OR NEW.execution_revision IS NOT OLD.execution_revision
 OR NEW.service_epoch IS NOT OLD.service_epoch
 OR NEW.machine_session_epoch IS NOT OLD.machine_session_epoch
 OR NEW.recovery_disposition IS NOT OLD.recovery_disposition
 OR NEW.recovery_intent IS NOT OLD.recovery_intent
 OR NEW.predecessor_execution_id IS NOT OLD.predecessor_execution_id
 OR NEW.admission_id IS NOT OLD.admission_id
 OR NEW.dispatch_nonce IS NOT OLD.dispatch_nonce
 OR NEW.reserved_at IS NOT OLD.reserved_at
BEGIN
    SELECT RAISE(ABORT, 'machine request authority is immutable');
END;

CREATE TRIGGER machine_requests_monotonic_result
BEFORE UPDATE ON machine_request_results
WHEN (OLD.response_json IS NOT NULL AND (
        NEW.response_json IS NOT OLD.response_json OR NEW.completed_at IS NOT OLD.completed_at
     ))
 OR (OLD.claimed_at IS NOT NULL AND NEW.claimed_at IS NOT OLD.claimed_at)
 OR (OLD.invalidated_at IS NOT NULL AND (
        NEW.invalidated_at IS NOT OLD.invalidated_at
     OR NEW.invalidation_reason IS NOT OLD.invalidation_reason
 ))
BEGIN
    SELECT RAISE(ABORT, 'machine request result is immutable');
END;

CREATE TRIGGER machine_requests_reject_delete
BEFORE DELETE ON machine_request_results
BEGIN
    SELECT RAISE(ABORT, 'machine request results cannot be deleted');
END;

CREATE TRIGGER jobs_reject_cancel_with_active_machine_owner
BEFORE UPDATE OF state ON jobs
WHEN NEW.state = 'CANCELLED'
 AND EXISTS (
    SELECT 1
    FROM machine_executions
    WHERE job_id = OLD.job_id
      AND retired_at IS NULL
      AND phase != 'COMPLETED'
 )
BEGIN
    SELECT RAISE(ABORT, 'active machine ownership prevents job cancellation');
END;
