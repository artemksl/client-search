-- TEMP-DATABASE ONLY. This file is intentionally outside the auto-discovered
-- outreach/migrations package until the production migration gate is approved.
-- Patient-query geography and campaign identities are deliberately separate
-- from the administrative territory taxonomy.  This migration adds no
-- production campaign data; reviewed manifests are installed by an explicit
-- store operation and are exercised against temporary databases first.

CREATE TABLE query_geography_releases (
    id                 TEXT PRIMARY KEY,
    schema_version     TEXT NOT NULL,
    registry_version   TEXT NOT NULL,
    lifecycle_state    TEXT NOT NULL
                           CHECK (lifecycle_state IN ('draft','approved')),
    payload_sha256     TEXT NOT NULL UNIQUE
                           CHECK (length(payload_sha256) = 64
                                  AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    approved_by        TEXT,
    approved_at        TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (id, payload_sha256),
    CHECK (
        (lifecycle_state = 'draft' AND approved_by IS NULL AND approved_at IS NULL)
        OR
        (lifecycle_state = 'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
    )
);

CREATE TABLE query_geography_versions (
    id                         TEXT PRIMARY KEY,
    query_geography_id         TEXT NOT NULL,
    release_id                 TEXT NOT NULL
                                   REFERENCES query_geography_releases(id)
                                   ON DELETE RESTRICT,
    level                      TEXT NOT NULL
                                   CHECK (level IN ('local','greater_area','city')),
    rendered_name              TEXT NOT NULL CHECK (length(trim(rendered_name)) > 0),
    country_code               TEXT NOT NULL
                                   CHECK (length(country_code) = 2
                                          AND country_code = upper(country_code)),
    registry_version           TEXT NOT NULL,
    registry_payload_sha256    TEXT NOT NULL
                                   CHECK (length(registry_payload_sha256) = 64
                                          AND registry_payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at                 TEXT NOT NULL,
    UNIQUE (release_id, query_geography_id)
);

CREATE TABLE evidence_freshness_policies (
    version                         TEXT PRIMARY KEY,
    schema_version                  TEXT NOT NULL,
    purpose                         TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    local_max_age_days              INTEGER NOT NULL CHECK (local_max_age_days >= 0),
    greater_area_max_age_days       INTEGER NOT NULL CHECK (greater_area_max_age_days >= 0),
    city_max_age_days               INTEGER NOT NULL CHECK (city_max_age_days >= 0),
    lifecycle_state                 TEXT NOT NULL CHECK (lifecycle_state IN ('draft','approved')),
    payload_sha256                  TEXT NOT NULL UNIQUE
                                        CHECK (length(payload_sha256) = 64
                                               AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    approved_by                     TEXT,
    approved_at                     TEXT,
    created_at                      TEXT NOT NULL,
    CHECK (
        (lifecycle_state = 'draft' AND approved_by IS NULL AND approved_at IS NULL)
        OR
        (lifecycle_state = 'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
    )
);

INSERT INTO evidence_freshness_policies
  (version,schema_version,purpose,local_max_age_days,
   greater_area_max_age_days,city_max_age_days,lifecycle_state,payload_sha256,
   approved_by,approved_at,created_at)
VALUES
  ('market-discovery-v1','evidence.freshness_policy.v1','market_discovery',7,7,7,
   'approved','7b48d927a120ad90a05fee05ec144994fbf805668200d7564f0e38d18cf8b3f9',
   'product-owner','2026-07-20T00:00:00Z','2026-07-20T00:00:00Z');

CREATE TABLE campaigns (
    id                          TEXT PRIMARY KEY,
    territory_id                TEXT NOT NULL UNIQUE,
    territory_taxonomy_version  TEXT NOT NULL,
    foundation_manifest_sha256  TEXT NOT NULL
                                    CHECK (length(foundation_manifest_sha256) = 64
                                           AND foundation_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at                  TEXT NOT NULL
);

CREATE TABLE campaign_revisions (
    id                              TEXT PRIMARY KEY,
    campaign_id                     TEXT NOT NULL REFERENCES campaigns(id) ON DELETE RESTRICT,
    revision_number                 INTEGER NOT NULL CHECK (revision_number >= 1),
    parent_revision_id              TEXT REFERENCES campaign_revisions(id) ON DELETE RESTRICT,
    query_geography_release_id      TEXT,
    query_geography_payload_sha256  TEXT
                                         CHECK (
                                           query_geography_payload_sha256 IS NULL
                                           OR (
                                             length(query_geography_payload_sha256) = 64
                                             AND query_geography_payload_sha256
                                                   NOT GLOB '*[^0-9a-f]*'
                                           )
                                         ),
    treatment_catalogue_release_id  TEXT REFERENCES treatment_catalog_releases(id) ON DELETE RESTRICT,
    freshness_policy_version        TEXT NOT NULL REFERENCES evidence_freshness_policies(version) ON DELETE RESTRICT,
    state                           TEXT NOT NULL CHECK (state IN ('planned','active','completed')),
    query_scope_status              TEXT NOT NULL CHECK (query_scope_status IN ('unscoped','approved')),
    foundation_manifest_sha256      TEXT NOT NULL
                                        CHECK (length(foundation_manifest_sha256) = 64
                                               AND foundation_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at                      TEXT NOT NULL,
    activated_at                    TEXT,
    completed_at                    TEXT,
    UNIQUE (campaign_id, revision_number),
    FOREIGN KEY (
        query_geography_release_id,
        query_geography_payload_sha256
    ) REFERENCES query_geography_releases(id, payload_sha256) ON DELETE RESTRICT,
    CHECK (parent_revision_id IS NULL OR parent_revision_id <> id),
    CHECK (
        (revision_number = 1 AND parent_revision_id IS NULL)
        OR
        (revision_number > 1 AND parent_revision_id IS NOT NULL)
    ),
    CHECK (
        (query_scope_status = 'unscoped'
          AND query_geography_release_id IS NULL
          AND query_geography_payload_sha256 IS NULL)
        OR
        (query_scope_status = 'approved'
          AND query_geography_release_id IS NOT NULL
          AND query_geography_payload_sha256 IS NOT NULL)
    ),
    CHECK (
        (state = 'planned' AND activated_at IS NULL AND completed_at IS NULL)
        OR
        (state = 'active' AND activated_at IS NOT NULL AND completed_at IS NULL
          AND query_scope_status = 'approved'
          AND treatment_catalogue_release_id IS NOT NULL)
        OR
        (state = 'completed' AND activated_at IS NOT NULL AND completed_at IS NOT NULL
          AND query_scope_status = 'approved'
          AND treatment_catalogue_release_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_one_active_campaign_revision
    ON campaign_revisions ((1)) WHERE state = 'active';
CREATE INDEX idx_campaign_revisions_campaign
    ON campaign_revisions (campaign_id, revision_number);

CREATE TABLE campaign_query_scopes (
    campaign_revision_id          TEXT NOT NULL
                                      REFERENCES campaign_revisions(id)
                                      ON DELETE RESTRICT,
    scope_level                   TEXT NOT NULL
                                      CHECK (scope_level IN ('local','greater_area','city')),
    query_geography_version_id    TEXT NOT NULL
                                      REFERENCES query_geography_versions(id)
                                      ON DELETE RESTRICT,
    created_at                    TEXT NOT NULL,
    PRIMARY KEY (campaign_revision_id, scope_level),
    UNIQUE (campaign_revision_id, query_geography_version_id)
);

CREATE TABLE campaign_search_intents (
    campaign_revision_id      TEXT NOT NULL
                                  REFERENCES campaign_revisions(id)
                                  ON DELETE RESTRICT,
    search_intent_version_id  TEXT NOT NULL
                                  REFERENCES patient_search_intent_versions(id)
                                  ON DELETE RESTRICT,
    created_at                TEXT NOT NULL,
    PRIMARY KEY (campaign_revision_id, search_intent_version_id)
);

CREATE TABLE campaign_search_intent_selection_events (
    id                        TEXT PRIMARY KEY,
    campaign_revision_id      TEXT NOT NULL
                                  REFERENCES campaign_revisions(id)
                                  ON DELETE RESTRICT,
    search_intent_version_id  TEXT NOT NULL
                                  REFERENCES patient_search_intent_versions(id)
                                  ON DELETE RESTRICT,
    action                    TEXT NOT NULL CHECK (action IN ('add','defer')),
    decided_by                TEXT NOT NULL,
    decided_at                TEXT NOT NULL,
    notes                     TEXT
);

CREATE TABLE campaign_candidates (
    id                    TEXT PRIMARY KEY,
    campaign_revision_id  TEXT NOT NULL
                              REFERENCES campaign_revisions(id)
                              ON DELETE RESTRICT,
    branch_id             TEXT NOT NULL
                              REFERENCES clinic_branches(id)
                              ON DELETE RESTRICT,
    created_at            TEXT NOT NULL,
    UNIQUE (campaign_revision_id, branch_id)
);

CREATE TABLE campaign_candidate_intent_relevance (
    candidate_id              TEXT NOT NULL
                                  REFERENCES campaign_candidates(id)
                                  ON DELETE RESTRICT,
    search_intent_version_id  TEXT NOT NULL
                                  REFERENCES patient_search_intent_versions(id)
                                  ON DELETE RESTRICT,
    created_at                TEXT NOT NULL,
    PRIMARY KEY (candidate_id, search_intent_version_id)
);

CREATE TABLE campaign_candidate_decision_events (
    id                TEXT PRIMARY KEY,
    candidate_id      TEXT NOT NULL REFERENCES campaign_candidates(id) ON DELETE RESTRICT,
    decision          TEXT NOT NULL
                          CHECK (decision IN (
                            'shortlist','research_first','defer','exclude'
                          )),
    decided_by        TEXT NOT NULL,
    decided_at        TEXT NOT NULL,
    notes             TEXT
);

CREATE TABLE campaign_completion_records (
    id                    TEXT PRIMARY KEY,
    campaign_revision_id  TEXT NOT NULL UNIQUE
                              REFERENCES campaign_revisions(id)
                              ON DELETE RESTRICT,
    completed_by          TEXT NOT NULL,
    completed_at          TEXT NOT NULL,
    completion_reason     TEXT NOT NULL,
    payload_sha256        TEXT NOT NULL UNIQUE
                              CHECK (length(payload_sha256) = 64
                                     AND payload_sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE campaign_origin_links (
    id                           TEXT PRIMARY KEY,
    campaign_revision_id         TEXT NOT NULL
                                     REFERENCES campaign_revisions(id)
                                     ON DELETE RESTRICT,
    origin_type                  TEXT NOT NULL
                                     CHECK (origin_type IN ('reopened_revision','candidate_follow_up','manual')),
    source_campaign_revision_id  TEXT REFERENCES campaign_revisions(id) ON DELETE RESTRICT,
    source_candidate_id          TEXT REFERENCES campaign_candidates(id) ON DELETE RESTRICT,
    created_at                   TEXT NOT NULL,
    CHECK (
        (origin_type = 'reopened_revision'
          AND source_campaign_revision_id IS NOT NULL
          AND source_candidate_id IS NULL)
        OR
        (origin_type = 'candidate_follow_up'
          AND source_campaign_revision_id IS NULL
          AND source_candidate_id IS NOT NULL)
        OR
        (origin_type = 'manual'
          AND source_campaign_revision_id IS NULL
          AND source_candidate_id IS NULL)
    )
);

-- Approved registry inputs cannot be edited in place.  A change is a new
-- release with a new canonical payload hash.
CREATE TRIGGER query_geography_release_approved_no_update
BEFORE UPDATE ON query_geography_releases
WHEN OLD.lifecycle_state = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approved query-geography releases are immutable');
END;

CREATE TRIGGER query_geography_release_approved_no_delete
BEFORE DELETE ON query_geography_releases
WHEN OLD.lifecycle_state = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approved query-geography releases are immutable');
END;

CREATE TRIGGER query_geography_version_no_update
BEFORE UPDATE ON query_geography_versions
BEGIN
    SELECT RAISE(ABORT, 'query-geography versions are immutable');
END;

CREATE TRIGGER query_geography_version_no_delete
BEFORE DELETE ON query_geography_versions
BEGIN
    SELECT RAISE(ABORT, 'query-geography versions are immutable');
END;

CREATE TRIGGER query_geography_version_no_insert_after_approval
BEFORE INSERT ON query_geography_versions
WHEN (SELECT lifecycle_state FROM query_geography_releases WHERE id=NEW.release_id) = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approved query-geography releases cannot gain rows');
END;

CREATE TRIGGER query_geography_version_validate_registry_insert
BEFORE INSERT ON query_geography_versions
WHEN NOT EXISTS (
    SELECT 1 FROM query_geography_releases release
    WHERE release.id = NEW.release_id
      AND release.lifecycle_state = 'draft'
      AND release.registry_version = NEW.registry_version
      AND release.payload_sha256 = NEW.registry_payload_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'query-geography version must match its draft registry release');
END;

CREATE TRIGGER query_geography_release_approval_requires_rows
BEFORE UPDATE OF lifecycle_state ON query_geography_releases
WHEN OLD.lifecycle_state = 'draft' AND NEW.lifecycle_state = 'approved'
     AND NOT EXISTS (
         SELECT 1 FROM query_geography_versions version
         WHERE version.release_id = OLD.id
     )
BEGIN
    SELECT RAISE(ABORT, 'query-geography releases cannot approve without rows');
END;

CREATE TRIGGER evidence_freshness_policy_approved_no_update
BEFORE UPDATE ON evidence_freshness_policies
WHEN OLD.lifecycle_state = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approved freshness policies are immutable');
END;

CREATE TRIGGER evidence_freshness_policy_approved_no_delete
BEFORE DELETE ON evidence_freshness_policies
WHEN OLD.lifecycle_state = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approved freshness policies are immutable');
END;

-- A campaign is a stable series identity.  Mutable planning happens only on a
-- planned revision, never on the series row or on active/completed history.
CREATE TRIGGER campaign_series_no_update
BEFORE UPDATE ON campaigns
BEGIN
    SELECT RAISE(ABORT, 'campaign series identities are immutable');
END;

CREATE TRIGGER campaign_series_no_delete
BEFORE DELETE ON campaigns
BEGIN
    SELECT RAISE(ABORT, 'campaign series identities are immutable');
END;

CREATE TRIGGER campaign_revision_starts_planned
BEFORE INSERT ON campaign_revisions
WHEN NEW.state <> 'planned'
BEGIN
    SELECT RAISE(ABORT, 'new campaign revisions must start planned');
END;

CREATE TRIGGER campaign_revision_parent_is_completed_same_series
BEFORE INSERT ON campaign_revisions
WHEN NEW.parent_revision_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM campaign_revisions parent
        WHERE parent.id = NEW.parent_revision_id
          AND parent.campaign_id = NEW.campaign_id
          AND parent.state = 'completed'
    ) THEN RAISE(ABORT, 'parent revision must be completed in the same campaign') END;
END;

CREATE TRIGGER campaign_revision_identity_no_update
BEFORE UPDATE ON campaign_revisions
WHEN NEW.id IS NOT OLD.id
  OR NEW.campaign_id IS NOT OLD.campaign_id
  OR NEW.revision_number IS NOT OLD.revision_number
  OR NEW.parent_revision_id IS NOT OLD.parent_revision_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'campaign revision identity is immutable');
END;

CREATE TRIGGER campaign_revision_valid_transition
BEFORE UPDATE OF state ON campaign_revisions
WHEN NOT (
    (OLD.state = 'planned' AND NEW.state IN ('planned','active'))
    OR
    (OLD.state = 'active' AND NEW.state = 'completed')
    OR
    (OLD.state = 'completed' AND NEW.state = 'completed')
)
BEGIN
    SELECT RAISE(ABORT, 'invalid campaign revision state transition');
END;

CREATE TRIGGER campaign_revision_activation_requires_exact_mvp_scopes
BEFORE UPDATE OF state ON campaign_revisions
WHEN OLD.state = 'planned' AND NEW.state = 'active'
BEGIN
    SELECT CASE WHEN NEW.activated_at IS NULL
        THEN RAISE(ABORT, 'campaign activation requires activated_at') END;
    SELECT CASE WHEN NEW.query_scope_status <> 'approved'
                      OR NEW.query_geography_release_id IS NULL
                      OR NEW.query_geography_payload_sha256 IS NULL
        THEN RAISE(ABORT, 'campaign activation requires approved query scopes pinned to exact bytes') END;
    SELECT CASE WHEN NEW.treatment_catalogue_release_id IS NULL
        THEN RAISE(ABORT, 'campaign activation requires a treatment catalogue') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM query_geography_releases release
        WHERE release.id = NEW.query_geography_release_id
          AND release.payload_sha256 = NEW.query_geography_payload_sha256
          AND release.lifecycle_state = 'approved'
    ) THEN RAISE(ABORT, 'campaign activation requires an approved geography release') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM campaign_query_scopes scope
        JOIN query_geography_versions geography
          ON geography.id = scope.query_geography_version_id
        WHERE scope.campaign_revision_id = NEW.id
          AND (
            geography.release_id <> NEW.query_geography_release_id
            OR geography.registry_payload_sha256
                 <> NEW.query_geography_payload_sha256
            OR geography.level <> scope.scope_level
          )
    ) THEN RAISE(ABORT, 'campaign activation found a scope outside the pinned geography bytes') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM treatment_catalog_releases release
        WHERE release.id = NEW.treatment_catalogue_release_id
          AND release.lifecycle_state = 'active'
          AND release.review_state = 'approved'
    ) THEN RAISE(ABORT, 'campaign activation requires an approved treatment catalogue') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM campaign_search_intents selection
        JOIN patient_search_intent_versions intent
          ON intent.id = selection.search_intent_version_id
        WHERE selection.campaign_revision_id = NEW.id
          AND intent.catalogue_release_id = NEW.treatment_catalogue_release_id
          AND intent.lifecycle_state = 'active'
          AND intent.review_state = 'approved'
          AND intent.campaign_selectability_state = 'selectable'
    ) THEN RAISE(ABORT, 'campaign activation requires at least one approved selectable intent') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM campaign_search_intents selection
        JOIN patient_search_intent_versions intent
          ON intent.id = selection.search_intent_version_id
        WHERE selection.campaign_revision_id = NEW.id
          AND (
            intent.catalogue_release_id <> NEW.treatment_catalogue_release_id
            OR intent.lifecycle_state <> 'active'
            OR intent.review_state <> 'approved'
            OR intent.campaign_selectability_state <> 'selectable'
          )
    ) THEN RAISE(ABORT, 'campaign activation found an invalid selected intent') END;
    SELECT CASE WHEN (
        SELECT count(*) FROM campaign_query_scopes scope
        WHERE scope.campaign_revision_id = NEW.id
    ) <> 3 THEN RAISE(ABORT, 'campaign activation requires exactly three query scopes') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM campaign_query_scopes scope
        WHERE scope.campaign_revision_id = NEW.id AND scope.scope_level = 'local'
    ) OR NOT EXISTS (
        SELECT 1 FROM campaign_query_scopes scope
        WHERE scope.campaign_revision_id = NEW.id AND scope.scope_level = 'greater_area'
    ) OR NOT EXISTS (
        SELECT 1 FROM campaign_query_scopes scope
        WHERE scope.campaign_revision_id = NEW.id AND scope.scope_level = 'city'
    ) THEN RAISE(ABORT, 'campaign activation requires local, greater-area, and city scopes') END;
END;

CREATE TRIGGER campaign_revision_planned_geography_rebind_guard
BEFORE UPDATE OF query_geography_release_id,query_geography_payload_sha256
ON campaign_revisions
WHEN OLD.state = 'planned'
  AND (
    NEW.query_geography_release_id IS NOT OLD.query_geography_release_id
    OR NEW.query_geography_payload_sha256
         IS NOT OLD.query_geography_payload_sha256
  )
  AND EXISTS (
    SELECT 1
    FROM campaign_query_scopes scope
    JOIN query_geography_versions geography
      ON geography.id = scope.query_geography_version_id
    WHERE scope.campaign_revision_id = OLD.id
      AND (
        geography.release_id IS NOT NEW.query_geography_release_id
        OR geography.registry_payload_sha256
             IS NOT NEW.query_geography_payload_sha256
        OR geography.level <> scope.scope_level
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'planned campaign geography cannot be rebound around existing scopes');
END;

CREATE TRIGGER campaign_revision_planned_treatment_rebind_guard
BEFORE UPDATE OF treatment_catalogue_release_id ON campaign_revisions
WHEN OLD.state = 'planned'
  AND NEW.treatment_catalogue_release_id IS NOT OLD.treatment_catalogue_release_id
  AND EXISTS (
    SELECT 1
    FROM campaign_search_intents selection
    JOIN patient_search_intent_versions intent
      ON intent.id = selection.search_intent_version_id
    WHERE selection.campaign_revision_id = OLD.id
      AND (
        intent.catalogue_release_id IS NOT NEW.treatment_catalogue_release_id
        OR intent.lifecycle_state <> 'active'
        OR intent.review_state <> 'approved'
        OR intent.campaign_selectability_state <> 'selectable'
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'planned campaign treatment release cannot be rebound around selected intents');
END;

CREATE TRIGGER campaign_revision_active_update_is_completion_only
BEFORE UPDATE ON campaign_revisions
WHEN OLD.state = 'active'
BEGIN
    SELECT CASE WHEN NEW.state <> 'completed'
        THEN RAISE(ABORT, 'active campaign revisions can only complete') END;
    SELECT CASE WHEN NEW.query_geography_release_id IS NOT OLD.query_geography_release_id
                      OR NEW.query_geography_payload_sha256
                           IS NOT OLD.query_geography_payload_sha256
                      OR NEW.treatment_catalogue_release_id IS NOT OLD.treatment_catalogue_release_id
                      OR NEW.freshness_policy_version IS NOT OLD.freshness_policy_version
                      OR NEW.query_scope_status IS NOT OLD.query_scope_status
                      OR NEW.foundation_manifest_sha256 IS NOT OLD.foundation_manifest_sha256
                      OR NEW.activated_at IS NOT OLD.activated_at
        THEN RAISE(ABORT, 'active campaign revision bindings are immutable') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM campaign_completion_records record
        WHERE record.campaign_revision_id = OLD.id
          AND record.completed_at = NEW.completed_at
    ) THEN RAISE(ABORT, 'campaign completion requires its immutable record') END;
END;

CREATE TRIGGER campaign_revision_completed_no_update
BEFORE UPDATE ON campaign_revisions
WHEN OLD.state = 'completed'
BEGIN
    SELECT RAISE(ABORT, 'completed campaign revisions are immutable');
END;

CREATE TRIGGER campaign_revision_no_delete
BEFORE DELETE ON campaign_revisions
BEGIN
    SELECT RAISE(ABORT, 'campaign revision history cannot be deleted');
END;

CREATE TRIGGER campaign_query_scope_validate_insert
BEFORE INSERT ON campaign_query_scopes
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM campaign_revisions revision
        JOIN query_geography_versions geography
          ON geography.id = NEW.query_geography_version_id
        WHERE revision.id = NEW.campaign_revision_id
          AND revision.state = 'planned'
          AND revision.query_scope_status = 'approved'
          AND geography.release_id = revision.query_geography_release_id
          AND geography.registry_payload_sha256 = revision.query_geography_payload_sha256
          AND geography.level = NEW.scope_level
    ) THEN RAISE(ABORT, 'query scope must match the planned revision release and level') END;
END;

CREATE TRIGGER campaign_query_scope_validate_update
BEFORE UPDATE ON campaign_query_scopes
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM campaign_revisions revision
        JOIN query_geography_versions geography
          ON geography.id = NEW.query_geography_version_id
        WHERE revision.id = NEW.campaign_revision_id
          AND revision.state = 'planned'
          AND revision.query_scope_status = 'approved'
          AND geography.release_id = revision.query_geography_release_id
          AND geography.registry_payload_sha256 = revision.query_geography_payload_sha256
          AND geography.level = NEW.scope_level
    ) THEN RAISE(ABORT, 'query scope must match the planned revision release and level') END;
END;

CREATE TRIGGER campaign_query_scope_identity_no_update
BEFORE UPDATE ON campaign_query_scopes
WHEN NEW.campaign_revision_id IS NOT OLD.campaign_revision_id
  OR NEW.scope_level IS NOT OLD.scope_level
BEGIN
    SELECT RAISE(ABORT, 'campaign query-scope ownership and level are immutable');
END;

CREATE TRIGGER campaign_query_scope_no_delete_after_activation
BEFORE DELETE ON campaign_query_scopes
WHEN (SELECT state FROM campaign_revisions WHERE id=OLD.campaign_revision_id) <> 'planned'
BEGIN
    SELECT RAISE(ABORT, 'active or completed campaign scopes are immutable');
END;

CREATE TRIGGER campaign_search_intent_validate_insert
BEFORE INSERT ON campaign_search_intents
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM campaign_revisions revision
        JOIN patient_search_intent_versions intent
          ON intent.id = NEW.search_intent_version_id
        WHERE revision.id = NEW.campaign_revision_id
          AND revision.state = 'planned'
          AND revision.treatment_catalogue_release_id = intent.catalogue_release_id
          AND intent.lifecycle_state = 'active'
          AND intent.review_state = 'approved'
          AND intent.campaign_selectability_state = 'selectable'
    ) THEN RAISE(ABORT, 'search intent must be approved, active, selectable, and belong to the pinned release') END;
END;

CREATE TRIGGER campaign_search_intent_no_update
BEFORE UPDATE ON campaign_search_intents
BEGIN
    SELECT RAISE(ABORT, 'campaign search-intent selections are immutable rows');
END;

CREATE TRIGGER campaign_search_intent_no_delete_after_activation
BEFORE DELETE ON campaign_search_intents
WHEN (SELECT state FROM campaign_revisions WHERE id=OLD.campaign_revision_id) <> 'planned'
BEGIN
    SELECT RAISE(ABORT, 'active or completed search-intent selections are immutable');
END;

CREATE TRIGGER campaign_search_intent_selection_event_validate_insert
BEFORE INSERT ON campaign_search_intent_selection_events
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM campaign_revisions revision
        JOIN patient_search_intent_versions intent
          ON intent.id = NEW.search_intent_version_id
        WHERE revision.id = NEW.campaign_revision_id
          AND revision.state = 'active'
          AND revision.treatment_catalogue_release_id = intent.catalogue_release_id
          AND intent.lifecycle_state = 'active'
          AND intent.review_state = 'approved'
          AND intent.campaign_selectability_state = 'selectable'
    ) THEN RAISE(ABORT, 'active intent events require an approved selectable intent from the pinned release') END;
END;

CREATE TRIGGER campaign_search_intent_selection_event_no_update
BEFORE UPDATE ON campaign_search_intent_selection_events
BEGIN
    SELECT RAISE(ABORT, 'campaign search-intent events are immutable');
END;

CREATE TRIGGER campaign_search_intent_selection_event_no_delete
BEFORE DELETE ON campaign_search_intent_selection_events
BEGIN
    SELECT RAISE(ABORT, 'campaign search-intent events are immutable');
END;

CREATE TRIGGER campaign_candidate_validate_insert
BEFORE INSERT ON campaign_candidates
WHEN NOT EXISTS (
    SELECT 1 FROM campaign_revisions revision
    WHERE revision.id = NEW.campaign_revision_id
      AND revision.state IN ('planned','active')
)
BEGIN
    SELECT RAISE(ABORT, 'campaign candidates require a writable revision');
END;

CREATE TRIGGER campaign_candidate_no_update
BEFORE UPDATE ON campaign_candidates
BEGIN
    SELECT RAISE(ABORT, 'campaign candidates are immutable');
END;

CREATE TRIGGER campaign_candidate_no_delete
BEFORE DELETE ON campaign_candidates
BEGIN
    SELECT RAISE(ABORT, 'campaign candidates are immutable');
END;

CREATE TRIGGER campaign_candidate_intent_relevance_validate_insert
BEFORE INSERT ON campaign_candidate_intent_relevance
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM campaign_candidates candidate
        JOIN campaign_revisions revision
          ON revision.id = candidate.campaign_revision_id
        JOIN patient_search_intent_versions intent
          ON intent.id = NEW.search_intent_version_id
        WHERE candidate.id = NEW.candidate_id
          AND revision.state IN ('planned','active')
          AND revision.treatment_catalogue_release_id = intent.catalogue_release_id
          AND intent.lifecycle_state = 'active'
          AND intent.review_state = 'approved'
          AND intent.campaign_selectability_state = 'selectable'
          AND COALESCE(
                (
                  SELECT event.action
                  FROM campaign_search_intent_selection_events event
                  WHERE event.campaign_revision_id = revision.id
                    AND event.search_intent_version_id = intent.id
                  ORDER BY event.decided_at DESC, event.id DESC
                  LIMIT 1
                ),
                CASE WHEN EXISTS (
                  SELECT 1 FROM campaign_search_intents initial_selection
                  WHERE initial_selection.campaign_revision_id = revision.id
                    AND initial_selection.search_intent_version_id = intent.id
                ) THEN 'add' ELSE 'defer' END
              ) = 'add'
    ) THEN RAISE(ABORT, 'candidate relevance requires a currently selected approved intent') END;
