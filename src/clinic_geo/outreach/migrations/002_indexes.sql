CREATE INDEX idx_snapshots_sales_release
    ON research_account_snapshots (sales_account_id, release_id);
CREATE INDEX idx_snapshots_priority
    ON research_account_snapshots (release_id, prospect_tier, prospect_rank);
CREATE INDEX idx_memberships_uid
    ON research_memberships (member_uid, release_id);
CREATE INDEX idx_people_primary
    ON people (account_id, is_primary, verification_status, status);
CREATE INDEX idx_contacts_account_status
    ON contact_points (account_id, verification_status, status);
CREATE INDEX idx_channel_reviews_contact
    ON channel_reviews (contact_point_id, reviewed_at);
CREATE INDEX idx_visibility_account_date
    ON visibility_checks (account_id, checked_at);
CREATE UNIQUE INDEX idx_current_rationale
    ON account_rationales (account_id) WHERE is_current = 1;
CREATE INDEX idx_drafts_account_status
    ON message_drafts (account_id, status, updated_at);
CREATE INDEX idx_tasks_open_due
    ON tasks (account_id, status, due_at);
CREATE INDEX idx_interactions_account_date
    ON interactions (account_id, occurred_at);
CREATE INDEX idx_interactions_draft
    ON interactions (message_draft_id, direction, occurred_at);
CREATE INDEX idx_suppressions_active
    ON suppressions (account_id, scope_type, active);
CREATE INDEX idx_worklist_items_position
    ON worklist_items (worklist_id, position);
CREATE INDEX idx_audit_entity
    ON audit_events (entity_type, entity_id, occurred_at);
