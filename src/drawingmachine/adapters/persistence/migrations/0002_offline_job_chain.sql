CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    request_json TEXT NOT NULL,
    config_snapshot_json TEXT NOT NULL,
    blocker_json TEXT,
    error_json TEXT,
    ready_snapshot_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_artifacts (
    job_id TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE RESTRICT,
    job_revision INTEGER NOT NULL CHECK (job_revision >= 0),
    role TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    media_type TEXT NOT NULL,
    PRIMARY KEY (job_id, job_revision, role)
);

CREATE TABLE job_events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE RESTRICT,
    job_revision INTEGER NOT NULL CHECK (job_revision >= 0),
    event_type TEXT NOT NULL,
    prior_state TEXT,
    result_state TEXT NOT NULL,
    request_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER job_artifacts_reject_insert_conflict
BEFORE INSERT ON job_artifacts
WHEN EXISTS (
    SELECT 1
    FROM job_artifacts
    WHERE job_id = NEW.job_id
      AND job_revision = NEW.job_revision
      AND role = NEW.role
)
BEGIN
    SELECT RAISE(ABORT, 'job_artifacts is append-only');
END;

CREATE TRIGGER job_artifacts_reject_update
BEFORE UPDATE ON job_artifacts
BEGIN
    SELECT RAISE(ABORT, 'job_artifacts is append-only');
END;

CREATE TRIGGER job_artifacts_reject_delete
BEFORE DELETE ON job_artifacts
BEGIN
    SELECT RAISE(ABORT, 'job_artifacts is append-only');
END;

CREATE TRIGGER job_events_reject_insert_conflict
BEFORE INSERT ON job_events
WHEN EXISTS (
    SELECT 1
    FROM job_events
    WHERE event_id = NEW.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'job_events is append-only');
END;

CREATE TRIGGER job_events_reject_update
BEFORE UPDATE ON job_events
BEGIN
    SELECT RAISE(ABORT, 'job_events is append-only');
END;

CREATE TRIGGER job_events_reject_delete
BEFORE DELETE ON job_events
BEGIN
    SELECT RAISE(ABORT, 'job_events is append-only');
END;

CREATE TRIGGER jobs_revision_step
BEFORE UPDATE ON jobs
WHEN NEW.revision != OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'jobs revision must advance by exactly one');
END;

CREATE TRIGGER jobs_ready_snapshot_immutable
BEFORE UPDATE ON jobs
WHEN OLD.ready_snapshot_json IS NOT NULL
 AND NEW.ready_snapshot_json IS NOT OLD.ready_snapshot_json
BEGIN
    SELECT RAISE(ABORT, 'jobs ready snapshot is immutable');
END;