END;

CREATE TRIGGER campaign_candidate_intent_relevance_no_update
BEFORE UPDATE ON campaign_candidate_intent_relevance
BEGIN
    SELECT RAISE(ABORT, 'campaign candidate intent relevance is immutable');
END;

CREATE TRIGGER campaign_candidate_intent_relevance_no_delete
BEFORE DELETE ON campaign_candidate_intent_relevance
BEGIN
    SELECT RAISE(ABORT, 'campaign candidate intent relevance is immutable');
END;

CREATE TRIGGER campaign_candidate_decision_event_validate_insert
BEFORE INSERT ON campaign_candidate_decision_events
WHEN NOT EXISTS (
    SELECT 1
    FROM campaign_candidates candidate
    JOIN campaign_revisions revision
      ON revision.id = candidate.campaign_revision_id
    WHERE candidate.id = NEW.candidate_id
      AND revision.state IN ('planned','active')
)
BEGIN
    SELECT RAISE(ABORT, 'campaign candidate decisions require a writable revision');
END;

CREATE TRIGGER campaign_completion_record_requires_active_revision
BEFORE INSERT ON campaign_completion_records
WHEN (SELECT state FROM campaign_revisions WHERE id=NEW.campaign_revision_id) <> 'active'
BEGIN
    SELECT RAISE(ABORT, 'completion records require an active campaign revision');
