CREATE TABLE staging_admissions (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    request_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('image', 'gcode')),
    bundle_name TEXT NOT NULL UNIQUE,
    payload_device INTEGER NOT NULL CHECK (payload_device >= 0),
    payload_inode INTEGER NOT NULL CHECK (payload_inode > 0),
    payload_size_bytes INTEGER NOT NULL CHECK (payload_size_bytes >= 0),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    lease_sha256 TEXT NOT NULL CHECK (length(lease_sha256) = 64),
    publisher_boot_id TEXT NOT NULL,
    published_monotonic_ns INTEGER NOT NULL CHECK (published_monotonic_ns >= 0),
    deadline_monotonic_ns INTEGER NOT NULL CHECK (deadline_monotonic_ns > published_monotonic_ns),
    state TEXT NOT NULL CHECK (state IN (
        'REQUEST_DECODED', 'CLAIMED', 'DURABLY_ADMITTED',
        'CONSUMED', 'QUARANTINED', 'RECOVERY_REQUIRED'
    )),
    job_id TEXT UNIQUE REFERENCES jobs (job_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    recovery_code TEXT CHECK (recovery_code IS NULL OR recovery_code IN (
        'EVIDENCE_WRITE_FAILED', 'IDENTITY_DRIFT', 'INVALID_PREPARE_NAME',
        'INVALID_STAGING_BUNDLE', 'LOCK_RACE', 'OBSERVATION_CORRUPT',
        'OBSERVATION_DELETE_FAILED', 'OBSERVATION_UPDATE_FAILED',
        'PARENT_FSYNC_FAILED', 'PUBLISH_MISMATCH', 'QUARANTINE_FAILED',
        'SPECIAL_STAGING_ENTRY', 'STALE_ADMITTED', 'STALE_DROP', 'STALE_PREPARE'
    )),
    CHECK (state NOT IN ('DURABLY_ADMITTED', 'CONSUMED') OR job_id IS NOT NULL),
    CHECK (state NOT IN ('REQUEST_DECODED', 'CLAIMED', 'QUARANTINED') OR job_id IS NULL),
    CHECK ((state = 'RECOVERY_REQUIRED') = (recovery_code IS NOT NULL))
);

CREATE INDEX idx_staging_admissions_state_deadline
ON staging_admissions (state, deadline_monotonic_ns, request_id);

CREATE TRIGGER staging_admissions_identity_immutable
BEFORE UPDATE ON staging_admissions
WHEN NEW.schema_version != OLD.schema_version
  OR NEW.request_id != OLD.request_id
  OR NEW.role != OLD.role
  OR NEW.bundle_name != OLD.bundle_name
  OR NEW.payload_device != OLD.payload_device
  OR NEW.payload_inode != OLD.payload_inode
  OR NEW.payload_size_bytes != OLD.payload_size_bytes
  OR NEW.source_sha256 != OLD.source_sha256
  OR NEW.lease_sha256 != OLD.lease_sha256
  OR NEW.publisher_boot_id != OLD.publisher_boot_id
  OR NEW.published_monotonic_ns != OLD.published_monotonic_ns
  OR NEW.deadline_monotonic_ns != OLD.deadline_monotonic_ns
  OR NEW.created_at_utc != OLD.created_at_utc
BEGIN
    SELECT RAISE(ABORT, 'staging admission identity is immutable');
END;

CREATE TRIGGER staging_admissions_revision_step
BEFORE UPDATE ON staging_admissions
WHEN NEW.revision != OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'staging admission revision must advance by exactly one');
END;

CREATE TRIGGER staging_admissions_state_edge
BEFORE UPDATE ON staging_admissions
WHEN NOT (
    (OLD.state = 'REQUEST_DECODED' AND NEW.state IN ('CLAIMED', 'QUARANTINED', 'RECOVERY_REQUIRED'))
 OR (OLD.state = 'CLAIMED' AND NEW.state IN ('DURABLY_ADMITTED', 'QUARANTINED', 'RECOVERY_REQUIRED'))
 OR (OLD.state = 'DURABLY_ADMITTED' AND NEW.state IN ('CONSUMED', 'RECOVERY_REQUIRED'))
 OR (OLD.state = 'QUARANTINED' AND NEW.state = 'RECOVERY_REQUIRED')
)
BEGIN
    SELECT RAISE(ABORT, 'staging admission state transition is illegal');
END;

CREATE TRIGGER staging_admissions_job_binding_immutable
BEFORE UPDATE ON staging_admissions
WHEN OLD.job_id IS NOT NULL AND NEW.job_id IS NOT OLD.job_id
BEGIN
    SELECT RAISE(ABORT, 'staging admission job binding is immutable');
END;

CREATE TRIGGER staging_admissions_reject_delete
BEFORE DELETE ON staging_admissions
BEGIN
    SELECT RAISE(ABORT, 'staging admissions are durable authority');
END;
