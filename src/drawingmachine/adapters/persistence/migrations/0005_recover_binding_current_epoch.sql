DROP TRIGGER machine_approval_current_execution_revision;

CREATE TRIGGER machine_approval_current_execution_revision
BEFORE INSERT ON machine_approval_challenges
WHEN NOT EXISTS (
    SELECT 1 FROM machine_executions
    WHERE execution_id = NEW.execution_id
      AND revision = NEW.execution_revision
      AND (
          NEW.action = 'RECOVER'
          OR service_epoch = json_extract(NEW.challenge_json, '$.binding.service_epoch')
      )
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

DROP TRIGGER machine_approval_current_authority_on_update;

CREATE TRIGGER machine_approval_current_authority_on_update
BEFORE UPDATE ON machine_approval_challenges
WHEN OLD.status = 'PENDING'
 AND NEW.status = 'CONSUMED'
 AND NOT EXISTS (
    SELECT 1 FROM machine_executions
    WHERE execution_id = OLD.execution_id
      AND revision = OLD.execution_revision
      AND (
          OLD.action = 'RECOVER'
          OR service_epoch = json_extract(OLD.challenge_json, '$.binding.service_epoch')
      )
      AND machine_session_epoch = json_extract(OLD.challenge_json, '$.binding.machine_session_epoch')
      AND phase = json_extract(OLD.challenge_json, '$.binding.required_prior_phase')
      AND retired_at IS NULL
      AND phase != 'COMPLETED'
 )
BEGIN
    SELECT RAISE(ABORT, 'machine approval authority is stale');
END;
