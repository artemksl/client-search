PRAGMA foreign_keys = ON;

CREATE TABLE research_releases (
    release_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL,
    release_path TEXT NOT NULL,
    created_at TEXT,
    imported_at TEXT NOT NULL,
    account_count INTEGER NOT NULL CHECK (account_count >= 0),
    imported_contact_route_count INTEGER NOT NULL DEFAULT 0 CHECK (imported_contact_route_count >= 0),
    manifest_json TEXT NOT NULL
);

CREATE TABLE workspace_state (
    key TEXT PRIMARY KEY CHECK (key IN ('active_release_id')),
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sales_accounts (
    id TEXT PRIMARY KEY,
    display_name_override TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'nurture', 'disqualified', 'won', 'lost', 'archived')),
    owner TEXT,
    stage TEXT NOT NULL DEFAULT 'research'
        CHECK (stage IN ('research', 'contacted', 'replied', 'positive', 'meeting',
                         'qualified', 'proposal', 'won', 'lost', 'nurture')),
    priority_override INTEGER NOT NULL DEFAULT 0 CHECK (priority_override IN (0, 1)),
    priority_override_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE research_account_snapshots (
    release_id TEXT NOT NULL REFERENCES research_releases(release_id) ON DELETE RESTRICT,
    research_account_id TEXT NOT NULL,
    sales_account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    clinic_name TEXT NOT NULL,
    primary_uid TEXT,
    stable_account_key TEXT,
    member_uids_json TEXT NOT NULL,
    prospect_rank INTEGER,
    prospect_score REAL,
    prospect_tier TEXT,
    prospect_status TEXT,
    prospect_analysis TEXT,
    area TEXT,
    category TEXT,
    owned_website TEXT,
    outreach_route TEXT,
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (release_id, research_account_id)
);

CREATE TABLE research_memberships (
    release_id TEXT NOT NULL REFERENCES research_releases(release_id) ON DELETE RESTRICT,
    member_uid TEXT NOT NULL,
    sales_account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    research_account_id TEXT NOT NULL,
    PRIMARY KEY (release_id, member_uid)
);

CREATE TABLE import_conflicts (
    id TEXT PRIMARY KEY,
    sales_account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    release_id TEXT NOT NULL REFERENCES research_releases(release_id) ON DELETE RESTRICT,
    research_account_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('merge', 'split', 'ambiguous')),
    candidate_sales_account_ids_json TEXT NOT NULL,
    resolution TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_notes TEXT
);

CREATE TABLE people (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    full_name TEXT NOT NULL,
    role_title TEXT NOT NULL,
    role_category TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current', 'unconfirmed', 'left', 'archived')),
    verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'confirmed', 'rejected')),
    source_url TEXT,
    verified_at TEXT,
    expires_at TEXT,
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE contact_points (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    person_id TEXT REFERENCES people(id) ON DELETE RESTRICT,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'phone', 'sms', 'whatsapp', 'linkedin', 'form', 'other')),
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    route_scope TEXT NOT NULL DEFAULT 'person' CHECK (route_scope IN ('person', 'organization')),
    organization_route_exception INTEGER NOT NULL DEFAULT 0 CHECK (organization_route_exception IN (0, 1)),
    verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('imported_unverified', 'unverified', 'verified', 'invalid', 'stale')),
    source_url TEXT,
    verified_at TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'invalid', 'archived')),
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, channel, normalized_value)
);

CREATE TABLE channel_reviews (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    contact_point_id TEXT NOT NULL REFERENCES contact_points(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'pending')),
    manual_approval INTEGER NOT NULL DEFAULT 0 CHECK (manual_approval IN (0, 1)),
    lawful_basis_status TEXT NOT NULL DEFAULT 'not_assessed',
    privacy_notice_status TEXT NOT NULL DEFAULT 'not_assessed',
    reviewer TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    expires_at TEXT,
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE visibility_checks (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    platform TEXT NOT NULL,
    browser_profile TEXT NOT NULL,
    logged_out_verified INTEGER NOT NULL CHECK (logged_out_verified IN (0, 1)),
    checked_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    prompt_count INTEGER NOT NULL CHECK (prompt_count >= 0),
    general_prompt_count INTEGER NOT NULL CHECK (general_prompt_count >= 0),
    clinic_specific_prompt_count INTEGER NOT NULL CHECK (clinic_specific_prompt_count >= 0),
    gap_prompt_count INTEGER NOT NULL CHECK (gap_prompt_count >= 0),
    conclusion TEXT NOT NULL CHECK (conclusion IN ('confirmed_gap', 'no_gap', 'inconclusive')),
    approval_status TEXT NOT NULL DEFAULT 'pending' CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_by TEXT,
    approved_at TEXT,
    raw_evidence_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (prompt_count >= general_prompt_count + clinic_specific_prompt_count),
    CHECK (gap_prompt_count <= prompt_count)
);

