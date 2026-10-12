ALTER TABLE people                 ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE contact_points         ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE interactions           ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE suppressions           ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE sequence_enrollments   ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE outreach_export_items  ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);

ALTER TABLE visibility_checks      ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE visibility_checks      ADD COLUMN treatment_slug TEXT REFERENCES treatments(slug);
ALTER TABLE account_rationales     ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE account_rationales     ADD COLUMN treatment_slug TEXT REFERENCES treatments(slug);
DROP INDEX idx_current_rationale;
CREATE UNIQUE INDEX idx_current_rationale
  ON account_rationales (account_id) WHERE is_current = 1 AND branch_id IS NULL;
CREATE UNIQUE INDEX idx_current_branch_rationale
  ON account_rationales (account_id, branch_id, treatment_slug)
  WHERE is_current = 1 AND branch_id IS NOT NULL;
ALTER TABLE message_drafts         ADD COLUMN branch_id TEXT REFERENCES clinic_branches(id);
ALTER TABLE message_drafts         ADD COLUMN treatment_slug TEXT REFERENCES treatments(slug);

CREATE INDEX idx_people_branch          ON people (account_id, branch_id, authority_role);
CREATE INDEX idx_contacts_branch        ON contact_points (account_id, branch_id, status);
CREATE INDEX idx_visibility_branch      ON visibility_checks (account_id, branch_id, treatment_slug, approval_status);
CREATE INDEX idx_rationales_branch      ON account_rationales (account_id, branch_id, is_current);
CREATE INDEX idx_drafts_branch          ON message_drafts (account_id, branch_id, treatment_slug, status);
CREATE INDEX idx_suppressions_branch    ON suppressions (account_id, branch_id, active);
