CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    request_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER audit_events_reject_insert_conflict
BEFORE INSERT ON audit_events
WHEN EXISTS (
    SELECT 1
    FROM audit_events
    WHERE event_id = NEW.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER audit_events_reject_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER audit_events_reject_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
