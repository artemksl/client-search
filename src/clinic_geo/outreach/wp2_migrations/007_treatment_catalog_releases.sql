-- TEMPORARY DATABASES ONLY.
-- This WP2 preview migration is deliberately outside the auto-discovered
-- outreach migration directory. Apply it explicitly; never to the live DB.

CREATE TABLE treatment_catalog_releases (
    id                       TEXT PRIMARY KEY,
    schema_version           TEXT NOT NULL,
    release_version          INTEGER NOT NULL CHECK (release_version > 0),
    lifecycle_state          TEXT NOT NULL CHECK (lifecycle_state IN ('proposed','active')),
    review_state             TEXT NOT NULL CHECK (review_state IN ('review_required','approved')),
    source_catalogue_path    TEXT NOT NULL,
    source_catalogue_sha256  TEXT NOT NULL CHECK (length(source_catalogue_sha256) = 64),
    payload_sha256           TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
    legacy_mapping_payload_sha256 TEXT NOT NULL CHECK (length(legacy_mapping_payload_sha256) = 64),
    approved_by              TEXT NOT NULL,
    approved_at              TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    CHECK (
        (lifecycle_state = 'proposed' AND review_state = 'review_required')
        OR (lifecycle_state = 'active' AND review_state = 'approved')
    )
);

CREATE TRIGGER treatment_catalog_release_starts_proposed
BEFORE INSERT ON treatment_catalog_releases
WHEN NEW.lifecycle_state <> 'proposed' OR NEW.review_state <> 'review_required'
BEGIN
    SELECT RAISE(ABORT, 'new treatment catalogue releases must start proposed');
END;