CREATE TABLE account_rationales (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    visibility_check_id TEXT REFERENCES visibility_checks(id) ON DELETE RESTRICT,
    why_chosen TEXT NOT NULL,
    problem_evidence TEXT NOT NULL,
    evidence_urls_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
    approved_by TEXT,
    approved_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE message_drafts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    person_id TEXT REFERENCES people(id) ON DELETE RESTRICT,
    contact_point_id TEXT REFERENCES contact_points(id) ON DELETE RESTRICT,
    visibility_check_id TEXT REFERENCES visibility_checks(id) ON DELETE RESTRICT,
    channel TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected', 'superseded')),
    approved_by TEXT,
    approved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    task_type TEXT NOT NULL,
    title TEXT NOT NULL,
    owner TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'completed', 'cancelled')),
    completed_at TEXT,
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE opportunities (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL,
    value_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'GBP',
    outcome_reason TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE interactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    person_id TEXT REFERENCES people(id) ON DELETE RESTRICT,
    contact_point_id TEXT REFERENCES contact_points(id) ON DELETE RESTRICT,
    message_draft_id TEXT REFERENCES message_drafts(id) ON DELETE RESTRICT,
    export_id TEXT REFERENCES outreach_exports(id) ON DELETE RESTRICT,
    sequence_step TEXT,
    interaction_type TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound', 'internal')),
    channel TEXT,
    occurred_at TEXT NOT NULL,
    outcome TEXT,
    notes TEXT,
    dedupe_key TEXT UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE suppressions (
    id TEXT PRIMARY KEY,
    account_id TEXT REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    person_id TEXT REFERENCES people(id) ON DELETE RESTRICT,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('account', 'person', 'destination', 'channel', 'domain')),
    scope_value TEXT,
    channel TEXT,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_by TEXT,
    lifted_at TEXT,
    lift_reason TEXT
);

CREATE TABLE sequence_enrollments (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    person_id TEXT REFERENCES people(id) ON DELETE RESTRICT,
    contact_point_id TEXT REFERENCES contact_points(id) ON DELETE RESTRICT,
    sequence_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'cancelled')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (account_id, sequence_key)
);

CREATE TABLE worklists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE worklist_items (
    worklist_id TEXT NOT NULL REFERENCES worklists(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position >= 1 AND position <= 25),
    selection_reason TEXT,
    added_at TEXT NOT NULL,
    PRIMARY KEY (worklist_id, account_id),
    UNIQUE (worklist_id, position)
);

CREATE TRIGGER worklist_max_25
BEFORE INSERT ON worklist_items
WHEN (SELECT COUNT(*) FROM worklist_items WHERE worklist_id = NEW.worklist_id) >= 25
BEGIN
    SELECT RAISE(ABORT, 'worklists are limited to 25 accounts');
END;

CREATE TABLE outreach_exports (
    id TEXT PRIMARY KEY,
    worklist_id TEXT REFERENCES worklists(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'complete', 'failed')),
    format TEXT NOT NULL DEFAULT 'csv',
    path TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);

CREATE TABLE outreach_export_items (
    export_id TEXT NOT NULL REFERENCES outreach_exports(id) ON DELETE RESTRICT,
    account_id TEXT NOT NULL REFERENCES sales_accounts(id) ON DELETE RESTRICT,
    contact_point_id TEXT NOT NULL REFERENCES contact_points(id) ON DELETE RESTRICT,
    message_draft_id TEXT NOT NULL REFERENCES message_drafts(id) ON DELETE RESTRICT,
    destination_key TEXT NOT NULL,
    readiness_json TEXT NOT NULL,
    PRIMARY KEY (export_id, account_id),
    UNIQUE (export_id, destination_key)
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    correlation_id TEXT
);

CREATE TRIGGER audit_events_append_only_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER audit_events_append_only_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