END;

CREATE TRIGGER campaign_completion_record_no_update
BEFORE UPDATE ON campaign_completion_records
BEGIN
    SELECT RAISE(ABORT, 'campaign completion records are immutable');
END;

CREATE TRIGGER campaign_completion_record_no_delete
BEFORE DELETE ON campaign_completion_records
BEGIN
    SELECT RAISE(ABORT, 'campaign completion records are immutable');
END;

CREATE TRIGGER campaign_candidate_decision_event_no_update
BEFORE UPDATE ON campaign_candidate_decision_events
BEGIN
    SELECT RAISE(ABORT, 'campaign candidate decision events are immutable');
END;

CREATE TRIGGER campaign_candidate_decision_event_no_delete
BEFORE DELETE ON campaign_candidate_decision_events
BEGIN
    SELECT RAISE(ABORT, 'campaign candidate decision events are immutable');
END;

CREATE TRIGGER campaign_origin_link_no_update
BEFORE UPDATE ON campaign_origin_links
BEGIN
    SELECT RAISE(ABORT, 'campaign origin links are immutable');
END;

CREATE TRIGGER campaign_origin_link_no_delete
BEFORE DELETE ON campaign_origin_links
BEGIN
    SELECT RAISE(ABORT, 'campaign origin links are immutable');
END;
