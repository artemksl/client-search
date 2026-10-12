-- Separate safe buyer-discovery outreach from delivery of clinic-specific evidence.
-- Readiness remains computed; these columns preserve the evidence used by each gate.

ALTER TABLE people ADD COLUMN authority_role TEXT NOT NULL DEFAULT 'unknown'
    CHECK (authority_role IN ('economic_buyer', 'marketing_owner', 'router', 'influencer', 'unknown'));

UPDATE people
SET authority_role = CASE
    WHEN lower(trim(role_category)) IN ('decision_maker', 'economic_buyer', 'owner', 'founder', 'director')
        THEN 'economic_buyer'
    WHEN lower(trim(role_category)) IN ('marketing_owner', 'marketing')
        THEN 'marketing_owner'
    WHEN lower(trim(role_category)) IN ('router', 'reception', 'receptionist')
        THEN 'router'
    WHEN lower(trim(role_category)) = 'influencer'
        THEN 'influencer'
    ELSE 'unknown'
END;

ALTER TABLE channel_reviews ADD COLUMN outreach_purpose TEXT NOT NULL DEFAULT 'evidence_delivery'
    CHECK (outreach_purpose IN ('routing', 'evidence_delivery'));
ALTER TABLE channel_reviews ADD COLUMN contact_version INTEGER;
ALTER TABLE channel_reviews ADD COLUMN destination_key TEXT;
ALTER TABLE channel_reviews ADD COLUMN policy_version TEXT;
ALTER TABLE channel_reviews ADD COLUMN policy_sha256 TEXT;
ALTER TABLE channel_reviews ADD COLUMN jurisdiction TEXT;
ALTER TABLE channel_reviews ADD COLUMN subscriber_type TEXT NOT NULL DEFAULT 'not_assessed'
    CHECK (subscriber_type IN ('corporate', 'individual', 'sole_trader', 'not_assessed'));
ALTER TABLE channel_reviews ADD COLUMN suppression_screen_status TEXT NOT NULL DEFAULT 'not_screened'
    CHECK (suppression_screen_status IN ('clear', 'blocked', 'not_screened'));
ALTER TABLE channel_reviews ADD COLUMN preference_service_status TEXT NOT NULL DEFAULT 'not_assessed'
    CHECK (preference_service_status IN ('clear', 'blocked', 'not_applicable', 'not_assessed'));
ALTER TABLE channel_reviews ADD COLUMN screened_by TEXT;
ALTER TABLE channel_reviews ADD COLUMN screened_at TEXT;
ALTER TABLE channel_reviews ADD COLUMN screening_notes TEXT;

ALTER TABLE visibility_checks ADD COLUMN evidence_standard TEXT NOT NULL DEFAULT 'legacy_3_plus_1';
ALTER TABLE visibility_checks ADD COLUMN base_prompt_count INTEGER NOT NULL DEFAULT 4 CHECK (base_prompt_count >= 0);
ALTER TABLE visibility_checks ADD COLUMN run_count INTEGER NOT NULL DEFAULT 1 CHECK (run_count >= 0);
ALTER TABLE visibility_checks ADD COLUMN observation_count INTEGER NOT NULL DEFAULT 4 CHECK (observation_count >= 0);
ALTER TABLE visibility_checks ADD COLUMN browser_profiles_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE message_drafts ADD COLUMN outreach_purpose TEXT NOT NULL DEFAULT 'evidence_delivery'
    CHECK (outreach_purpose IN ('routing', 'evidence_delivery'));
ALTER TABLE message_drafts ADD COLUMN routing_scope_attested INTEGER NOT NULL DEFAULT 0
    CHECK (routing_scope_attested IN (0, 1));
ALTER TABLE message_drafts ADD COLUMN content_policy_version TEXT;
ALTER TABLE message_drafts ADD COLUMN content_sha256 TEXT;
ALTER TABLE interactions ADD COLUMN outreach_purpose TEXT NOT NULL DEFAULT 'evidence_delivery'
    CHECK (outreach_purpose IN ('routing', 'evidence_delivery'));
ALTER TABLE sequence_enrollments ADD COLUMN outreach_purpose TEXT NOT NULL DEFAULT 'evidence_delivery'
    CHECK (outreach_purpose IN ('routing', 'evidence_delivery'));
ALTER TABLE outreach_exports ADD COLUMN outreach_purpose TEXT NOT NULL DEFAULT 'evidence_delivery'
    CHECK (outreach_purpose IN ('routing', 'evidence_delivery'));
ALTER TABLE outreach_exports ADD COLUMN research_release_id TEXT;
ALTER TABLE outreach_exports ADD COLUMN research_manifest_sha256 TEXT;

CREATE INDEX idx_people_authority
    ON people (account_id, authority_role, verification_status, status);
CREATE INDEX idx_channel_reviews_purpose
    ON channel_reviews (contact_point_id, outreach_purpose, policy_version, contact_version, reviewed_at DESC);
CREATE INDEX idx_visibility_standard
    ON visibility_checks (account_id, evidence_standard, checked_at DESC);
CREATE INDEX idx_drafts_purpose
    ON message_drafts (account_id, outreach_purpose, status, updated_at);
CREATE INDEX idx_interactions_purpose
    ON interactions (account_id, outreach_purpose, occurred_at);

-- Imported research rows are immutable evidence.  New releases append new
-- snapshots/memberships; mutable sales state belongs in the outreach tables.
CREATE TRIGGER research_snapshots_append_only_update
BEFORE UPDATE ON research_account_snapshots
BEGIN
    SELECT RAISE(ABORT, 'research snapshots are append-only');
END;

CREATE TRIGGER research_snapshots_append_only_delete
BEFORE DELETE ON research_account_snapshots
BEGIN
    SELECT RAISE(ABORT, 'research snapshots are append-only');
END;

CREATE TRIGGER research_memberships_append_only_update
BEFORE UPDATE ON research_memberships
BEGIN
    SELECT RAISE(ABORT, 'research memberships are append-only');
END;

CREATE TRIGGER research_memberships_append_only_delete
BEFORE DELETE ON research_memberships
BEGIN
    SELECT RAISE(ABORT, 'research memberships are append-only');
END;

CREATE TRIGGER approved_message_content_immutable
BEFORE UPDATE ON message_drafts
WHEN OLD.status='approved' AND (
    NEW.subject IS NOT OLD.subject OR NEW.body IS NOT OLD.body OR NEW.channel IS NOT OLD.channel
    OR NEW.person_id IS NOT OLD.person_id OR NEW.contact_point_id IS NOT OLD.contact_point_id
    OR NEW.visibility_check_id IS NOT OLD.visibility_check_id
    OR NEW.outreach_purpose IS NOT OLD.outreach_purpose
)
BEGIN
    SELECT RAISE(ABORT, 'approved message content and bindings are immutable; create a new draft');
END;