CREATE TABLE treatment_concepts (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE treatment_concept_versions (
    id                                      TEXT PRIMARY KEY,
    concept_id                              TEXT NOT NULL REFERENCES treatment_concepts(id),
    catalogue_release_id                    TEXT NOT NULL REFERENCES treatment_catalog_releases(id),
    canonical_name                          TEXT NOT NULL,
    lifecycle_state                         TEXT NOT NULL CHECK (lifecycle_state IN ('proposed','active','superseded','retired')),
    review_state                            TEXT NOT NULL CHECK (review_state IN ('review_required','deferred','approved','rejected')),
    campaign_selectability_state             TEXT NOT NULL CHECK (campaign_selectability_state IN ('selectable','excluded')),
    recommendation_prompt_eligibility_state  TEXT NOT NULL CHECK (recommendation_prompt_eligibility_state IN ('eligible','restricted_pom','excluded')),
    UNIQUE (catalogue_release_id, concept_id),
    CHECK (
        (lifecycle_state = 'active' AND review_state = 'approved')
        OR (lifecycle_state = 'proposed' AND review_state IN ('review_required','deferred'))
        OR (lifecycle_state = 'superseded' AND review_state = 'approved')
        OR (lifecycle_state = 'retired' AND review_state IN ('approved','rejected'))
    ),
    CHECK (campaign_selectability_state = 'excluded' OR (lifecycle_state = 'active' AND review_state = 'approved')),
    CHECK (
        recommendation_prompt_eligibility_state = 'excluded'
        OR (
            lifecycle_state = 'active'
            AND review_state = 'approved'
            AND campaign_selectability_state = 'selectable'
        )
    ),
    CHECK (
        lifecycle_state NOT IN ('superseded','retired')
        OR (
            campaign_selectability_state = 'excluded'
            AND recommendation_prompt_eligibility_state = 'excluded'
        )
    )
);

CREATE TABLE treatment_term_versions (
    id                                      TEXT PRIMARY KEY,
    term_id                                 TEXT NOT NULL,
    catalogue_release_id                    TEXT NOT NULL REFERENCES treatment_catalog_releases(id),
    term_kind                               TEXT NOT NULL CHECK (term_kind IN ('canonical','alias','spelling','brand','device','subtype','concern','broad_category','package')),
    exact_term                              TEXT NOT NULL,
    term_content_sha256                     TEXT NOT NULL CHECK (length(term_content_sha256) = 64),
    lifecycle_state                         TEXT NOT NULL CHECK (lifecycle_state IN ('proposed','active','superseded','retired')),
    review_state                            TEXT NOT NULL CHECK (review_state IN ('review_required','deferred','approved','rejected')),
    campaign_selectability_state             TEXT NOT NULL CHECK (campaign_selectability_state IN ('selectable','excluded')),
    recommendation_prompt_eligibility_state  TEXT NOT NULL CHECK (recommendation_prompt_eligibility_state IN ('eligible','restricted_pom','excluded')),
    UNIQUE (catalogue_release_id, term_id),
    CHECK (
        (lifecycle_state = 'active' AND review_state = 'approved')
        OR (lifecycle_state = 'proposed' AND review_state IN ('review_required','deferred'))
        OR (lifecycle_state = 'superseded' AND review_state = 'approved')
        OR (lifecycle_state = 'retired' AND review_state IN ('approved','rejected'))
    ),
    CHECK (campaign_selectability_state = 'excluded' OR (lifecycle_state = 'active' AND review_state = 'approved')),
    CHECK (
        recommendation_prompt_eligibility_state = 'excluded'
        OR (
            lifecycle_state = 'active'
            AND review_state = 'approved'
            AND campaign_selectability_state = 'selectable'
        )
    ),
    CHECK (
        lifecycle_state NOT IN ('superseded','retired')
        OR (
            campaign_selectability_state = 'excluded'
            AND recommendation_prompt_eligibility_state = 'excluded'
        )
    )
);

CREATE TABLE treatment_term_concept_links (
    term_version_id     TEXT NOT NULL REFERENCES treatment_term_versions(id),
    concept_version_id  TEXT NOT NULL REFERENCES treatment_concept_versions(id),
    PRIMARY KEY (term_version_id, concept_version_id)
);

CREATE TABLE patient_search_intents (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE patient_search_intent_versions (
    id                                      TEXT PRIMARY KEY,
    patient_search_intent_id                TEXT NOT NULL REFERENCES patient_search_intents(id),
    catalogue_release_id                    TEXT NOT NULL REFERENCES treatment_catalog_releases(id),
    language_locale                         TEXT NOT NULL,
    exact_patient_phrase                    TEXT NOT NULL,
    exact_phrase_content_sha256             TEXT NOT NULL CHECK (length(exact_phrase_content_sha256) = 64),
    rendered_treatment_term_sha256          TEXT NOT NULL CHECK (length(rendered_treatment_term_sha256) = 64),
    lifecycle_state                         TEXT NOT NULL CHECK (lifecycle_state IN ('proposed','active','superseded','retired')),
    review_state                            TEXT NOT NULL CHECK (review_state IN ('review_required','deferred','approved','rejected')),
    campaign_selectability_state             TEXT NOT NULL CHECK (campaign_selectability_state IN ('selectable','excluded')),
    recommendation_prompt_eligibility_state  TEXT NOT NULL CHECK (recommendation_prompt_eligibility_state IN ('eligible','restricted_pom','excluded')),
    branch_offering_inference_allowed       INTEGER NOT NULL DEFAULT 0 CHECK (branch_offering_inference_allowed = 0),
    UNIQUE (catalogue_release_id, patient_search_intent_id),
    CHECK (
        (lifecycle_state = 'active' AND review_state = 'approved')
        OR (lifecycle_state = 'proposed' AND review_state IN ('review_required','deferred'))
        OR (lifecycle_state = 'superseded' AND review_state = 'approved')
        OR (lifecycle_state = 'retired' AND review_state IN ('approved','rejected'))
    ),
    CHECK (campaign_selectability_state = 'excluded' OR (lifecycle_state = 'active' AND review_state = 'approved')),
    CHECK (
        recommendation_prompt_eligibility_state = 'excluded'
        OR (
            lifecycle_state = 'active'
            AND review_state = 'approved'
            AND campaign_selectability_state = 'selectable'
        )
    ),
    CHECK (
        lifecycle_state NOT IN ('superseded','retired')
        OR (
            campaign_selectability_state = 'excluded'
            AND recommendation_prompt_eligibility_state = 'excluded'
        )
    )
);

CREATE TABLE search_intent_concept_links (
    search_intent_version_id  TEXT NOT NULL REFERENCES patient_search_intent_versions(id),
    concept_version_id        TEXT NOT NULL REFERENCES treatment_concept_versions(id),
    PRIMARY KEY (search_intent_version_id, concept_version_id)
);

CREATE TABLE legacy_treatment_mappings (
    id                       TEXT PRIMARY KEY,
    catalogue_release_id     TEXT NOT NULL REFERENCES treatment_catalog_releases(id),
    legacy_slug              TEXT NOT NULL,
    legacy_name              TEXT NOT NULL,
    classification           TEXT NOT NULL CHECK (classification IN ('existing_treatment','alias_or_spelling','brand_or_device','subtype','related_but_distinct','patient_concern_or_broad_category','package_containing_multiple_treatments','genuinely_new_treatment','insufficient_evidence')),
    review_state             TEXT NOT NULL CHECK (review_state IN ('approved','review_required')),
    reason_summary           TEXT NOT NULL,
    UNIQUE (catalogue_release_id, legacy_slug)
);

CREATE TABLE legacy_treatment_mapping_concept_links (
    legacy_mapping_id   TEXT NOT NULL REFERENCES legacy_treatment_mappings(id),
    concept_version_id  TEXT NOT NULL REFERENCES treatment_concept_versions(id),
    authority_kind      TEXT NOT NULL CHECK (authority_kind IN ('approved','candidate')),
    PRIMARY KEY (legacy_mapping_id, concept_version_id, authority_kind)
);

CREATE TABLE legacy_treatment_mapping_intent_links (
    legacy_mapping_id        TEXT NOT NULL REFERENCES legacy_treatment_mappings(id),
    search_intent_version_id  TEXT NOT NULL REFERENCES patient_search_intent_versions(id),
    authority_kind            TEXT NOT NULL CHECK (authority_kind IN ('approved','candidate')),
    PRIMARY KEY (legacy_mapping_id, search_intent_version_id, authority_kind)
);

CREATE TRIGGER concept_version_insert_requires_draft_release
BEFORE INSERT ON treatment_concept_versions
WHEN NOT EXISTS (
    SELECT 1 FROM treatment_catalog_releases
    WHERE id = NEW.catalogue_release_id
      AND lifecycle_state = 'proposed'
      AND review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'concept versions require an unsealed draft release'); END;

CREATE TRIGGER term_version_insert_requires_draft_release
BEFORE INSERT ON treatment_term_versions
WHEN NOT EXISTS (
    SELECT 1 FROM treatment_catalog_releases
    WHERE id = NEW.catalogue_release_id
      AND lifecycle_state = 'proposed'
      AND review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'term versions require an unsealed draft release'); END;

CREATE TRIGGER intent_version_insert_requires_draft_release
BEFORE INSERT ON patient_search_intent_versions
WHEN NOT EXISTS (
    SELECT 1 FROM treatment_catalog_releases
    WHERE id = NEW.catalogue_release_id
      AND lifecycle_state = 'proposed'
      AND review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'intent versions require an unsealed draft release'); END;

CREATE TRIGGER legacy_mapping_insert_requires_draft_release
BEFORE INSERT ON legacy_treatment_mappings
WHEN NOT EXISTS (
    SELECT 1 FROM treatment_catalog_releases
    WHERE id = NEW.catalogue_release_id
      AND lifecycle_state = 'proposed'
      AND review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'legacy mappings require an unsealed draft release'); END;

CREATE TRIGGER term_link_requires_same_draft_release
BEFORE INSERT ON treatment_term_concept_links
WHEN NOT EXISTS (
    SELECT 1
    FROM treatment_term_versions AS term
    JOIN treatment_concept_versions AS concept
      ON concept.id = NEW.concept_version_id
     AND concept.catalogue_release_id = term.catalogue_release_id
    JOIN treatment_catalog_releases AS release
      ON release.id = term.catalogue_release_id
    WHERE term.id = NEW.term_version_id
      AND release.lifecycle_state = 'proposed'
      AND release.review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'term links require the same unsealed draft release'); END;

CREATE TRIGGER intent_link_requires_same_draft_release
BEFORE INSERT ON search_intent_concept_links
WHEN NOT EXISTS (
    SELECT 1
    FROM patient_search_intent_versions AS intent
    JOIN treatment_concept_versions AS concept
      ON concept.id = NEW.concept_version_id
     AND concept.catalogue_release_id = intent.catalogue_release_id
    JOIN treatment_catalog_releases AS release
      ON release.id = intent.catalogue_release_id
    WHERE intent.id = NEW.search_intent_version_id
      AND release.lifecycle_state = 'proposed'
      AND release.review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'intent links require the same unsealed draft release'); END;

CREATE TRIGGER legacy_concept_link_requires_same_draft_release
BEFORE INSERT ON legacy_treatment_mapping_concept_links
WHEN NOT EXISTS (
    SELECT 1
    FROM legacy_treatment_mappings AS mapping
    JOIN treatment_concept_versions AS concept
      ON concept.id = NEW.concept_version_id
     AND concept.catalogue_release_id = mapping.catalogue_release_id
    JOIN treatment_catalog_releases AS release
      ON release.id = mapping.catalogue_release_id
    WHERE mapping.id = NEW.legacy_mapping_id
      AND release.lifecycle_state = 'proposed'
      AND release.review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'legacy concept links require the same unsealed draft release'); END;

CREATE TRIGGER legacy_intent_link_requires_same_draft_release
BEFORE INSERT ON legacy_treatment_mapping_intent_links
WHEN NOT EXISTS (
    SELECT 1
    FROM legacy_treatment_mappings AS mapping
    JOIN patient_search_intent_versions AS intent
      ON intent.id = NEW.search_intent_version_id
     AND intent.catalogue_release_id = mapping.catalogue_release_id
    JOIN treatment_catalog_releases AS release
      ON release.id = mapping.catalogue_release_id
    WHERE mapping.id = NEW.legacy_mapping_id
      AND release.lifecycle_state = 'proposed'
      AND release.review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'legacy intent links require the same unsealed draft release'); END;

CREATE TRIGGER term_authority_requires_concept_authority
BEFORE INSERT ON treatment_term_concept_links
WHEN EXISTS (
    SELECT 1 FROM treatment_term_versions
    WHERE id = NEW.term_version_id AND lifecycle_state = 'active' AND review_state = 'approved'
) AND NOT EXISTS (
    SELECT 1
    FROM treatment_concept_versions AS concept
    JOIN treatment_term_versions AS term
      ON term.id = NEW.term_version_id
     AND term.catalogue_release_id = concept.catalogue_release_id
    WHERE concept.id = NEW.concept_version_id
      AND concept.lifecycle_state = 'active'
      AND concept.review_state = 'approved'
)
BEGIN SELECT RAISE(ABORT, 'authoritative term requires authoritative concept'); END;

CREATE TRIGGER intent_authority_requires_concept_authority
BEFORE INSERT ON search_intent_concept_links
WHEN EXISTS (
    SELECT 1 FROM patient_search_intent_versions
    WHERE id = NEW.search_intent_version_id AND lifecycle_state = 'active' AND review_state = 'approved'
) AND NOT EXISTS (
    SELECT 1
    FROM treatment_concept_versions AS concept
    JOIN patient_search_intent_versions AS intent
      ON intent.id = NEW.search_intent_version_id
     AND intent.catalogue_release_id = concept.catalogue_release_id
    WHERE concept.id = NEW.concept_version_id
      AND concept.lifecycle_state = 'active'
      AND concept.review_state = 'approved'
)
BEGIN SELECT RAISE(ABORT, 'authoritative intent requires authoritative concept'); END;

CREATE TRIGGER approved_legacy_concept_link_requires_authority
BEFORE INSERT ON legacy_treatment_mapping_concept_links
WHEN NEW.authority_kind = 'approved' AND (
    NOT EXISTS (
        SELECT 1 FROM legacy_treatment_mappings
        WHERE id = NEW.legacy_mapping_id AND review_state = 'approved'
    ) OR NOT EXISTS (
        SELECT 1
        FROM treatment_concept_versions AS concept
        JOIN legacy_treatment_mappings AS mapping
          ON mapping.id = NEW.legacy_mapping_id
         AND mapping.catalogue_release_id = concept.catalogue_release_id
        WHERE concept.id = NEW.concept_version_id
          AND concept.lifecycle_state = 'active'
          AND concept.review_state = 'approved'
    )
)
BEGIN SELECT RAISE(ABORT, 'approved legacy mapping requires authoritative concept'); END;

CREATE TRIGGER approved_legacy_intent_link_requires_authority
BEFORE INSERT ON legacy_treatment_mapping_intent_links
WHEN NEW.authority_kind = 'approved' AND (
    NOT EXISTS (
        SELECT 1 FROM legacy_treatment_mappings
        WHERE id = NEW.legacy_mapping_id AND review_state = 'approved'
    ) OR NOT EXISTS (
        SELECT 1
        FROM patient_search_intent_versions AS intent
        JOIN legacy_treatment_mappings AS mapping
          ON mapping.id = NEW.legacy_mapping_id
         AND mapping.catalogue_release_id = intent.catalogue_release_id
        WHERE intent.id = NEW.search_intent_version_id
          AND intent.lifecycle_state = 'active'
          AND intent.review_state = 'approved'
    )
)
BEGIN SELECT RAISE(ABORT, 'approved legacy mapping requires authoritative intent'); END;

CREATE TRIGGER candidate_legacy_concept_link_requires_review
BEFORE INSERT ON legacy_treatment_mapping_concept_links
WHEN NEW.authority_kind = 'candidate' AND NOT EXISTS (
    SELECT 1 FROM legacy_treatment_mappings
    WHERE id = NEW.legacy_mapping_id AND review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'candidate legacy mapping must remain review-required'); END;

CREATE TRIGGER candidate_legacy_intent_link_requires_review
BEFORE INSERT ON legacy_treatment_mapping_intent_links
WHEN NEW.authority_kind = 'candidate' AND NOT EXISTS (
    SELECT 1 FROM legacy_treatment_mappings
    WHERE id = NEW.legacy_mapping_id AND review_state = 'review_required'
)
BEGIN SELECT RAISE(ABORT, 'candidate legacy mapping must remain review-required'); END;

CREATE TRIGGER treatment_catalog_releases_seal_only BEFORE UPDATE ON treatment_catalog_releases
WHEN NOT (
    OLD.lifecycle_state = 'proposed'
    AND OLD.review_state = 'review_required'
    AND NEW.lifecycle_state = 'active'
    AND NEW.review_state = 'approved'
    AND NEW.id = OLD.id
    AND NEW.schema_version = OLD.schema_version
    AND NEW.release_version = OLD.release_version
    AND NEW.source_catalogue_path = OLD.source_catalogue_path
    AND NEW.source_catalogue_sha256 = OLD.source_catalogue_sha256
    AND NEW.payload_sha256 = OLD.payload_sha256
    AND NEW.legacy_mapping_payload_sha256 = OLD.legacy_mapping_payload_sha256
    AND NEW.approved_by = OLD.approved_by
    AND NEW.approved_at = OLD.approved_at
    AND NEW.created_at = OLD.created_at
)
BEGIN SELECT RAISE(ABORT, 'treatment catalogue release permits only one seal transition'); END;
CREATE TRIGGER treatment_catalog_releases_no_delete BEFORE DELETE ON treatment_catalog_releases
BEGIN SELECT RAISE(ABORT, 'approved treatment catalogue releases are immutable'); END;
CREATE TRIGGER treatment_concepts_no_update BEFORE UPDATE ON treatment_concepts
BEGIN SELECT RAISE(ABORT, 'treatment concepts are immutable'); END;
CREATE TRIGGER treatment_concepts_no_delete BEFORE DELETE ON treatment_concepts
BEGIN SELECT RAISE(ABORT, 'treatment concepts are immutable'); END;
CREATE TRIGGER treatment_concept_versions_no_update BEFORE UPDATE ON treatment_concept_versions
BEGIN SELECT RAISE(ABORT, 'treatment concept versions are immutable'); END;
CREATE TRIGGER treatment_concept_versions_no_delete BEFORE DELETE ON treatment_concept_versions
BEGIN SELECT RAISE(ABORT, 'treatment concept versions are immutable'); END;
CREATE TRIGGER treatment_term_versions_no_update BEFORE UPDATE ON treatment_term_versions
BEGIN SELECT RAISE(ABORT, 'treatment term versions are immutable'); END;
CREATE TRIGGER treatment_term_versions_no_delete BEFORE DELETE ON treatment_term_versions
BEGIN SELECT RAISE(ABORT, 'treatment term versions are immutable'); END;
CREATE TRIGGER treatment_term_concept_links_no_update BEFORE UPDATE ON treatment_term_concept_links
BEGIN SELECT RAISE(ABORT, 'treatment term links are immutable'); END;
CREATE TRIGGER treatment_term_concept_links_no_delete BEFORE DELETE ON treatment_term_concept_links
BEGIN SELECT RAISE(ABORT, 'treatment term links are immutable'); END;
CREATE TRIGGER patient_search_intents_no_update BEFORE UPDATE ON patient_search_intents
BEGIN SELECT RAISE(ABORT, 'patient search intents are immutable'); END;
CREATE TRIGGER patient_search_intents_no_delete BEFORE DELETE ON patient_search_intents
BEGIN SELECT RAISE(ABORT, 'patient search intents are immutable'); END;
CREATE TRIGGER patient_search_intent_versions_no_update BEFORE UPDATE ON patient_search_intent_versions
BEGIN SELECT RAISE(ABORT, 'patient search intent versions are immutable'); END;
CREATE TRIGGER patient_search_intent_versions_no_delete BEFORE DELETE ON patient_search_intent_versions
BEGIN SELECT RAISE(ABORT, 'patient search intent versions are immutable'); END;
CREATE TRIGGER search_intent_concept_links_no_update BEFORE UPDATE ON search_intent_concept_links
BEGIN SELECT RAISE(ABORT, 'search intent links are immutable'); END;
CREATE TRIGGER search_intent_concept_links_no_delete BEFORE DELETE ON search_intent_concept_links
BEGIN SELECT RAISE(ABORT, 'search intent links are immutable'); END;
CREATE TRIGGER legacy_treatment_mappings_no_update BEFORE UPDATE ON legacy_treatment_mappings
BEGIN SELECT RAISE(ABORT, 'legacy treatment mappings are immutable'); END;
CREATE TRIGGER legacy_treatment_mappings_no_delete BEFORE DELETE ON legacy_treatment_mappings
BEGIN SELECT RAISE(ABORT, 'legacy treatment mappings are immutable'); END;
CREATE TRIGGER legacy_treatment_mapping_concept_links_no_update BEFORE UPDATE ON legacy_treatment_mapping_concept_links
BEGIN SELECT RAISE(ABORT, 'legacy treatment concept links are immutable'); END;
CREATE TRIGGER legacy_treatment_mapping_concept_links_no_delete BEFORE DELETE ON legacy_treatment_mapping_concept_links
BEGIN SELECT RAISE(ABORT, 'legacy treatment concept links are immutable'); END;
CREATE TRIGGER legacy_treatment_mapping_intent_links_no_update BEFORE UPDATE ON legacy_treatment_mapping_intent_links
BEGIN SELECT RAISE(ABORT, 'legacy treatment intent links are immutable'); END;
CREATE TRIGGER legacy_treatment_mapping_intent_links_no_delete BEFORE DELETE ON legacy_treatment_mapping_intent_links
BEGIN SELECT RAISE(ABORT, 'legacy treatment intent links are immutable'); END;
