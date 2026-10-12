CREATE TABLE treatments (
    slug             TEXT PRIMARY KEY,
    canonical_name   TEXT NOT NULL,
    category         TEXT,
    aliases_json     TEXT NOT NULL DEFAULT '[]',
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    taxonomy_version TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE clinic_branches (
    id               TEXT PRIMARY KEY,
    sales_account_id TEXT NOT NULL REFERENCES sales_accounts(id),
    first_release_id TEXT NOT NULL REFERENCES research_releases(release_id),
    created_at       TEXT NOT NULL
);

CREATE TABLE clinic_branch_snapshots (
    release_id           TEXT NOT NULL REFERENCES research_releases(release_id),
    branch_id            TEXT NOT NULL REFERENCES clinic_branches(id),
    sales_account_id     TEXT NOT NULL REFERENCES sales_accounts(id),
    branch_key           TEXT NOT NULL,
    member_uids_json     TEXT NOT NULL,
    derived_parent_name  TEXT NOT NULL,
    derived_branch_name  TEXT NOT NULL,
    area                 TEXT,
    area_name            TEXT,
    address              TEXT,
    postcode             TEXT,
    latitude             REAL,
    longitude            REAL,
    is_compound_or_ambiguous INTEGER NOT NULL DEFAULT 0 CHECK (is_compound_or_ambiguous IN (0,1)),
    derivation_status    TEXT NOT NULL DEFAULT 'derived'
                           CHECK (derivation_status IN ('derived','needs_confirmation')),
    created_at           TEXT NOT NULL,
    PRIMARY KEY (release_id, branch_id),
    UNIQUE (release_id, sales_account_id, branch_key)
);

CREATE TABLE branch_confirmations (
    branch_id    TEXT PRIMARY KEY REFERENCES clinic_branches(id),
    status       TEXT NOT NULL DEFAULT 'unconfirmed'
                   CHECK (status IN ('unconfirmed','confirmed','rejected','merged')),
    display_name TEXT,
    merged_into  TEXT REFERENCES clinic_branches(id),
    confirmed_by TEXT,
    confirmed_at TEXT,
    notes        TEXT,
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL
);

CREATE TABLE account_treatment_observations (
    release_id       TEXT NOT NULL REFERENCES research_releases(release_id),
    sales_account_id TEXT NOT NULL REFERENCES sales_accounts(id),
    treatment_slug   TEXT NOT NULL REFERENCES treatments(slug),
    source_name      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (release_id, sales_account_id, treatment_slug)
);

CREATE TABLE branch_treatments (
    id             TEXT PRIMARY KEY,
    branch_id      TEXT NOT NULL REFERENCES clinic_branches(id),
    treatment_slug TEXT NOT NULL REFERENCES treatments(slug),
    state          TEXT NOT NULL
                     CHECK (state IN ('observed','offered','not_offered','unknown')),
    source         TEXT NOT NULL
                     CHECK (source IN ('research_observed','founder','website')),
    confidence     TEXT CHECK (confidence IN ('high','medium','low') OR confidence IS NULL),
    evidence_url   TEXT,
    release_id     TEXT REFERENCES research_releases(release_id),
    confirmed_by   TEXT,
    confirmed_at   TEXT,
    notes          TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (branch_id, treatment_slug, source),
    CHECK (
        (source = 'research_observed' AND state = 'observed')
        OR (source IN ('founder','website') AND state IN ('offered','not_offered','unknown'))
    )
);

CREATE INDEX idx_branch_snapshots_account ON clinic_branch_snapshots (sales_account_id, release_id);
CREATE INDEX idx_branches_account         ON clinic_branches (sales_account_id);
CREATE INDEX idx_branch_tx_branch         ON branch_treatments (branch_id, state);
CREATE INDEX idx_branch_tx_treatment      ON branch_treatments (treatment_slug, state);
CREATE INDEX idx_account_tx_account       ON account_treatment_observations (sales_account_id, release_id);

CREATE TRIGGER clinic_branches_no_update BEFORE UPDATE ON clinic_branches
BEGIN SELECT RAISE(ABORT, 'clinic_branches rows are immutable; use branch_confirmations'); END;
CREATE TRIGGER clinic_branches_no_delete BEFORE DELETE ON clinic_branches
BEGIN SELECT RAISE(ABORT, 'clinic_branches rows are immutable'); END;

CREATE TRIGGER branch_snapshots_append_only_update BEFORE UPDATE ON clinic_branch_snapshots
BEGIN SELECT RAISE(ABORT, 'branch snapshots are append-only'); END;
CREATE TRIGGER branch_snapshots_append_only_delete BEFORE DELETE ON clinic_branch_snapshots
BEGIN SELECT RAISE(ABORT, 'branch snapshots are append-only'); END;

CREATE TRIGGER account_tx_append_only_update BEFORE UPDATE ON account_treatment_observations
BEGIN SELECT RAISE(ABORT, 'account treatment observations are append-only'); END;
CREATE TRIGGER account_tx_append_only_delete BEFORE DELETE ON account_treatment_observations
BEGIN SELECT RAISE(ABORT, 'account treatment observations are append-only'); END;

CREATE TRIGGER branch_tx_research_append_only_update BEFORE UPDATE ON branch_treatments
WHEN OLD.source = 'research_observed'
BEGIN SELECT RAISE(ABORT, 'research-observed branch treatments are append-only'); END;
CREATE TRIGGER branch_tx_research_append_only_delete BEFORE DELETE ON branch_treatments
WHEN OLD.source = 'research_observed'
BEGIN SELECT RAISE(ABORT, 'research-observed branch treatments are append-only'); END;
