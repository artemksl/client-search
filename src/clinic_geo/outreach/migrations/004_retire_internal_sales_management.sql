-- The active workspace is operated by one founder.  Retain historical task
-- rows for audit, but retire every still-open assignment and stop using tasks
-- or account owners as readiness inputs in application code.

INSERT INTO audit_events
  (occurred_at, actor, entity_type, entity_id, action, before_json, after_json)
SELECT
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  'migration-004',
  'task',
  id,
  'retired_solo_founder_workflow',
  '{"status":"open"}',
  '{"status":"cancelled","reason":"internal sales management removed"}'
FROM tasks
WHERE status = 'open';

UPDATE tasks
SET status = 'cancelled',
    notes = CASE
      WHEN notes IS NULL OR trim(notes) = ''
        THEN 'Retired when internal sales management was removed.'
      ELSE notes || ' Retired when internal sales management was removed.'
    END,
    version = version + 1,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'open';
