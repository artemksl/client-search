-- TEMPORARY DATABASES ONLY.
--
-- P2 completion attestations and treatment-library review records.  This
-- successor migration extends, but never rewrites, the accepted migration 009
-- foundations.

CREATE TABLE market_occurrence_processing (
    occurrence_run_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    state TEXT NOT NULL CHECK (state IN ('completed','incomplete','failed')),
    raw_artifact_manifest_json TEXT NOT NULL CHECK (json_valid(raw_artifact_manifest_json)),
    raw_artifact_manifest_sha256 TEXT NOT NULL
        CHECK (length(raw_artifact_manifest_sha256)=64
               AND raw_artifact_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    offset_contract_version TEXT NOT NULL CHECK (length(trim(offset_contract_version)) > 0),
    extractor_input_manifest_json TEXT NOT NULL
        CHECK (json_valid(extractor_input_manifest_json)),
    extractor_input_manifest_sha256 TEXT NOT NULL
        CHECK (length(extractor_input_manifest_sha256)=64
               AND extractor_input_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    candidate_contract_version TEXT NOT NULL
        CHECK (length(trim(candidate_contract_version)) > 0),
    candidate_manifest_json TEXT NOT NULL CHECK (json_valid(candidate_manifest_json)),
    candidate_manifest_sha256 TEXT NOT NULL
        CHECK (length(candidate_manifest_sha256)=64
               AND candidate_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    coverage_contract_version TEXT NOT NULL
        CHECK (length(trim(coverage_contract_version)) > 0),
    coverage_manifest_json TEXT NOT NULL CHECK (json_valid(coverage_manifest_json)),
    coverage_manifest_sha256 TEXT NOT NULL
        CHECK (length(coverage_manifest_sha256)=64
               AND coverage_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    coverage_complete INTEGER NOT NULL CHECK (coverage_complete IN (0,1)),
    coverage_proof_count INTEGER NOT NULL CHECK (coverage_proof_count >= 0),
    occurrence_manifest_json TEXT NOT NULL CHECK (json_valid(occurrence_manifest_json)),
    occurrence_manifest_sha256 TEXT NOT NULL
        CHECK (length(occurrence_manifest_sha256)=64
               AND occurrence_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    extraction_run_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(extraction_run_sha256)=64
               AND extraction_run_sha256 NOT GLOB '*[^0-9a-f]*'),
    result_count INTEGER NOT NULL CHECK (result_count >= 0),
    failure_code TEXT,
    completed_at TEXT NOT NULL CHECK (julianday(completed_at) IS NOT NULL),
    UNIQUE (occurrence_run_id, purpose),
    FOREIGN KEY (occurrence_run_id, purpose)
        REFERENCES market_occurrence_runs(occurrence_run_id, purpose) ON DELETE RESTRICT,
    CHECK ((state='completed' AND coverage_complete=1 AND failure_code IS NULL)
        OR (state='incomplete' AND coverage_complete=0
            AND length(trim(failure_code)) > 0)
        OR (state='failed' AND coverage_complete=0
            AND length(trim(failure_code)) > 0))
);

CREATE TABLE market_occurrence_candidates (
    candidate_id TEXT PRIMARY KEY,
    occurrence_run_id TEXT NOT NULL,
    logical_observation_id TEXT NOT NULL,
    coverage_evidence_id TEXT NOT NULL CHECK (length(trim(coverage_evidence_id)) > 0),
    artifact_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    literal_name TEXT NOT NULL CHECK (length(literal_name) > 0),
    exact_excerpt TEXT NOT NULL CHECK (literal_name=exact_excerpt),
    character_start INTEGER NOT NULL CHECK (character_start >= 0),
    character_end INTEGER NOT NULL CHECK (character_end > character_start),
    byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
    byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
    raw_answer_sha256 TEXT NOT NULL,
    candidate_contract_version TEXT NOT NULL
        CHECK (length(trim(candidate_contract_version)) > 0),
    derivation_rule TEXT NOT NULL CHECK (length(trim(derivation_rule)) > 0),
    candidate_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(candidate_sha256)=64
               AND candidate_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (candidate_id,purpose),
    UNIQUE (occurrence_run_id,logical_observation_id,character_start,character_end),
    FOREIGN KEY (occurrence_run_id,purpose)
        REFERENCES market_occurrence_runs(occurrence_run_id,purpose) ON DELETE RESTRICT,
    FOREIGN KEY (logical_observation_id,purpose)
        REFERENCES market_logical_observations(logical_observation_id,purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_sha256,purpose)
        REFERENCES market_artifacts(artifact_sha256,purpose) ON DELETE RESTRICT,
    CHECK (artifact_sha256=raw_answer_sha256)
);

CREATE TABLE market_literal_occurrence_details (
    occurrence_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    candidate_id TEXT NOT NULL,
    coverage_evidence_id TEXT NOT NULL CHECK (length(trim(coverage_evidence_id)) > 0),
    literal_name TEXT NOT NULL CHECK (length(literal_name) > 0),
    character_start INTEGER NOT NULL CHECK (character_start >= 0),
    character_end INTEGER NOT NULL CHECK (character_end > character_start),
    exact_excerpt TEXT NOT NULL CHECK (length(exact_excerpt) > 0),
    raw_answer_sha256 TEXT NOT NULL
        CHECK (length(raw_answer_sha256)=64
               AND raw_answer_sha256 NOT GLOB '*[^0-9a-f]*'),
    offset_contract_version TEXT NOT NULL CHECK (length(trim(offset_contract_version)) > 0),
    detail_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(detail_sha256)=64
               AND detail_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (occurrence_id, purpose),
    FOREIGN KEY (occurrence_id, purpose)
        REFERENCES market_literal_occurrences(occurrence_id, purpose) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_id, purpose)
        REFERENCES market_occurrence_candidates(candidate_id, purpose) ON DELETE RESTRICT
);

CREATE TABLE market_occurrence_coverage_proofs (
    coverage_proof_sha256 TEXT NOT NULL
        CHECK (length(coverage_proof_sha256)=64
               AND coverage_proof_sha256 NOT GLOB '*[^0-9a-f]*'),
    occurrence_run_id TEXT NOT NULL,
    logical_observation_id TEXT NOT NULL,
    coverage_evidence_id TEXT NOT NULL CHECK (length(trim(coverage_evidence_id)) > 0),
    artifact_sha256 TEXT NOT NULL,
    raw_answer_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length > 0),
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    coverage_contract_version TEXT NOT NULL
        CHECK (length(trim(coverage_contract_version)) > 0),
    state TEXT NOT NULL CHECK (state IN ('complete','incomplete')),
    proof_kind TEXT NOT NULL CHECK (proof_kind IN (
        'strict-structured-occurrence-envelope','unstructured-candidate-discovery'
    )),
    candidate_ids_json TEXT NOT NULL
        CHECK (json_valid(candidate_ids_json)
               AND json_type(candidate_ids_json)='array'),
    proof_manifest_json TEXT NOT NULL CHECK (json_valid(proof_manifest_json)),
    failure_code TEXT,
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    PRIMARY KEY (occurrence_run_id,coverage_proof_sha256),
    UNIQUE (occurrence_run_id,logical_observation_id),
    FOREIGN KEY (occurrence_run_id,purpose)
        REFERENCES market_occurrence_runs(occurrence_run_id,purpose) ON DELETE RESTRICT,
    FOREIGN KEY (logical_observation_id,purpose)
        REFERENCES market_logical_observations(logical_observation_id,purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_sha256,purpose)
        REFERENCES market_artifacts(artifact_sha256,purpose) ON DELETE RESTRICT,
    CHECK (artifact_sha256=raw_answer_sha256),
    CHECK ((state='complete'
            AND proof_kind='strict-structured-occurrence-envelope'
            AND failure_code IS NULL)
        OR (state='incomplete'
            AND proof_kind='unstructured-candidate-discovery'
            AND length(trim(failure_code)) > 0))
);

CREATE TABLE market_classification_requests (
    classification_run_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    occurrence_manifest_sha256 TEXT NOT NULL
        CHECK (length(occurrence_manifest_sha256)=64
               AND occurrence_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    raw_artifact_manifest_sha256 TEXT NOT NULL
        CHECK (length(raw_artifact_manifest_sha256)=64
               AND raw_artifact_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    occurrence_coverage_manifest_sha256 TEXT NOT NULL
        CHECK (length(occurrence_coverage_manifest_sha256)=64
               AND occurrence_coverage_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    occurrence_coverage_complete INTEGER NOT NULL
        CHECK (occurrence_coverage_complete IN (0,1)),
    source_evidence_ids_json TEXT NOT NULL
        CHECK (json_valid(source_evidence_ids_json)
               AND json_type(source_evidence_ids_json)='array'
               AND json_array_length(source_evidence_ids_json) > 0),
    source_occurrence_coverage_json TEXT NOT NULL
        CHECK (json_valid(source_occurrence_coverage_json)
               AND json_type(source_occurrence_coverage_json)='array'
               AND json_array_length(source_occurrence_coverage_json) > 0),
    batching_contract_version TEXT NOT NULL CHECK (length(trim(batching_contract_version)) > 0),
    request_bytes BLOB NOT NULL CHECK (length(request_bytes) > 0),
    request_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(request_sha256)=64
               AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    prompt_version TEXT NOT NULL CHECK (length(trim(prompt_version)) > 0),
    prompt_sha256 TEXT NOT NULL
        CHECK (length(prompt_sha256)=64 AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'),
    model_contract TEXT NOT NULL CHECK (length(trim(model_contract)) > 0),
    observed_model_epoch TEXT NOT NULL CHECK (length(trim(observed_model_epoch)) > 0),
    response_schema_version TEXT NOT NULL
        CHECK (response_schema_version='market_evidence.classification_response.v1'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (classification_run_id, purpose),
    FOREIGN KEY (classification_run_id, purpose)
        REFERENCES market_classification_runs(classification_run_id, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE market_classification_processing (
    classification_run_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    state TEXT NOT NULL CHECK (state IN ('completed','failed')),
    raw_response_artifact_sha256 TEXT,
    raw_response_sha256 TEXT NOT NULL
        CHECK (length(raw_response_sha256)=64
               AND raw_response_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_manifest_json TEXT NOT NULL CHECK (json_valid(response_manifest_json)),
    response_manifest_sha256 TEXT NOT NULL
        CHECK (length(response_manifest_sha256)=64
               AND response_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    result_count INTEGER NOT NULL CHECK (result_count >= 0),
    failure_code TEXT,
    completed_at TEXT NOT NULL CHECK (julianday(completed_at) IS NOT NULL),
    UNIQUE (classification_run_id, purpose),
    FOREIGN KEY (classification_run_id, purpose)
        REFERENCES market_classification_runs(classification_run_id, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (raw_response_artifact_sha256, purpose)
        REFERENCES market_artifacts(artifact_sha256, purpose) ON DELETE RESTRICT,
    CHECK ((state='completed' AND failure_code IS NULL
            AND raw_response_artifact_sha256 IS NOT NULL)
        OR (state='failed' AND length(trim(failure_code)) > 0))
);

-- P2-A010: single-winner idempotency lock acquired before the external
-- classifier call.  It is intentionally standalone (no run FK): the claim is
-- taken before the classification run row exists.
CREATE TABLE market_classification_claims (
    classification_run_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    request_sha256 TEXT NOT NULL
        CHECK (length(request_sha256)=64
               AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    claimed_at TEXT NOT NULL CHECK (julianday(claimed_at) IS NOT NULL),
    UNIQUE (classification_run_id, purpose)
);

CREATE TABLE market_classification_result_details (
    classification_result_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    literal_name TEXT NOT NULL CHECK (length(literal_name) > 0),
    canonical_name TEXT,
    detail_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(detail_sha256)=64 AND detail_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (classification_result_id, purpose),
    FOREIGN KEY (classification_result_id, purpose)
        REFERENCES market_classification_results(classification_result_id, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE market_projection_processing (
    projection_run_id TEXT PRIMARY KEY,
    occurrence_run_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    state TEXT NOT NULL CHECK (state IN ('completed','failed')),
    alias_set_sha256 TEXT NOT NULL
        CHECK (length(alias_set_sha256)=64 AND alias_set_sha256 NOT GLOB '*[^0-9a-f]*'),
    branch_identity_version TEXT NOT NULL CHECK (length(trim(branch_identity_version)) > 0),
    projection_algorithm_version TEXT NOT NULL CHECK (length(trim(projection_algorithm_version)) > 0),
    input_manifest_json TEXT NOT NULL CHECK (json_valid(input_manifest_json)),
    input_manifest_sha256 TEXT NOT NULL
        CHECK (length(input_manifest_sha256)=64
               AND input_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    output_manifest_json TEXT NOT NULL CHECK (json_valid(output_manifest_json)),
    output_manifest_sha256 TEXT NOT NULL
        CHECK (length(output_manifest_sha256)=64
               AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    processed_occurrence_manifest_sha256 TEXT NOT NULL
        CHECK (length(processed_occurrence_manifest_sha256)=64
               AND processed_occurrence_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    match_count INTEGER NOT NULL CHECK (match_count >= 0),
    failure_code TEXT,
    completed_at TEXT NOT NULL CHECK (julianday(completed_at) IS NOT NULL),
    UNIQUE (projection_run_id, purpose),
    FOREIGN KEY (projection_run_id, purpose)
        REFERENCES market_projection_runs(projection_run_id, purpose) ON DELETE RESTRICT,
    FOREIGN KEY (occurrence_run_id, purpose)
        REFERENCES market_occurrence_runs(occurrence_run_id, purpose) ON DELETE RESTRICT,
    CHECK ((state='completed' AND failure_code IS NULL)
        OR (state='failed' AND length(trim(failure_code)) > 0))
);

CREATE TABLE market_projection_decisions (
    decision_id TEXT PRIMARY KEY,
    projection_run_id TEXT NOT NULL,
    occurrence_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose='market_discovery'),
    decision_state TEXT NOT NULL CHECK (decision_state IN (
        'matched','unmatched','ambiguous','account_level_review'
    )),
    decision_manifest_json TEXT NOT NULL CHECK (json_valid(decision_manifest_json)),
    decision_manifest_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(decision_manifest_sha256)=64
               AND decision_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (projection_run_id, occurrence_id),
    UNIQUE (decision_id, purpose),
    FOREIGN KEY (projection_run_id, purpose)
        REFERENCES market_projection_runs(projection_run_id, purpose) ON DELETE RESTRICT,
    FOREIGN KEY (occurrence_id, purpose)
        REFERENCES market_literal_occurrences(occurrence_id, purpose) ON DELETE RESTRICT
);

-- P2-A007: the exact reviewed identity release for one projection run, so the
-- decision loader can rebuild a genuine immutable projection run rather than
-- trusting persisted decision rows.
CREATE TABLE market_projection_identity_releases (
    projection_run_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    branch_identity_version TEXT NOT NULL
        CHECK (length(trim(branch_identity_version)) > 0),
    alias_set_json TEXT NOT NULL
        CHECK (json_valid(alias_set_json)
               AND json_type(alias_set_json)='array'),
    alias_set_sha256 TEXT NOT NULL
        CHECK (length(alias_set_sha256)=64
               AND alias_set_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (projection_run_id, purpose),
    FOREIGN KEY (projection_run_id, purpose)
        REFERENCES market_projection_runs(projection_run_id, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE treatment_review_sessions (
    session_id TEXT PRIMARY KEY,
    identity_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(identity_sha256)=64 AND identity_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version TEXT NOT NULL CHECK (schema_version='treatment_library.review_session.v1'),
    source_excerpt TEXT NOT NULL CHECK (length(source_excerpt) > 0),
    source_reference TEXT NOT NULL CHECK (length(trim(source_reference)) > 0),
    source_url TEXT,
    source_content_sha256 TEXT NOT NULL
        CHECK (length(source_content_sha256)=64
               AND source_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    branch_id TEXT NOT NULL CHECK (length(trim(branch_id)) > 0),
    campaign_revision_id TEXT NOT NULL CHECK (length(trim(campaign_revision_id)) > 0),
    catalogue_release_id TEXT NOT NULL,
    catalogue_payload_sha256 TEXT NOT NULL
        CHECK (length(catalogue_payload_sha256)=64
               AND catalogue_payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    input_manifest_json TEXT NOT NULL CHECK (json_valid(input_manifest_json)),
    input_manifest_sha256 TEXT NOT NULL
        CHECK (length(input_manifest_sha256)=64
               AND input_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    initial_state TEXT NOT NULL CHECK (initial_state IN ('deterministic_complete','ai_required')),
    submitted_at TEXT NOT NULL CHECK (julianday(submitted_at) IS NOT NULL),
    UNIQUE (session_id,catalogue_release_id,catalogue_payload_sha256),
    FOREIGN KEY (catalogue_release_id)
        REFERENCES treatment_catalog_releases(id) ON DELETE RESTRICT
);

CREATE TABLE treatment_review_input_terms (
    input_term_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES treatment_review_sessions(session_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    original_phrase TEXT NOT NULL CHECK (length(original_phrase) > 0),
    phrase_sha256 TEXT NOT NULL
        CHECK (length(phrase_sha256)=64 AND phrase_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (session_id, ordinal),
    UNIQUE (session_id, original_phrase)
);

CREATE TABLE treatment_review_deterministic_results (
    input_term_id TEXT PRIMARY KEY REFERENCES treatment_review_input_terms(input_term_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('matched','review_required','ambiguous','no_match')),
    target_concept_ids_json TEXT NOT NULL CHECK (json_valid(target_concept_ids_json)),
    target_search_intent_ids_json TEXT NOT NULL CHECK (json_valid(target_search_intent_ids_json)),
    matched_term_ids_json TEXT NOT NULL CHECK (json_valid(matched_term_ids_json)),
    campaign_selectable INTEGER NOT NULL CHECK (campaign_selectable IN (0,1)),
    recommendation_prompt_eligible INTEGER NOT NULL
        CHECK (recommendation_prompt_eligible IN (0,1)),
    result_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(result_sha256)=64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL)
);

CREATE TABLE treatment_review_ai_preflights (
    preflight_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES treatment_review_sessions(session_id) ON DELETE RESTRICT,
    preflight_sha256 TEXT NOT NULL
        CHECK (length(preflight_sha256)=64 AND preflight_sha256 NOT GLOB '*[^0-9a-f]*'),
    request_bytes BLOB NOT NULL CHECK (length(request_bytes) > 0),
    request_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    unresolved_manifest_sha256 TEXT NOT NULL
        CHECK (length(unresolved_manifest_sha256)=64
               AND unresolved_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    catalogue_release_id TEXT NOT NULL,
    catalogue_payload_sha256 TEXT NOT NULL,
    model_contract TEXT NOT NULL CHECK (length(trim(model_contract)) > 0),
    observed_model_epoch TEXT NOT NULL CHECK (length(trim(observed_model_epoch)) > 0),
    prompt_version TEXT NOT NULL CHECK (length(trim(prompt_version)) > 0),
    prompt_sha256 TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL CHECK (estimated_input_tokens >= 0),
    estimated_cost_minor_units INTEGER NOT NULL CHECK (estimated_cost_minor_units >= 0),
    currency_code TEXT NOT NULL CHECK (length(currency_code)=3 AND currency_code=upper(currency_code)),
    maximum_input_tokens INTEGER NOT NULL CHECK (maximum_input_tokens > 0),
    maximum_request_bytes INTEGER NOT NULL CHECK (maximum_request_bytes > 0),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (session_id, request_sha256),
    UNIQUE (session_id, preflight_sha256),
    FOREIGN KEY (session_id,catalogue_release_id,catalogue_payload_sha256)
        REFERENCES treatment_review_sessions(
            session_id,catalogue_release_id,catalogue_payload_sha256
        ) ON DELETE RESTRICT,
    FOREIGN KEY (catalogue_release_id)
        REFERENCES treatment_catalog_releases(id) ON DELETE RESTRICT
);

CREATE TABLE treatment_review_ai_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES treatment_review_sessions(session_id) ON DELETE RESTRICT,
    preflight_id TEXT NOT NULL UNIQUE REFERENCES treatment_review_ai_preflights(preflight_id) ON DELETE RESTRICT,
    request_sha256 TEXT NOT NULL,
    catalogue_payload_sha256 TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL CHECK (estimated_input_tokens >= 0),
    estimated_cost_minor_units INTEGER NOT NULL CHECK (estimated_cost_minor_units >= 0),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    confirmation_sha256 TEXT NOT NULL UNIQUE,
    confirmed_at TEXT NOT NULL CHECK (julianday(confirmed_at) IS NOT NULL),
    UNIQUE (session_id, idempotency_key)
);

CREATE TABLE treatment_review_ai_call_claims (
    claim_id TEXT PRIMARY KEY,
    confirmation_id TEXT NOT NULL UNIQUE
        REFERENCES treatment_review_ai_confirmations(confirmation_id) ON DELETE RESTRICT,
    request_sha256 TEXT NOT NULL
        CHECK (length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    claimed_at TEXT NOT NULL CHECK (julianday(claimed_at) IS NOT NULL)
);

CREATE TABLE treatment_review_ai_response_artifacts (
    response_artifact_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL UNIQUE
        REFERENCES treatment_review_ai_call_claims(claim_id) ON DELETE RESTRICT,
    confirmation_id TEXT NOT NULL UNIQUE
        REFERENCES treatment_review_ai_confirmations(confirmation_id) ON DELETE RESTRICT,
    raw_response_bytes BLOB NOT NULL CHECK (length(raw_response_bytes) > 0),
    raw_response_sha256 TEXT NOT NULL
        CHECK (length(raw_response_sha256)=64
               AND raw_response_sha256 NOT GLOB '*[^0-9a-f]*'),
    validation_state TEXT NOT NULL CHECK (validation_state IN ('valid','invalid')),
    failure_code TEXT,
    result_manifest_json TEXT NOT NULL CHECK (json_valid(result_manifest_json)),
    result_manifest_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    CHECK ((validation_state='valid' AND failure_code IS NULL)
        OR (validation_state='invalid' AND length(trim(failure_code)) > 0))
);

CREATE TABLE treatment_review_ai_call_outcomes (
    claim_id TEXT PRIMARY KEY
        REFERENCES treatment_review_ai_call_claims(claim_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('completed','ambiguous')),
    response_artifact_id TEXT UNIQUE
        REFERENCES treatment_review_ai_response_artifacts(response_artifact_id)
        ON DELETE RESTRICT,
    failure_code TEXT,
    completed_at TEXT NOT NULL CHECK (julianday(completed_at) IS NOT NULL),
    CHECK ((state='completed' AND response_artifact_id IS NOT NULL
            AND failure_code IS NULL)
        OR (state='ambiguous' AND response_artifact_id IS NULL
            AND length(trim(failure_code)) > 0))
);

CREATE TABLE treatment_review_ai_results (
    result_id TEXT PRIMARY KEY,
    response_artifact_id TEXT NOT NULL
        REFERENCES treatment_review_ai_response_artifacts(response_artifact_id)
        ON DELETE RESTRICT,
    input_term_id TEXT NOT NULL REFERENCES treatment_review_input_terms(input_term_id) ON DELETE RESTRICT,
    classification TEXT NOT NULL CHECK (classification IN (
        'existing_treatment','alias_or_spelling','brand_or_device','subtype',
        'related_but_distinct','patient_concern_or_broad_category',
        'package_containing_multiple_treatments','genuinely_new_treatment',
        'insufficient_evidence'
    )),
    target_concept_ids_json TEXT NOT NULL CHECK (json_valid(target_concept_ids_json)),
    target_search_intent_ids_json TEXT NOT NULL CHECK (json_valid(target_search_intent_ids_json)),
    suggested_term_kind TEXT,
    suggested_canonical_name TEXT,
    confidence TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    reason_summary TEXT NOT NULL CHECK (length(reason_summary) > 0),
    evidence_limitations_json TEXT NOT NULL CHECK (json_valid(evidence_limitations_json)),
    result_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (response_artifact_id, input_term_id),
    CHECK ((classification='genuinely_new_treatment' AND suggested_canonical_name IS NOT NULL)
        OR (classification<>'genuinely_new_treatment' AND suggested_canonical_name IS NULL))
);

CREATE TABLE treatment_review_decision_events (
    decision_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES treatment_review_sessions(session_id) ON DELETE RESTRICT,
    input_term_id TEXT REFERENCES treatment_review_input_terms(input_term_id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK (action IN (
        'map_existing_term','approve_typed_term','record_branch_offering',
        'propose_search_intent','propose_provisional_treatment',
        'request_more_evidence','defer','reject'
    )),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_sha256 TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    decided_at TEXT NOT NULL CHECK (julianday(decided_at) IS NOT NULL),
    UNIQUE (session_id, idempotency_key)
);

CREATE TABLE treatment_review_actions (
    action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE
        REFERENCES treatment_review_decision_events(decision_id) ON DELETE RESTRICT,
    action_kind TEXT NOT NULL,
    action_state TEXT NOT NULL CHECK (action_state IN ('recorded','proposed')),
    target_payload_json TEXT NOT NULL CHECK (json_valid(target_payload_json)),
    catalogue_mutation_applied INTEGER NOT NULL DEFAULT 0
        CHECK (catalogue_mutation_applied=0),
    collection_queued INTEGER NOT NULL DEFAULT 0 CHECK (collection_queued=0),
    campaign_selectable INTEGER NOT NULL DEFAULT 0 CHECK (campaign_selectable=0),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL)
);

CREATE TRIGGER p2_occurrence_processing_matches_header
BEFORE INSERT ON market_occurrence_processing
WHEN NOT EXISTS (
    SELECT 1 FROM market_occurrence_runs run
    WHERE run.occurrence_run_id=NEW.occurrence_run_id
      AND run.purpose=NEW.purpose
      AND run.output_manifest_sha256=NEW.occurrence_manifest_sha256
)
BEGIN SELECT RAISE(ABORT, 'occurrence completion must match its exact header manifest'); END;

CREATE TRIGGER p2_occurrence_detail_matches_parent
BEFORE INSERT ON market_literal_occurrence_details
WHEN NOT EXISTS (
    SELECT 1 FROM market_literal_occurrences occurrence
    JOIN market_occurrence_candidates candidate
      ON candidate.candidate_id=NEW.candidate_id
     AND candidate.purpose=NEW.purpose
    WHERE occurrence.occurrence_id=NEW.occurrence_id
      AND occurrence.purpose=NEW.purpose
      AND occurrence.exact_text=NEW.exact_excerpt
      AND occurrence.logical_observation_id=candidate.logical_observation_id
      AND occurrence.artifact_sha256=candidate.artifact_sha256
      AND NEW.coverage_evidence_id=candidate.coverage_evidence_id
      AND NEW.literal_name=candidate.literal_name
      AND NEW.character_start=candidate.character_start
      AND NEW.character_end=candidate.character_end
      AND occurrence.byte_start=candidate.byte_start
      AND occurrence.byte_end=candidate.byte_end
)
BEGIN SELECT RAISE(ABORT, 'occurrence detail must match its immutable occurrence'); END;

CREATE TRIGGER p2_occurrence_coverage_proof_matches_children
BEFORE INSERT ON market_occurrence_coverage_proofs
WHEN NOT EXISTS (
    SELECT 1
    FROM market_occurrence_runs run
    JOIN market_logical_observations observation
      ON observation.logical_observation_id=NEW.logical_observation_id
     AND observation.window_id=run.window_id
     AND observation.purpose=run.purpose
    JOIN market_observation_acceptances acceptance
      ON acceptance.logical_observation_id=observation.logical_observation_id
     AND acceptance.purpose=observation.purpose
    JOIN market_artifact_bindings binding
      ON binding.attempt_id=acceptance.attempt_id
     AND binding.logical_observation_id=observation.logical_observation_id
     AND binding.purpose=observation.purpose
     AND binding.artifact_role='raw_answer'
    JOIN market_artifacts artifact
      ON artifact.artifact_sha256=binding.artifact_sha256
     AND artifact.purpose=binding.purpose
    WHERE run.occurrence_run_id=NEW.occurrence_run_id
      AND run.purpose=NEW.purpose
      AND binding.artifact_sha256=NEW.artifact_sha256
      AND artifact.byte_length=NEW.byte_length
      AND json_array_length(NEW.candidate_ids_json)=(
          SELECT count(*) FROM market_occurrence_candidates candidate
          WHERE candidate.occurrence_run_id=NEW.occurrence_run_id
            AND candidate.logical_observation_id=NEW.logical_observation_id
            AND candidate.purpose=NEW.purpose
      )
      AND NOT EXISTS (
          SELECT 1 FROM market_occurrence_candidates candidate
          WHERE candidate.occurrence_run_id=NEW.occurrence_run_id
            AND candidate.logical_observation_id=NEW.logical_observation_id
            AND candidate.purpose=NEW.purpose
            AND candidate.candidate_id NOT IN (
                SELECT value FROM json_each(NEW.candidate_ids_json)
            )
      )
      AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.candidate_ids_json) supplied
          WHERE supplied.value NOT IN (
              SELECT candidate.candidate_id
              FROM market_occurrence_candidates candidate
              WHERE candidate.occurrence_run_id=NEW.occurrence_run_id
                AND candidate.logical_observation_id=NEW.logical_observation_id
                AND candidate.purpose=NEW.purpose
          )
      )
)
BEGIN SELECT RAISE(ABORT, 'coverage proof must match exact raw and candidate children'); END;

CREATE TRIGGER p2_classification_request_matches_occurrences
BEFORE INSERT ON market_classification_requests
WHEN NOT EXISTS (
    SELECT 1
    FROM market_classification_runs run
    JOIN market_occurrence_processing processing
      ON processing.occurrence_run_id=run.occurrence_run_id
     AND processing.purpose=run.purpose
    WHERE run.classification_run_id=NEW.classification_run_id
      AND run.purpose=NEW.purpose
      AND run.input_manifest_sha256=NEW.occurrence_manifest_sha256
      AND processing.occurrence_manifest_sha256=NEW.occurrence_manifest_sha256
      AND processing.raw_artifact_manifest_sha256=
          NEW.raw_artifact_manifest_sha256
      AND processing.coverage_manifest_sha256=
          NEW.occurrence_coverage_manifest_sha256
      AND processing.coverage_complete=NEW.occurrence_coverage_complete
      AND processing.state IN ('completed','incomplete')
      AND json_array_length(NEW.source_evidence_ids_json)=
          json_array_length(processing.raw_artifact_manifest_json,'$.artifacts')
      AND json_array_length(NEW.source_occurrence_coverage_json)=
          processing.coverage_proof_count
      AND NOT EXISTS (
          SELECT 1
          FROM json_each(
              processing.raw_artifact_manifest_json,'$.artifacts'
          ) artifact
          WHERE json_extract(artifact.value,'$.coverage_evidence_id')
                NOT IN (SELECT value FROM json_each(NEW.source_evidence_ids_json))
      )
      AND NOT EXISTS (
          SELECT 1 FROM market_occurrence_coverage_proofs proof
          WHERE proof.occurrence_run_id=run.occurrence_run_id
            AND NOT EXISTS (
                SELECT 1 FROM json_each(NEW.source_occurrence_coverage_json) supplied
                WHERE json_extract(supplied.value,'$.coverage_evidence_id')=
                      proof.coverage_evidence_id
                  AND json_extract(supplied.value,'$.complete')=
                      (proof.state='complete')
                  AND json_extract(supplied.value,'$.coverage_proof_sha256')=
                      proof.coverage_proof_sha256
            )
      )
)
BEGIN SELECT RAISE(ABORT, 'classification request requires completed exact occurrences'); END;

CREATE TRIGGER p2_classification_processing_matches_header
BEFORE INSERT ON market_classification_processing
WHEN NOT EXISTS (
    SELECT 1 FROM market_classification_runs run
    WHERE run.classification_run_id=NEW.classification_run_id
      AND run.purpose=NEW.purpose
      AND run.output_manifest_sha256=NEW.response_manifest_sha256
)
BEGIN SELECT RAISE(ABORT, 'classification completion must match its exact header manifest'); END;

CREATE TRIGGER p2_projection_processing_matches_chain
BEFORE INSERT ON market_projection_processing
WHEN NOT EXISTS (
    SELECT 1
    FROM market_projection_runs projection
    JOIN market_classification_runs classification
      ON classification.classification_run_id=projection.classification_run_id
     AND classification.purpose=projection.purpose
    JOIN market_classification_processing processing
      ON processing.classification_run_id=classification.classification_run_id
     AND processing.purpose=classification.purpose
    WHERE projection.projection_run_id=NEW.projection_run_id
      AND classification.occurrence_run_id=NEW.occurrence_run_id
      AND projection.purpose=NEW.purpose
      AND projection.input_manifest_sha256=classification.output_manifest_sha256
      AND projection.output_manifest_sha256=NEW.output_manifest_sha256
      AND processing.state='completed'
)
BEGIN SELECT RAISE(ABORT, 'projection completion requires the exact completed classification chain'); END;

CREATE TRIGGER p2_occurrence_completion_requires_exact_count
BEFORE INSERT ON market_occurrence_processing
WHEN NEW.state IN ('completed','incomplete') AND (
    NEW.candidate_count <> (
        SELECT count(*) FROM market_occurrence_candidates
        WHERE occurrence_run_id=NEW.occurrence_run_id AND purpose=NEW.purpose
    )
    OR NEW.candidate_count <>
       json_array_length(NEW.candidate_manifest_json,'$.candidates')
    OR NEW.result_count <> (
        SELECT count(*) FROM market_literal_occurrences
        WHERE occurrence_run_id=NEW.occurrence_run_id AND purpose=NEW.purpose
    )
    OR NEW.result_count <> NEW.candidate_count
    OR NEW.result_count <> json_array_length(NEW.occurrence_manifest_json,'$.occurrences')
    OR NEW.coverage_proof_count <> 3
    OR NEW.coverage_proof_count <> (
        SELECT count(*) FROM market_occurrence_coverage_proofs
        WHERE occurrence_run_id=NEW.occurrence_run_id AND purpose=NEW.purpose
    )
    OR NEW.coverage_proof_count <>
       json_array_length(NEW.coverage_manifest_json,'$.proofs')
    OR NEW.coverage_complete <> (NOT EXISTS (
        SELECT 1 FROM market_occurrence_coverage_proofs
        WHERE occurrence_run_id=NEW.occurrence_run_id AND purpose=NEW.purpose
          AND state<>'complete'
    ))
)
BEGIN SELECT RAISE(ABORT, 'occurrence completion requires exact coverage and child counts'); END;

CREATE TRIGGER treatment_review_session_catalogue_binding
BEFORE INSERT ON treatment_review_sessions
WHEN NOT EXISTS (
    SELECT 1 FROM treatment_catalog_releases release
    WHERE release.id=NEW.catalogue_release_id
      AND release.payload_sha256=NEW.catalogue_payload_sha256
      AND release.lifecycle_state='active'
      AND release.review_state='approved'
)
BEGIN SELECT RAISE(ABORT, 'review session requires its exact approved catalogue'); END;

CREATE TRIGGER treatment_review_preflight_catalogue_binding
BEFORE INSERT ON treatment_review_ai_preflights
WHEN NOT EXISTS (
    SELECT 1 FROM treatment_review_sessions session
    WHERE session.session_id=NEW.session_id
      AND session.catalogue_release_id=NEW.catalogue_release_id
      AND session.catalogue_payload_sha256=NEW.catalogue_payload_sha256
)
BEGIN SELECT RAISE(ABORT, 'AI preflight must retain its session catalogue'); END;

CREATE TRIGGER treatment_review_confirmation_session_binding
BEFORE INSERT ON treatment_review_ai_confirmations
WHEN NOT EXISTS (
    SELECT 1 FROM treatment_review_ai_preflights preflight
    WHERE preflight.preflight_id=NEW.preflight_id
      AND preflight.session_id=NEW.session_id
      AND preflight.request_sha256=NEW.request_sha256
      AND preflight.catalogue_payload_sha256=NEW.catalogue_payload_sha256
)
BEGIN SELECT RAISE(ABORT, 'AI confirmation must retain its exact preflight'); END;

CREATE TRIGGER p2_classification_completion_requires_exact_count
BEFORE INSERT ON market_classification_processing
WHEN NEW.state='completed' AND (
    NEW.result_count <> (
        SELECT count(*) FROM market_classification_results
        WHERE classification_run_id=NEW.classification_run_id AND purpose=NEW.purpose
    )
    OR NEW.result_count <> json_array_length(NEW.response_manifest_json,'$.results')
)
BEGIN SELECT RAISE(ABORT, 'classification completion requires its exact child count'); END;

-- P2-A008: exactly one classification result per (run, occurrence); a second
-- row with a conflicting classification for an already-classified occurrence is
-- rejected outright.
CREATE UNIQUE INDEX p2_classification_results_unique_occurrence
    ON market_classification_results (classification_run_id, occurrence_id, purpose);

-- P2-A008: once a classification run is recorded in processing, its result and
-- result-detail rows are frozen; a late or conflicting row cannot be appended.
-- Normal completion inserts results and details before the processing row, so
-- these triggers never fire on the legitimate path.
CREATE TRIGGER p2_classification_results_frozen_after_completion
BEFORE INSERT ON market_classification_results
WHEN EXISTS (
    SELECT 1 FROM market_classification_processing processing
    WHERE processing.classification_run_id=NEW.classification_run_id
      AND processing.purpose=NEW.purpose
)
BEGIN SELECT RAISE(ABORT, 'classification results are frozen once the run is recorded'); END;

CREATE TRIGGER p2_classification_result_details_frozen_after_completion
BEFORE INSERT ON market_classification_result_details
WHEN EXISTS (
    SELECT 1
    FROM market_classification_results result
    JOIN market_classification_processing processing
      ON processing.classification_run_id=result.classification_run_id
     AND processing.purpose=result.purpose
    WHERE result.classification_result_id=NEW.classification_result_id
      AND result.purpose=NEW.purpose
)
BEGIN SELECT RAISE(ABORT, 'classification result details are frozen once the run is recorded'); END;

CREATE TRIGGER p2_projection_completion_requires_exact_children
BEFORE INSERT ON market_projection_processing
WHEN NEW.state='completed' AND (
    NEW.match_count <> (
        SELECT count(*) FROM market_projection_matches
        WHERE projection_run_id=NEW.projection_run_id AND purpose=NEW.purpose
    )
    OR (SELECT count(*) FROM market_projection_decisions
        WHERE projection_run_id=NEW.projection_run_id AND purpose=NEW.purpose)
       <> json_array_length(NEW.output_manifest_json,'$.decisions')
)
BEGIN SELECT RAISE(ABORT, 'projection completion requires exact decisions and matches'); END;

-- All P2 evidence, review inputs, AI artifacts, decisions and actions are
-- immutable or append-only.  Operational collection jobs remain exclusively
-- owned by migration 009 and are never written by P2 review/projection code.
CREATE TRIGGER p2_occurrence_processing_no_update BEFORE UPDATE ON market_occurrence_processing BEGIN SELECT RAISE(ABORT,'P2 occurrence processing is immutable'); END;
CREATE TRIGGER p2_occurrence_processing_no_delete BEFORE DELETE ON market_occurrence_processing BEGIN SELECT RAISE(ABORT,'P2 occurrence processing is immutable'); END;
CREATE TRIGGER p2_occurrence_details_no_update BEFORE UPDATE ON market_literal_occurrence_details BEGIN SELECT RAISE(ABORT,'P2 occurrence details are immutable'); END;
CREATE TRIGGER p2_occurrence_details_no_delete BEFORE DELETE ON market_literal_occurrence_details BEGIN SELECT RAISE(ABORT,'P2 occurrence details are immutable'); END;
CREATE TRIGGER p2_occurrence_candidates_no_update BEFORE UPDATE ON market_occurrence_candidates BEGIN SELECT RAISE(ABORT,'P2 occurrence candidates are immutable'); END;
CREATE TRIGGER p2_occurrence_candidates_no_delete BEFORE DELETE ON market_occurrence_candidates BEGIN SELECT RAISE(ABORT,'P2 occurrence candidates are immutable'); END;
CREATE TRIGGER p2_occurrence_coverage_no_update BEFORE UPDATE ON market_occurrence_coverage_proofs BEGIN SELECT RAISE(ABORT,'P2 occurrence coverage proofs are immutable'); END;
CREATE TRIGGER p2_occurrence_coverage_no_delete BEFORE DELETE ON market_occurrence_coverage_proofs BEGIN SELECT RAISE(ABORT,'P2 occurrence coverage proofs are immutable'); END;
CREATE TRIGGER p2_classification_requests_no_update BEFORE UPDATE ON market_classification_requests BEGIN SELECT RAISE(ABORT,'P2 classification requests are immutable'); END;
CREATE TRIGGER p2_classification_requests_no_delete BEFORE DELETE ON market_classification_requests BEGIN SELECT RAISE(ABORT,'P2 classification requests are immutable'); END;
CREATE TRIGGER p2_classification_processing_no_update BEFORE UPDATE ON market_classification_processing BEGIN SELECT RAISE(ABORT,'P2 classification processing is immutable'); END;
CREATE TRIGGER p2_classification_processing_no_delete BEFORE DELETE ON market_classification_processing BEGIN SELECT RAISE(ABORT,'P2 classification processing is immutable'); END;
CREATE TRIGGER p2_classification_details_no_update BEFORE UPDATE ON market_classification_result_details BEGIN SELECT RAISE(ABORT,'P2 classification details are immutable'); END;
CREATE TRIGGER p2_classification_details_no_delete BEFORE DELETE ON market_classification_result_details BEGIN SELECT RAISE(ABORT,'P2 classification details are immutable'); END;
CREATE TRIGGER p2_projection_processing_no_update BEFORE UPDATE ON market_projection_processing BEGIN SELECT RAISE(ABORT,'P2 projection processing is immutable'); END;
CREATE TRIGGER p2_projection_processing_no_delete BEFORE DELETE ON market_projection_processing BEGIN SELECT RAISE(ABORT,'P2 projection processing is immutable'); END;
CREATE TRIGGER p2_projection_decisions_no_update BEFORE UPDATE ON market_projection_decisions BEGIN SELECT RAISE(ABORT,'P2 projection decisions are immutable'); END;
CREATE TRIGGER p2_projection_decisions_no_delete BEFORE DELETE ON market_projection_decisions BEGIN SELECT RAISE(ABORT,'P2 projection decisions are immutable'); END;
CREATE TRIGGER p2_classification_claims_no_update BEFORE UPDATE ON market_classification_claims BEGIN SELECT RAISE(ABORT,'P2 classification claims are immutable'); END;
CREATE TRIGGER p2_classification_claims_no_delete BEFORE DELETE ON market_classification_claims BEGIN SELECT RAISE(ABORT,'P2 classification claims are immutable'); END;
CREATE TRIGGER p2_projection_identity_releases_no_update BEFORE UPDATE ON market_projection_identity_releases BEGIN SELECT RAISE(ABORT,'P2 projection identity releases are immutable'); END;
CREATE TRIGGER p2_projection_identity_releases_no_delete BEFORE DELETE ON market_projection_identity_releases BEGIN SELECT RAISE(ABORT,'P2 projection identity releases are immutable'); END;
CREATE TRIGGER treatment_review_sessions_no_update BEFORE UPDATE ON treatment_review_sessions BEGIN SELECT RAISE(ABORT,'treatment review sessions are immutable'); END;
CREATE TRIGGER treatment_review_sessions_no_delete BEFORE DELETE ON treatment_review_sessions BEGIN SELECT RAISE(ABORT,'treatment review sessions are immutable'); END;
CREATE TRIGGER treatment_review_terms_no_update BEFORE UPDATE ON treatment_review_input_terms BEGIN SELECT RAISE(ABORT,'treatment review terms are immutable'); END;
CREATE TRIGGER treatment_review_terms_no_delete BEFORE DELETE ON treatment_review_input_terms BEGIN SELECT RAISE(ABORT,'treatment review terms are immutable'); END;
CREATE TRIGGER treatment_review_deterministic_no_update BEFORE UPDATE ON treatment_review_deterministic_results BEGIN SELECT RAISE(ABORT,'deterministic review results are immutable'); END;
CREATE TRIGGER treatment_review_deterministic_no_delete BEFORE DELETE ON treatment_review_deterministic_results BEGIN SELECT RAISE(ABORT,'deterministic review results are immutable'); END;
CREATE TRIGGER treatment_review_preflights_no_update BEFORE UPDATE ON treatment_review_ai_preflights BEGIN SELECT RAISE(ABORT,'AI preflights are immutable'); END;
CREATE TRIGGER treatment_review_preflights_no_delete BEFORE DELETE ON treatment_review_ai_preflights BEGIN SELECT RAISE(ABORT,'AI preflights are immutable'); END;
CREATE TRIGGER treatment_review_confirmations_no_update BEFORE UPDATE ON treatment_review_ai_confirmations BEGIN SELECT RAISE(ABORT,'AI confirmations are immutable'); END;
CREATE TRIGGER treatment_review_confirmations_no_delete BEFORE DELETE ON treatment_review_ai_confirmations BEGIN SELECT RAISE(ABORT,'AI confirmations are immutable'); END;
CREATE TRIGGER treatment_review_call_claims_no_update BEFORE UPDATE ON treatment_review_ai_call_claims BEGIN SELECT RAISE(ABORT,'AI call claims are immutable'); END;
CREATE TRIGGER treatment_review_call_claims_no_delete BEFORE DELETE ON treatment_review_ai_call_claims BEGIN SELECT RAISE(ABORT,'AI call claims are immutable'); END;
CREATE TRIGGER treatment_review_responses_no_update BEFORE UPDATE ON treatment_review_ai_response_artifacts BEGIN SELECT RAISE(ABORT,'AI response artifacts are immutable'); END;
CREATE TRIGGER treatment_review_responses_no_delete BEFORE DELETE ON treatment_review_ai_response_artifacts BEGIN SELECT RAISE(ABORT,'AI response artifacts are immutable'); END;
CREATE TRIGGER treatment_review_call_outcomes_no_update BEFORE UPDATE ON treatment_review_ai_call_outcomes BEGIN SELECT RAISE(ABORT,'AI call outcomes are immutable'); END;
CREATE TRIGGER treatment_review_call_outcomes_no_delete BEFORE DELETE ON treatment_review_ai_call_outcomes BEGIN SELECT RAISE(ABORT,'AI call outcomes are immutable'); END;
CREATE TRIGGER treatment_review_results_no_update BEFORE UPDATE ON treatment_review_ai_results BEGIN SELECT RAISE(ABORT,'AI review results are immutable'); END;
CREATE TRIGGER treatment_review_results_no_delete BEFORE DELETE ON treatment_review_ai_results BEGIN SELECT RAISE(ABORT,'AI review results are immutable'); END;
CREATE TRIGGER treatment_review_decisions_no_update BEFORE UPDATE ON treatment_review_decision_events BEGIN SELECT RAISE(ABORT,'treatment decisions are append-only'); END;
CREATE TRIGGER treatment_review_decisions_no_delete BEFORE DELETE ON treatment_review_decision_events BEGIN SELECT RAISE(ABORT,'treatment decisions are append-only'); END;
CREATE TRIGGER treatment_review_actions_no_update BEFORE UPDATE ON treatment_review_actions BEGIN SELECT RAISE(ABORT,'treatment actions are immutable'); END;
CREATE TRIGGER treatment_review_actions_no_delete BEFORE DELETE ON treatment_review_actions BEGIN SELECT RAISE(ABORT,'treatment actions are immutable'); END;
