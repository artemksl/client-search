-- TEMPORARY DATABASES ONLY.
--
-- P3 campaign-runtime durability: typed-API idempotency records and action
-- previews.  This additive successor migration extends, but never rewrites,
-- the accepted migrations 006/008/009/010 foundations.  It creates no evidence
-- tables and changes no Market Evidence, Snapshot, or outreach state; campaigns
-- consume evidence read-only through their public APIs.

-- Durable idempotency for typed campaign-lifecycle actions.  A replayed key for
-- the same endpoint and request bytes returns the recorded response; the same
-- key with different request bytes is a conflict enforced in the API layer.
CREATE TABLE campaign_api_idempotency (
    endpoint TEXT NOT NULL CHECK (length(trim(endpoint)) > 0),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    request_sha256 TEXT NOT NULL
        CHECK (length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    http_status INTEGER NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    PRIMARY KEY (endpoint, idempotency_key)
);

-- Action previews bind an exact payload to the revision version token they were
-- computed against, with an explicit expiry.  Confirming an action requires an
-- unexpired preview whose version token still matches the current revision.
CREATE TABLE campaign_action_previews (
    preview_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL CHECK (length(trim(endpoint)) > 0),
    campaign_revision_id TEXT NOT NULL
        REFERENCES campaign_revisions(id) ON DELETE RESTRICT,
    revision_version_token TEXT NOT NULL
        CHECK (length(revision_version_token)=64
               AND revision_version_token NOT GLOB '*[^0-9a-f]*'),
    payload_sha256 TEXT NOT NULL
        CHECK (length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    expires_at TEXT NOT NULL CHECK (julianday(expires_at) IS NOT NULL),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL)
);

CREATE INDEX idx_campaign_action_previews_revision
    ON campaign_action_previews (campaign_revision_id, endpoint);

-- Both records are append-only; the API layer never mutates or deletes them.
CREATE TRIGGER p3_campaign_api_idempotency_no_update
BEFORE UPDATE ON campaign_api_idempotency
BEGIN SELECT RAISE(ABORT,'P3 campaign API idempotency records are immutable'); END;
CREATE TRIGGER p3_campaign_api_idempotency_no_delete
BEFORE DELETE ON campaign_api_idempotency
BEGIN SELECT RAISE(ABORT,'P3 campaign API idempotency records are immutable'); END;
CREATE TRIGGER p3_campaign_action_previews_no_update
BEFORE UPDATE ON campaign_action_previews
BEGIN SELECT RAISE(ABORT,'P3 campaign action previews are immutable'); END;
CREATE TRIGGER p3_campaign_action_previews_no_delete
BEFORE DELETE ON campaign_action_previews
BEGIN SELECT RAISE(ABORT,'P3 campaign action previews are immutable'); END;
