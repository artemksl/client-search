-- TEMPORARY OR COPIED DATABASES ONLY.
--
-- Additive P5 structural-response preprocessing and segment-classification
-- records.  These tables deliberately do not reuse the deterministic
-- candidate/occurrence identities introduced by migration 010: a span chosen
-- by a model is model-attested evidence and must remain distinguishable from a
-- purely deterministic occurrence.

CREATE TABLE market_structural_manifests (
    structural_manifest_sha256 TEXT PRIMARY KEY
        CHECK (length(structural_manifest_sha256)=64
               AND structural_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version TEXT NOT NULL
        CHECK (schema_version='market_evidence.structural_manifest.v1'),
    preprocessor_contract_version TEXT NOT NULL
        CHECK (preprocessor_contract_version='market-raw-response-segments.v1'),
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    logical_observation_id TEXT NOT NULL,
    coverage_evidence_id TEXT NOT NULL
        CHECK (length(trim(coverage_evidence_id)) > 0),
    source_platform TEXT NOT NULL CHECK (source_platform IN (
        'chatgpt','gemini','google_ai_overview','copilot'
    )),
    run_ordinal INTEGER NOT NULL CHECK (run_ordinal > 0),
    artifact_sha256 TEXT NOT NULL
        CHECK (length(artifact_sha256)=64
               AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
    raw_answer_sha256 TEXT NOT NULL
        CHECK (length(raw_answer_sha256)=64
               AND raw_answer_sha256 NOT GLOB '*[^0-9a-f]*'),
    raw_character_length INTEGER NOT NULL CHECK (raw_character_length >= 0),
    raw_byte_length INTEGER NOT NULL CHECK (raw_byte_length >= 0),
    structural_partition_complete INTEGER NOT NULL
        CHECK (structural_partition_complete=1),
    model_input_byte_length INTEGER NOT NULL
        CHECK (model_input_byte_length >= 0
               AND model_input_byte_length <= raw_byte_length),
    parser_status TEXT NOT NULL CHECK (parser_status IN (
        'exact','fallback','failed'
    )),
    semantic_occurrence_coverage_state TEXT NOT NULL CHECK (
        semantic_occurrence_coverage_state IN (
            'unproven',
            'not_applicable_proven_nonanswer',
            'complete_strict_envelope'
        )
    ),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (
        logical_observation_id,
        artifact_sha256,
        preprocessor_contract_version
    ),
    UNIQUE (structural_manifest_sha256, purpose),
    FOREIGN KEY (logical_observation_id, purpose)
        REFERENCES market_logical_observations(logical_observation_id, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_sha256, purpose)
        REFERENCES market_artifacts(artifact_sha256, purpose)
        ON DELETE RESTRICT,
    CHECK (raw_answer_sha256=artifact_sha256)
);

CREATE TABLE market_structural_segments (
    segment_id TEXT PRIMARY KEY,
    structural_manifest_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    segment_ordinal INTEGER NOT NULL CHECK (segment_ordinal > 0),
    structural_role TEXT NOT NULL CHECK (structural_role IN (
        'primary_answer',
        'surface_evidence',
        'known_ui_chrome',
        'provider_failure',
        'provider_no_answer',
        'unparsed'
    )),
    model_disposition TEXT NOT NULL CHECK (model_disposition IN (
        'include_verbatim',
        'exclude_exact_ui',
        'exclude_exact_nonanswer'
    )),
    derivation_rule TEXT NOT NULL CHECK (length(trim(derivation_rule)) > 0),
    ambiguity_codes_json TEXT NOT NULL CHECK (
        json_valid(ambiguity_codes_json)
        AND json_type(ambiguity_codes_json)='array'
    ),
    character_start INTEGER NOT NULL CHECK (character_start >= 0),
    character_end INTEGER NOT NULL CHECK (character_end >= character_start),
    byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
    byte_end INTEGER NOT NULL CHECK (byte_end >= byte_start),
    exact_text TEXT NOT NULL,
    exact_text_sha256 TEXT NOT NULL
        CHECK (length(exact_text_sha256)=64
               AND exact_text_sha256 NOT GLOB '*[^0-9a-f]*'),
    segment_sha256 TEXT NOT NULL
        CHECK (length(segment_sha256)=64
               AND segment_sha256 NOT GLOB '*[^0-9a-f]*'),
    segment_json TEXT NOT NULL CHECK (json_valid(segment_json)),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (structural_manifest_sha256, segment_ordinal),
    UNIQUE (structural_manifest_sha256, segment_id),
    FOREIGN KEY (structural_manifest_sha256, purpose)
        REFERENCES market_structural_manifests(
            structural_manifest_sha256, purpose
        ) ON DELETE RESTRICT
);

CREATE TABLE market_structural_hints (
    hint_id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL,
    structural_manifest_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    hint_kind TEXT NOT NULL CHECK (length(trim(hint_kind)) > 0),
    relative_character_start INTEGER NOT NULL
        CHECK (relative_character_start >= 0),
    relative_character_end INTEGER NOT NULL
        CHECK (relative_character_end >= relative_character_start),
    risk_flags_json TEXT NOT NULL CHECK (
        json_valid(risk_flags_json)
        AND json_type(risk_flags_json)='array'
    ),
    hint_json TEXT NOT NULL CHECK (json_valid(hint_json)),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (segment_id, hint_id),
    FOREIGN KEY (structural_manifest_sha256, segment_id)
        REFERENCES market_structural_segments(
            structural_manifest_sha256, segment_id
        ) ON DELETE RESTRICT
);

CREATE TABLE market_segment_classification_processing (
    segment_classification_run_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    audit_id TEXT NOT NULL CHECK (length(trim(audit_id)) > 0),
    checker_version TEXT NOT NULL
        CHECK (checker_version='p5-market-per-run-segment-chat-v3'),
    input_schema_version TEXT NOT NULL
        CHECK (input_schema_version='p5.market_run_segment_input.v3'),
    output_schema_version TEXT NOT NULL
        CHECK (output_schema_version='p5.market_run_segment_output.v3'),
    source_platform TEXT NOT NULL CHECK (source_platform IN (
        'chatgpt','gemini','google_ai_overview','copilot'
    )),
    run_ordinal INTEGER NOT NULL CHECK (run_ordinal > 0),
    input_sha256 TEXT NOT NULL
        CHECK (length(input_sha256)=64
               AND input_sha256 NOT GLOB '*[^0-9a-f]*'),
    validated_response_sha256 TEXT NOT NULL
        CHECK (length(validated_response_sha256)=64
               AND validated_response_sha256 NOT GLOB '*[^0-9a-f]*'),
    resolution TEXT NOT NULL CHECK (resolution IN (
        'model_validated','conservative_fallback'
    )),
    unresolved INTEGER NOT NULL CHECK (unresolved IN (0,1)),
    included_segment_count INTEGER NOT NULL
        CHECK (included_segment_count >= 0),
    uncertain_segment_count INTEGER NOT NULL
        CHECK (uncertain_segment_count >= 0
               AND uncertain_segment_count <= included_segment_count),
    ambiguous_entity_count INTEGER NOT NULL
        CHECK (ambiguous_entity_count >= 0),
    clinic_name_count INTEGER NOT NULL CHECK (clinic_name_count >= 0),
    input_json TEXT NOT NULL CHECK (json_valid(input_json)),
    validated_output_json TEXT NOT NULL
        CHECK (json_valid(validated_output_json)),
    completed_at TEXT NOT NULL CHECK (julianday(completed_at) IS NOT NULL),
    UNIQUE (
        audit_id, checker_version, source_platform, run_ordinal
    ),
    UNIQUE (segment_classification_run_id, purpose),
    CHECK (
        (resolution='model_validated')
        OR (resolution='conservative_fallback' AND unresolved=1)
    )
);

CREATE TABLE market_segment_classification_manifests (
    segment_classification_run_id TEXT NOT NULL,
    structural_manifest_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    PRIMARY KEY (
        segment_classification_run_id,
        structural_manifest_sha256
    ),
    FOREIGN KEY (segment_classification_run_id, purpose)
        REFERENCES market_segment_classification_processing(
            segment_classification_run_id, purpose
        ) ON DELETE RESTRICT,
    FOREIGN KEY (structural_manifest_sha256, purpose)
        REFERENCES market_structural_manifests(
            structural_manifest_sha256, purpose
        ) ON DELETE RESTRICT
);

CREATE TABLE market_model_attested_occurrences (
    model_attested_occurrence_id TEXT PRIMARY KEY,
    occurrence_schema_version TEXT NOT NULL CHECK (
        occurrence_schema_version='market_evidence.model_attested_occurrence.v1'
    ),
    segment_classification_run_id TEXT NOT NULL,
    structural_manifest_sha256 TEXT NOT NULL,
    validated_response_sha256 TEXT NOT NULL
        CHECK (length(validated_response_sha256)=64
               AND validated_response_sha256 NOT GLOB '*[^0-9a-f]*'),
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    segment_id TEXT NOT NULL,
    logical_observation_id TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL
        CHECK (length(artifact_sha256)=64
               AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
    absolute_character_start INTEGER NOT NULL
        CHECK (absolute_character_start >= 0),
    absolute_character_end INTEGER NOT NULL
        CHECK (absolute_character_end > absolute_character_start),
    absolute_byte_start INTEGER NOT NULL CHECK (absolute_byte_start >= 0),
    absolute_byte_end INTEGER NOT NULL
        CHECK (absolute_byte_end > absolute_byte_start),
    exact_text TEXT NOT NULL CHECK (length(exact_text) > 0),
    classification TEXT NOT NULL CHECK (classification IN (
        'clinic_name','non_clinic','ambiguous'
    )),
    canonical_name TEXT,
    countable_clinic INTEGER NOT NULL CHECK (countable_clinic IN (0,1)),
    occurrence_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(occurrence_sha256)=64
               AND occurrence_sha256 NOT GLOB '*[^0-9a-f]*'),
    occurrence_json TEXT NOT NULL CHECK (json_valid(occurrence_json)),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (
        segment_classification_run_id,
        segment_id,
        absolute_character_start,
        absolute_character_end
    ),
    FOREIGN KEY (segment_classification_run_id, purpose)
        REFERENCES market_segment_classification_processing(
            segment_classification_run_id, purpose
        ) ON DELETE RESTRICT,
    FOREIGN KEY (structural_manifest_sha256, segment_id)
        REFERENCES market_structural_segments(
            structural_manifest_sha256, segment_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (logical_observation_id, purpose)
        REFERENCES market_logical_observations(
            logical_observation_id, purpose
        ) ON DELETE RESTRICT,
    FOREIGN KEY (artifact_sha256, purpose)
        REFERENCES market_artifacts(artifact_sha256, purpose)
        ON DELETE RESTRICT,
    CHECK (
        (classification='clinic_name'
         AND (
             canonical_name IS NULL
             OR length(trim(canonical_name)) > 0
         ))
        OR
        (classification IN ('non_clinic','ambiguous')
         AND countable_clinic=0
         AND canonical_name IS NULL)
    )
);

CREATE TABLE market_segment_visibility_projection (
    segment_projection_id TEXT PRIMARY KEY,
    segment_classification_run_id TEXT NOT NULL,
    logical_observation_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'appeared','measured_not_observed','unknown','not_shown'
    )),
    semantic_occurrence_coverage_state TEXT NOT NULL CHECK (
        semantic_occurrence_coverage_state IN (
            'unproven',
            'not_applicable_proven_nonanswer',
            'complete_strict_envelope'
        )
    ),
    clinic_name_count INTEGER NOT NULL CHECK (clinic_name_count >= 0),
    warning_codes_json TEXT NOT NULL CHECK (
        json_valid(warning_codes_json)
        AND json_type(warning_codes_json)='array'
    ),
    projection_json TEXT NOT NULL CHECK (json_valid(projection_json)),
    projection_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(projection_sha256)=64
               AND projection_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (segment_classification_run_id, logical_observation_id),
    FOREIGN KEY (segment_classification_run_id, purpose)
        REFERENCES market_segment_classification_processing(
            segment_classification_run_id, purpose
        ) ON DELETE RESTRICT,
    FOREIGN KEY (logical_observation_id, purpose)
        REFERENCES market_logical_observations(
            logical_observation_id, purpose
        ) ON DELETE RESTRICT,
    CHECK (
        outcome!='measured_not_observed'
        OR semantic_occurrence_coverage_state='complete_strict_envelope'
    ),
    CHECK (
        outcome!='not_shown'
        OR semantic_occurrence_coverage_state='not_applicable_proven_nonanswer'
    ),
    CHECK (outcome!='appeared' OR clinic_name_count > 0)
);

-- Every v3 record is append-only.  Replay must compare exact hashes rather
-- than modifying historical evidence in place.
CREATE TRIGGER p5_structural_manifests_no_update
BEFORE UPDATE ON market_structural_manifests
BEGIN
    SELECT RAISE(ABORT, 'market structural manifests are immutable');
END;

CREATE TRIGGER p5_structural_manifests_no_delete
BEFORE DELETE ON market_structural_manifests
BEGIN
    SELECT RAISE(ABORT, 'market structural manifests are immutable');
END;

CREATE TRIGGER p5_structural_segments_no_update
BEFORE UPDATE ON market_structural_segments
BEGIN
    SELECT RAISE(ABORT, 'market structural segments are immutable');
END;

CREATE TRIGGER p5_structural_segments_no_delete
BEFORE DELETE ON market_structural_segments
BEGIN
    SELECT RAISE(ABORT, 'market structural segments are immutable');
END;

CREATE TRIGGER p5_structural_hints_no_update
BEFORE UPDATE ON market_structural_hints
BEGIN
    SELECT RAISE(ABORT, 'market structural hints are immutable');
END;

CREATE TRIGGER p5_structural_hints_no_delete
BEFORE DELETE ON market_structural_hints
BEGIN
    SELECT RAISE(ABORT, 'market structural hints are immutable');
END;

CREATE TRIGGER p5_segment_classification_no_update
BEFORE UPDATE ON market_segment_classification_processing
BEGIN
    SELECT RAISE(ABORT, 'segment classification records are immutable');
END;

CREATE TRIGGER p5_segment_classification_no_delete
BEFORE DELETE ON market_segment_classification_processing
BEGIN
    SELECT RAISE(ABORT, 'segment classification records are immutable');
END;

CREATE TRIGGER p5_segment_classification_links_no_update
BEFORE UPDATE ON market_segment_classification_manifests
BEGIN
    SELECT RAISE(ABORT, 'segment classification manifest links are immutable');
END;

CREATE TRIGGER p5_segment_classification_links_no_delete
BEFORE DELETE ON market_segment_classification_manifests
BEGIN
    SELECT RAISE(ABORT, 'segment classification manifest links are immutable');
END;

CREATE TRIGGER p5_model_attested_occurrences_no_update
BEFORE UPDATE ON market_model_attested_occurrences
BEGIN
    SELECT RAISE(ABORT, 'model-attested occurrences are immutable');
END;

CREATE TRIGGER p5_model_attested_occurrences_no_delete
BEFORE DELETE ON market_model_attested_occurrences
BEGIN
    SELECT RAISE(ABORT, 'model-attested occurrences are immutable');
END;

CREATE TRIGGER p5_segment_projection_no_update
BEFORE UPDATE ON market_segment_visibility_projection
BEGIN
    SELECT RAISE(ABORT, 'segment visibility projections are immutable');
END;

CREATE TRIGGER p5_segment_projection_no_delete
BEFORE DELETE ON market_segment_visibility_projection
BEGIN
    SELECT RAISE(ABORT, 'segment visibility projections are immutable');
END;
