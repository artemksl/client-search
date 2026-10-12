-- TEMPORARY DATABASES ONLY.
--
-- This Market Evidence preview migration is deliberately outside the
-- auto-discovered outreach migration directory.  It may be applied only by
-- the explicit temporary-database migration API in this package.

CREATE TABLE market_measurement_specifications (
    measurement_spec_sha256 TEXT PRIMARY KEY
        CHECK (length(measurement_spec_sha256) = 64
               AND measurement_spec_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version TEXT NOT NULL CHECK (schema_version IN (
        'market_evidence.measurement_specification.v1',
        'market_evidence.measurement_specification.v2'
    )),
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
    canonical_byte_length INTEGER NOT NULL CHECK (canonical_byte_length > 0),
    source_platform TEXT NOT NULL
        CHECK (source_platform IN ('chatgpt','gemini','google_ai_overview','copilot')),
    product_surface TEXT NOT NULL CHECK (length(trim(product_surface)) > 0),
    data_mode TEXT NOT NULL CHECK (data_mode IN ('live','derived')),
    adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) > 0),
    requested_model_contract TEXT NOT NULL CHECK (length(trim(requested_model_contract)) > 0),
    observed_surface_model_epoch_contract TEXT NOT NULL
        CHECK (length(trim(observed_surface_model_epoch_contract)) > 0),
    profile_pool_id TEXT NOT NULL CHECK (length(trim(profile_pool_id)) > 0),
    profile_pool_revision TEXT NOT NULL CHECK (length(trim(profile_pool_revision)) > 0),
    collection_regime_sha256 TEXT NOT NULL
        CHECK (length(collection_regime_sha256) = 64
               AND collection_regime_sha256 NOT GLOB '*[^0-9a-f]*'),
    profile_assignment_plan_sha256 TEXT NOT NULL
        CHECK (length(profile_assignment_plan_sha256) = 64
               AND profile_assignment_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    retry_policy_version TEXT NOT NULL CHECK (length(trim(retry_policy_version)) > 0),
    maximum_window_span_policy TEXT NOT NULL
        CHECK (length(trim(maximum_window_span_policy)) > 0),
    maximum_window_span_seconds INTEGER NOT NULL CHECK (maximum_window_span_seconds > 0),
    created_at TEXT NOT NULL,
    UNIQUE (measurement_spec_sha256, purpose),
    CHECK (canonical_byte_length = length(CAST(canonical_json AS BLOB)))
);

CREATE TABLE market_demand_plans (
    plan_sha256 TEXT PRIMARY KEY
        CHECK (length(plan_sha256) = 64
               AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version TEXT NOT NULL CHECK (schema_version = 'market_evidence.demand_plan.v1'),
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
    canonical_byte_length INTEGER NOT NULL CHECK (canonical_byte_length > 0),
    reused_cell_count INTEGER NOT NULL CHECK (reused_cell_count >= 0),
    collection_cell_count INTEGER NOT NULL CHECK (collection_cell_count >= 0),
    semantic_prompt_count INTEGER NOT NULL CHECK (semantic_prompt_count >= 0),
    platform_rendering_count INTEGER NOT NULL CHECK (platform_rendering_count >= 0),
    logical_observation_count INTEGER NOT NULL CHECK (logical_observation_count >= 0),
    live_collector_action_count INTEGER NOT NULL CHECK (live_collector_action_count >= 0),
    estimated_duration_seconds INTEGER NOT NULL CHECK (estimated_duration_seconds >= 0),
    currency_code TEXT NOT NULL
        CHECK (length(currency_code) = 3 AND currency_code = upper(currency_code)),
    estimated_cost_minor_units INTEGER NOT NULL CHECK (estimated_cost_minor_units >= 0),
    cost_model_version TEXT NOT NULL CHECK (length(trim(cost_model_version)) > 0),
    cost_model_sha256 TEXT NOT NULL
        CHECK (length(cost_model_sha256) = 64
               AND cost_model_sha256 NOT GLOB '*[^0-9a-f]*'),
    prompt_pack_version TEXT NOT NULL CHECK (length(trim(prompt_pack_version)) > 0),
    prompt_pack_content_sha256 TEXT NOT NULL
        CHECK (length(prompt_pack_content_sha256) = 64
               AND prompt_pack_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    prompt_catalogue_version TEXT NOT NULL CHECK (length(trim(prompt_catalogue_version)) > 0),
    prompt_catalogue_content_sha256 TEXT NOT NULL
        CHECK (length(prompt_catalogue_content_sha256) = 64
               AND prompt_catalogue_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE (plan_sha256, purpose),
    CHECK (canonical_byte_length = length(CAST(canonical_json AS BLOB)))
);

-- This immutable junction is a rigorous part of deterministic plan
-- persistence.  It prevents a confirmation from queueing a specification
-- that was not in the exact canonical plan.
CREATE TABLE market_demand_plan_specifications (
    plan_sha256 TEXT NOT NULL,
    measurement_spec_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    evidence_status TEXT NOT NULL
        CHECK (evidence_status IN ('reused','missing','stale','incompatible')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_sha256, measurement_spec_sha256),
    FOREIGN KEY (plan_sha256, purpose)
        REFERENCES market_demand_plans(plan_sha256, purpose) ON DELETE RESTRICT,
    FOREIGN KEY (measurement_spec_sha256, purpose)
        REFERENCES market_measurement_specifications(measurement_spec_sha256, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE market_demand_consumers (
    consumer_id TEXT PRIMARY KEY,
    consumer_identity_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(consumer_identity_sha256) = 64
               AND consumer_identity_sha256 NOT GLOB '*[^0-9a-f]*'),
    plan_sha256 TEXT NOT NULL,
    campaign_revision_id TEXT NOT NULL
        REFERENCES campaign_revisions(id) ON DELETE RESTRICT,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    attached_at TEXT NOT NULL,
    UNIQUE (plan_sha256, campaign_revision_id),
    FOREIGN KEY (plan_sha256, purpose)
        REFERENCES market_demand_plans(plan_sha256, purpose) ON DELETE RESTRICT,
    CHECK (consumer_id = 'market-demand-consumer-' || consumer_identity_sha256)
);

CREATE TABLE market_measurement_windows (
    window_id TEXT PRIMARY KEY CHECK (length(trim(window_id)) > 0),
    measurement_spec_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    opened_at TEXT NOT NULL CHECK (julianday(opened_at) IS NOT NULL),
    profile_assignment_plan_sha256 TEXT NOT NULL
        CHECK (length(profile_assignment_plan_sha256) = 64
               AND profile_assignment_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    run_1_profile_id TEXT NOT NULL CHECK (length(trim(run_1_profile_id)) > 0),
    run_1_profile_revision TEXT NOT NULL CHECK (length(trim(run_1_profile_revision)) > 0),
    run_2_profile_id TEXT NOT NULL CHECK (length(trim(run_2_profile_id)) > 0),
    run_2_profile_revision TEXT NOT NULL CHECK (length(trim(run_2_profile_revision)) > 0),
    run_3_profile_id TEXT NOT NULL CHECK (length(trim(run_3_profile_id)) > 0),
    run_3_profile_revision TEXT NOT NULL CHECK (length(trim(run_3_profile_revision)) > 0),
    created_at TEXT NOT NULL,
    UNIQUE (window_id, measurement_spec_sha256, purpose),
    UNIQUE (window_id, purpose),
    FOREIGN KEY (measurement_spec_sha256, purpose)
        REFERENCES market_measurement_specifications(measurement_spec_sha256, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE market_logical_observations (
    logical_observation_id TEXT PRIMARY KEY,
    identity_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(identity_sha256) = 64
               AND identity_sha256 NOT GLOB '*[^0-9a-f]*'),
    measurement_spec_sha256 TEXT NOT NULL,
    window_id TEXT NOT NULL,
    run_ordinal INTEGER NOT NULL CHECK (run_ordinal IN (1,2,3)),
    planned_profile_id TEXT NOT NULL CHECK (length(trim(planned_profile_id)) > 0),
    planned_profile_revision TEXT NOT NULL
        CHECK (length(trim(planned_profile_revision)) > 0),
    source_platform TEXT NOT NULL
        CHECK (source_platform IN ('chatgpt','gemini','google_ai_overview','copilot')),
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    created_at TEXT NOT NULL,
    UNIQUE (window_id, run_ordinal),
    UNIQUE (logical_observation_id, measurement_spec_sha256, window_id,
            run_ordinal, planned_profile_id, planned_profile_revision, purpose),
    UNIQUE (logical_observation_id, purpose),
    FOREIGN KEY (window_id, measurement_spec_sha256, purpose)
        REFERENCES market_measurement_windows(window_id, measurement_spec_sha256, purpose)
        ON DELETE RESTRICT,
    CHECK (logical_observation_id = 'market-logical-observation-' || identity_sha256)
);

CREATE TABLE market_observation_attempts (
    attempt_id TEXT PRIMARY KEY CHECK (length(trim(attempt_id)) > 0),
    logical_observation_id TEXT NOT NULL,
    measurement_spec_sha256 TEXT NOT NULL,
    window_id TEXT NOT NULL,
    run_ordinal INTEGER NOT NULL CHECK (run_ordinal IN (1,2,3)),
    planned_profile_id TEXT NOT NULL,
    planned_profile_revision TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal > 0),
    job_id TEXT,
    lease_generation INTEGER CHECK (lease_generation IS NULL OR lease_generation > 0),
    runtime_source_platform TEXT
        CHECK (runtime_source_platform IN ('chatgpt','gemini','google_ai_overview','copilot')),
    runtime_product_surface TEXT CHECK (length(trim(runtime_product_surface)) > 0),
    runtime_data_mode TEXT CHECK (runtime_data_mode IN ('live','derived')),
    runtime_adapter_version TEXT CHECK (length(trim(runtime_adapter_version)) > 0),
    runtime_requested_model_contract TEXT
        CHECK (length(trim(runtime_requested_model_contract)) > 0),
    runtime_observed_surface_model_epoch_contract TEXT
        CHECK (length(trim(runtime_observed_surface_model_epoch_contract)) > 0),
    runtime_profile_id TEXT CHECK (length(trim(runtime_profile_id)) > 0),
    runtime_profile_revision TEXT CHECK (length(trim(runtime_profile_revision)) > 0),
    runtime_collection_regime_sha256 TEXT
        CHECK (runtime_collection_regime_sha256 IS NULL OR
               (length(runtime_collection_regime_sha256) = 64
                AND runtime_collection_regime_sha256 NOT GLOB '*[^0-9a-f]*')),
    runtime_context_sha256 TEXT
        CHECK (runtime_context_sha256 IS NULL OR
               (length(runtime_context_sha256) = 64
                AND runtime_context_sha256 NOT GLOB '*[^0-9a-f]*')),
    compatibility_state TEXT NOT NULL
        CHECK (compatibility_state IN ('compatible','incompatible','not_observed')),
    compatibility_reason TEXT NOT NULL
        CHECK (compatibility_reason IN (
            'exact_match','runtime_context_mismatch','runtime_not_observed'
        )),
    send_phase TEXT NOT NULL CHECK (send_phase IN (
        'not_started','attempt_reserved','send_started','send_acknowledged','send_unknown'
    )),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'reserved','succeeded','retryable_failure','terminal_failure',
        'incompatible','send_unknown'
    )),
    started_at TEXT NOT NULL CHECK (julianday(started_at) IS NOT NULL),
    completed_at TEXT CHECK (completed_at IS NULL OR julianday(completed_at) IS NOT NULL),
    error_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (logical_observation_id, attempt_ordinal),
    UNIQUE (attempt_id, logical_observation_id, purpose),
    UNIQUE (attempt_id, purpose),
    FOREIGN KEY (
        logical_observation_id, measurement_spec_sha256, window_id, run_ordinal,
        planned_profile_id, planned_profile_revision, purpose
    ) REFERENCES market_logical_observations(
        logical_observation_id, measurement_spec_sha256, window_id, run_ordinal,
        planned_profile_id, planned_profile_revision, purpose
    ) ON DELETE RESTRICT,
    CHECK ((compatibility_state = 'compatible' AND compatibility_reason = 'exact_match')
        OR (compatibility_state = 'incompatible'
            AND compatibility_reason = 'runtime_context_mismatch')
        OR (compatibility_state = 'not_observed'
            AND compatibility_reason = 'runtime_not_observed')),
    CHECK (
        (compatibility_state = 'not_observed'
          AND runtime_source_platform IS NULL AND runtime_product_surface IS NULL
          AND runtime_data_mode IS NULL AND runtime_adapter_version IS NULL
          AND runtime_requested_model_contract IS NULL
          AND runtime_observed_surface_model_epoch_contract IS NULL
          AND runtime_profile_id IS NULL AND runtime_profile_revision IS NULL
          AND runtime_collection_regime_sha256 IS NULL
          AND runtime_context_sha256 IS NULL)
        OR
        (compatibility_state <> 'not_observed'
          AND runtime_source_platform IS NOT NULL AND runtime_product_surface IS NOT NULL
          AND runtime_data_mode IS NOT NULL AND runtime_adapter_version IS NOT NULL
          AND runtime_requested_model_contract IS NOT NULL
          AND runtime_observed_surface_model_epoch_contract IS NOT NULL
          AND runtime_profile_id IS NOT NULL AND runtime_profile_revision IS NOT NULL
          AND runtime_collection_regime_sha256 IS NOT NULL
          AND runtime_context_sha256 IS NOT NULL)
    ),
    CHECK (send_phase <> 'send_unknown' OR outcome = 'send_unknown')
    ,CHECK (outcome = 'reserved'
            OR ((compatibility_state = 'incompatible') = (outcome = 'incompatible')))
    ,CHECK ((job_id IS NULL AND lease_generation IS NULL)
            OR (job_id IS NOT NULL AND lease_generation IS NOT NULL))
    ,CHECK ((outcome = 'reserved' AND completed_at IS NULL)
            OR (outcome <> 'reserved' AND completed_at IS NOT NULL))
    ,CHECK (completed_at IS NULL OR julianday(completed_at) >= julianday(started_at))
);

CREATE TABLE market_observation_acceptances (
    acceptance_id TEXT PRIMARY KEY CHECK (length(trim(acceptance_id)) > 0),
    logical_observation_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL UNIQUE,
    measurement_spec_sha256 TEXT NOT NULL,
    window_id TEXT NOT NULL,
    run_ordinal INTEGER NOT NULL CHECK (run_ordinal IN (1,2,3)),
    planned_profile_id TEXT NOT NULL,
    planned_profile_revision TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    accepted_at TEXT NOT NULL CHECK (julianday(accepted_at) IS NOT NULL),
    acceptance_manifest_sha256 TEXT NOT NULL
        CHECK (length(acceptance_manifest_sha256) = 64
               AND acceptance_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE (acceptance_id, purpose),
    FOREIGN KEY (attempt_id, logical_observation_id, purpose)
        REFERENCES market_observation_attempts(attempt_id, logical_observation_id, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        logical_observation_id, measurement_spec_sha256, window_id, run_ordinal,
        planned_profile_id, planned_profile_revision, purpose
    ) REFERENCES market_logical_observations(
        logical_observation_id, measurement_spec_sha256, window_id, run_ordinal,
        planned_profile_id, planned_profile_revision, purpose
    ) ON DELETE RESTRICT
);

CREATE TABLE market_window_finalizations (
    finalization_id TEXT PRIMARY KEY CHECK (length(trim(finalization_id)) > 0),
    window_id TEXT NOT NULL UNIQUE,
    measurement_spec_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    finalized_at TEXT NOT NULL CHECK (julianday(finalized_at) IS NOT NULL),
    accepted_run_manifest_sha256 TEXT NOT NULL
        CHECK (length(accepted_run_manifest_sha256) = 64
               AND accepted_run_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    oldest_accepted_at TEXT NOT NULL CHECK (julianday(oldest_accepted_at) IS NOT NULL),
    newest_accepted_at TEXT NOT NULL CHECK (julianday(newest_accepted_at) IS NOT NULL),
    created_at TEXT NOT NULL,
    UNIQUE (finalization_id, purpose),
    FOREIGN KEY (window_id, measurement_spec_sha256, purpose)
        REFERENCES market_measurement_windows(window_id, measurement_spec_sha256, purpose)
        ON DELETE RESTRICT,
    CHECK (julianday(oldest_accepted_at) <= julianday(newest_accepted_at))
);

CREATE TABLE market_artifacts (
    artifact_sha256 TEXT NOT NULL
        CHECK (length(artifact_sha256) = 64
               AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
    byte_length INTEGER NOT NULL CHECK (byte_length > 0),
    media_type TEXT NOT NULL CHECK (length(trim(media_type)) > 0),
    purpose TEXT NOT NULL CHECK (purpose IN (
        'market_discovery','free_snapshot_confirmation','outreach_gap_confirmation'
    )),
    storage_relative_path TEXT NOT NULL,
    creator TEXT NOT NULL CHECK (length(trim(creator)) > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_sha256, purpose),
    CHECK (storage_relative_path = substr(artifact_sha256,1,2) || '/' || artifact_sha256)
);

CREATE TABLE market_artifact_bindings (
    binding_id TEXT PRIMARY KEY CHECK (length(trim(binding_id)) > 0),
    artifact_sha256 TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    logical_observation_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    artifact_role TEXT NOT NULL CHECK (artifact_role IN (
        'raw_answer','occurrence_extraction','classification','projection'
    )),
    binding_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(binding_sha256) = 64
               AND binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, artifact_role),
    UNIQUE (binding_id, purpose),
    FOREIGN KEY (artifact_sha256, purpose)
        REFERENCES market_artifacts(artifact_sha256, purpose) ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id, logical_observation_id, purpose)
        REFERENCES market_observation_attempts(attempt_id, logical_observation_id, purpose)
        ON DELETE RESTRICT
);

-- Quarantine is an append-only event.  Artifact metadata is never mutated,
-- and any event makes the bytes ineligible until a new digest is collected.
CREATE TABLE market_artifact_quarantines (
    quarantine_id TEXT PRIMARY KEY CHECK (length(trim(quarantine_id)) > 0),
    artifact_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN (
        'market_discovery','free_snapshot_confirmation','outreach_gap_confirmation'
    )),
    reason TEXT NOT NULL CHECK (reason IN (
        'missing','corrupt','symlink','not_regular_file','path_substitution',
        'unreadable','truncated','byte_length_mismatch','digest_mismatch',
        'metadata_mismatch','purpose_mismatch','media_type_mismatch'
    )),
    details_sha256 TEXT NOT NULL
        CHECK (length(details_sha256) = 64
               AND details_sha256 NOT GLOB '*[^0-9a-f]*'),
    quarantined_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (artifact_sha256, purpose)
        REFERENCES market_artifacts(artifact_sha256, purpose) ON DELETE RESTRICT
);

CREATE TABLE market_collection_previews (
    preview_id TEXT PRIMARY KEY CHECK (length(trim(preview_id)) > 0),
    preview_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(preview_sha256) = 64
               AND preview_sha256 NOT GLOB '*[^0-9a-f]*'),
    plan_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    campaign_revision_id TEXT NOT NULL
        REFERENCES campaign_revisions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    expires_at TEXT NOT NULL CHECK (julianday(expires_at) IS NOT NULL),
    reused_cell_count INTEGER NOT NULL CHECK (reused_cell_count >= 0),
    collection_cell_count INTEGER NOT NULL CHECK (collection_cell_count >= 0),
    semantic_prompt_count INTEGER NOT NULL CHECK (semantic_prompt_count >= 0),
    platform_rendering_count INTEGER NOT NULL CHECK (platform_rendering_count >= 0),
    logical_observation_count INTEGER NOT NULL CHECK (logical_observation_count >= 0),
    live_collector_action_count INTEGER NOT NULL CHECK (live_collector_action_count >= 0),
    estimated_duration_seconds INTEGER NOT NULL CHECK (estimated_duration_seconds >= 0),
    currency_code TEXT NOT NULL
        CHECK (length(currency_code) = 3 AND currency_code = upper(currency_code)),
    estimated_cost_minor_units INTEGER NOT NULL CHECK (estimated_cost_minor_units >= 0),
    blockers_json TEXT NOT NULL CHECK (json_valid(blockers_json)),
    blockers_sha256 TEXT NOT NULL
        CHECK (length(blockers_sha256) = 64
               AND blockers_sha256 NOT GLOB '*[^0-9a-f]*'),
    query_geography_versions_sha256 TEXT NOT NULL
        CHECK (length(query_geography_versions_sha256) = 64
               AND query_geography_versions_sha256 NOT GLOB '*[^0-9a-f]*'),
    prompt_pack_version TEXT NOT NULL,
    prompt_pack_content_sha256 TEXT NOT NULL,
    prompt_catalogue_version TEXT NOT NULL,
    prompt_catalogue_content_sha256 TEXT NOT NULL,
    model_surface_epochs_sha256 TEXT NOT NULL,
    profile_assignments_sha256 TEXT NOT NULL,
    freshness_policy_version TEXT NOT NULL,
    freshness_policy_sha256 TEXT NOT NULL,
    cost_model_version TEXT NOT NULL,
    rate_card_sha256 TEXT NOT NULL,
    canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
    canonical_byte_length INTEGER NOT NULL CHECK (canonical_byte_length > 0),
    UNIQUE (preview_id, plan_sha256, purpose),
    FOREIGN KEY (plan_sha256, purpose)
        REFERENCES market_demand_plans(plan_sha256, purpose) ON DELETE RESTRICT,
    CHECK (julianday(expires_at) > julianday(created_at)),
    CHECK (canonical_byte_length = length(CAST(canonical_json AS BLOB))),
    CHECK (length(prompt_pack_content_sha256) = 64
           AND prompt_pack_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(prompt_catalogue_content_sha256) = 64
           AND prompt_catalogue_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(model_surface_epochs_sha256) = 64
           AND model_surface_epochs_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(profile_assignments_sha256) = 64
           AND profile_assignments_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(freshness_policy_sha256) = 64
           AND freshness_policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(rate_card_sha256) = 64
           AND rate_card_sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE market_collection_confirmations (
    confirmation_id TEXT PRIMARY KEY CHECK (length(trim(confirmation_id)) > 0),
    confirmation_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(confirmation_sha256) = 64
               AND confirmation_sha256 NOT GLOB '*[^0-9a-f]*'),
    preview_id TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    acknowledged_observation_count INTEGER NOT NULL
        CHECK (acknowledged_observation_count >= 0),
    maximum_cost_minor_units INTEGER NOT NULL CHECK (maximum_cost_minor_units >= 0),
    currency_code TEXT NOT NULL
        CHECK (length(currency_code) = 3 AND currency_code = upper(currency_code)),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
    canonical_byte_length INTEGER NOT NULL CHECK (canonical_byte_length > 0),
    confirmed_at TEXT NOT NULL CHECK (julianday(confirmed_at) IS NOT NULL),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    UNIQUE (purpose, idempotency_key),
    UNIQUE (confirmation_id, plan_sha256, purpose),
    FOREIGN KEY (preview_id, plan_sha256, purpose)
        REFERENCES market_collection_previews(preview_id, plan_sha256, purpose)
        ON DELETE RESTRICT,
    CHECK (canonical_byte_length = length(CAST(canonical_json AS BLOB)))
);

CREATE TABLE market_collection_jobs (
    job_id TEXT PRIMARY KEY CHECK (length(trim(job_id)) > 0),
    logical_observation_id TEXT NOT NULL,
    confirmation_id TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    state TEXT NOT NULL CHECK (state IN (
        'queued','leased','retry_wait','succeeded','terminal_failed','cancelled','send_unknown'
    )),
    send_phase TEXT NOT NULL CHECK (send_phase IN (
        'not_started','attempt_reserved','send_started','send_acknowledged','send_unknown'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    maximum_attempts INTEGER NOT NULL CHECK (maximum_attempts > 0),
    backoff_policy_version TEXT NOT NULL CHECK (length(trim(backoff_policy_version)) > 0),
    next_attempt_at TEXT CHECK (
        next_attempt_at IS NULL OR julianday(next_attempt_at) IS NOT NULL
    ),
    worker_id TEXT,
    lease_token TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_at TEXT CHECK (
        lease_expires_at IS NULL OR julianday(lease_expires_at) IS NOT NULL
    ),
    reserved_attempt_id TEXT,
    last_attempt_id TEXT,
    last_error_code TEXT,
    state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    updated_at TEXT NOT NULL CHECK (julianday(updated_at) IS NOT NULL),
    FOREIGN KEY (logical_observation_id, purpose)
        REFERENCES market_logical_observations(logical_observation_id, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (confirmation_id, plan_sha256, purpose)
        REFERENCES market_collection_confirmations(confirmation_id, plan_sha256, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (last_attempt_id, logical_observation_id, purpose)
        REFERENCES market_observation_attempts(attempt_id, logical_observation_id, purpose)
        ON DELETE RESTRICT,
    CHECK (attempt_count <= maximum_attempts),
    CHECK (length(created_at)=27 AND substr(created_at,27,1)='Z'),
    CHECK (length(updated_at)=27 AND substr(updated_at,27,1)='Z'),
    CHECK (next_attempt_at IS NULL OR
           (length(next_attempt_at)=27 AND substr(next_attempt_at,27,1)='Z')),
    CHECK (lease_expires_at IS NULL OR
           (length(lease_expires_at)=27 AND substr(lease_expires_at,27,1)='Z')),
    CHECK (
        (state = 'leased' AND worker_id IS NOT NULL AND lease_token IS NOT NULL
          AND lease_expires_at IS NOT NULL)
        OR
        (state <> 'leased' AND worker_id IS NULL AND lease_token IS NULL
          AND lease_expires_at IS NULL)
    ),
    CHECK ((state = 'retry_wait' AND next_attempt_at IS NOT NULL)
           OR (state <> 'retry_wait' AND next_attempt_at IS NULL)),
    CHECK (
        state = 'leased'
        OR (state IN ('queued','retry_wait','cancelled')
            AND send_phase = 'not_started')
        OR (state = 'succeeded' AND send_phase = 'send_acknowledged')
        OR (state = 'send_unknown' AND send_phase = 'send_unknown')
        OR (state = 'terminal_failed'
            AND send_phase IN ('not_started','send_acknowledged','send_unknown'))
    ),
    CHECK ((send_phase = 'not_started' AND reserved_attempt_id IS NULL)
           OR (send_phase <> 'not_started' AND reserved_attempt_id IS NOT NULL))
);

CREATE UNIQUE INDEX market_one_active_job_per_observation
    ON market_collection_jobs(logical_observation_id)
    WHERE state IN ('queued','leased','retry_wait','send_unknown');
CREATE INDEX market_jobs_claimable
    ON market_collection_jobs(state,next_attempt_at,created_at);

CREATE TABLE market_send_unknown_resolutions (
    resolution_id TEXT PRIMARY KEY CHECK (length(trim(resolution_id)) > 0),
    job_id TEXT NOT NULL UNIQUE
        REFERENCES market_collection_jobs(job_id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    from_state TEXT NOT NULL CHECK (from_state = 'send_unknown'),
    resolution TEXT NOT NULL CHECK (resolution = 'terminal_failed'),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    resolved_at TEXT NOT NULL CHECK (
        julianday(resolved_at) IS NOT NULL
        AND length(resolved_at)=27 AND substr(resolved_at,27,1)='Z'
    ),
    created_at TEXT NOT NULL CHECK (julianday(created_at) IS NOT NULL),
    FOREIGN KEY (attempt_id, purpose)
        REFERENCES market_observation_attempts(attempt_id, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE market_confirmation_jobs (
    confirmation_id TEXT NOT NULL
        REFERENCES market_collection_confirmations(confirmation_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL
        REFERENCES market_collection_jobs(job_id) ON DELETE RESTRICT,
    logical_observation_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    attached_at TEXT NOT NULL CHECK (julianday(attached_at) IS NOT NULL),
    PRIMARY KEY (confirmation_id, job_id),
    UNIQUE (confirmation_id, logical_observation_id),
    FOREIGN KEY (logical_observation_id, purpose)
        REFERENCES market_logical_observations(logical_observation_id, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE market_occurrence_runs (
    occurrence_run_id TEXT PRIMARY KEY CHECK (length(trim(occurrence_run_id)) > 0),
    window_id TEXT NOT NULL,
    measurement_spec_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    schema_version TEXT NOT NULL,
    extractor_contract_version TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL
        CHECK (length(input_manifest_sha256) = 64
               AND input_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    output_manifest_sha256 TEXT NOT NULL
        CHECK (length(output_manifest_sha256) = 64
               AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE (occurrence_run_id, purpose),
    FOREIGN KEY (window_id, measurement_spec_sha256, purpose)
        REFERENCES market_measurement_windows(window_id, measurement_spec_sha256, purpose)
        ON DELETE RESTRICT
);

CREATE TABLE market_literal_occurrences (
    occurrence_id TEXT PRIMARY KEY CHECK (length(trim(occurrence_id)) > 0),
    occurrence_run_id TEXT NOT NULL,
    logical_observation_id TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    exact_text TEXT NOT NULL CHECK (length(exact_text) > 0),
    byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
    byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
    occurrence_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(occurrence_sha256) = 64
               AND occurrence_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    UNIQUE (occurrence_id, purpose),
    FOREIGN KEY (occurrence_run_id, purpose)
        REFERENCES market_occurrence_runs(occurrence_run_id, purpose) ON DELETE RESTRICT,
    FOREIGN KEY (logical_observation_id, purpose)
        REFERENCES market_logical_observations(logical_observation_id, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (artifact_sha256, purpose)
        REFERENCES market_artifacts(artifact_sha256, purpose) ON DELETE RESTRICT
);

CREATE TABLE market_classification_runs (
    classification_run_id TEXT PRIMARY KEY CHECK (length(trim(classification_run_id)) > 0),
    occurrence_run_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    schema_version TEXT NOT NULL,
    classifier_contract_version TEXT NOT NULL,
    classifier_model_contract TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL,
    output_manifest_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (classification_run_id, purpose),
    FOREIGN KEY (occurrence_run_id, purpose)
        REFERENCES market_occurrence_runs(occurrence_run_id, purpose) ON DELETE RESTRICT,
    CHECK (length(input_manifest_sha256) = 64
           AND input_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(output_manifest_sha256) = 64
           AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE market_classification_results (
    classification_result_id TEXT PRIMARY KEY,
    classification_run_id TEXT NOT NULL,
    occurrence_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    classification TEXT NOT NULL CHECK (classification IN (
        'clinic_name','non_clinic','ambiguous','unclassified'
    )),
    result_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (classification_result_id, purpose),
    FOREIGN KEY (classification_run_id, purpose)
        REFERENCES market_classification_runs(classification_run_id, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (occurrence_id, purpose)
        REFERENCES market_literal_occurrences(occurrence_id, purpose) ON DELETE RESTRICT,
    CHECK (length(result_sha256) = 64
           AND result_sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE market_projection_runs (
    projection_run_id TEXT PRIMARY KEY CHECK (length(trim(projection_run_id)) > 0),
    classification_run_id TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    schema_version TEXT NOT NULL,
    projection_contract_version TEXT NOT NULL,
    target_catalogue_release_id TEXT NOT NULL,
    target_catalogue_payload_sha256 TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL,
    output_manifest_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (projection_run_id, purpose),
    FOREIGN KEY (classification_run_id, purpose)
        REFERENCES market_classification_runs(classification_run_id, purpose)
        ON DELETE RESTRICT,
    FOREIGN KEY (plan_sha256, purpose)
        REFERENCES market_demand_plans(plan_sha256, purpose) ON DELETE RESTRICT,
    CHECK (length(target_catalogue_payload_sha256) = 64
           AND target_catalogue_payload_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(input_manifest_sha256) = 64
           AND input_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(output_manifest_sha256) = 64
           AND output_manifest_sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE market_projection_matches (
    projection_match_id TEXT PRIMARY KEY CHECK (length(trim(projection_match_id)) > 0),
    projection_run_id TEXT NOT NULL,
    occurrence_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose = 'market_discovery'),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('branch','entity')),
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
    validation_state TEXT NOT NULL CHECK (validation_state IN (
        'validated','rejected','ambiguous'
    )),
    match_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (projection_match_id, purpose),
    FOREIGN KEY (projection_run_id, purpose)
        REFERENCES market_projection_runs(projection_run_id, purpose) ON DELETE RESTRICT,
    FOREIGN KEY (occurrence_id, purpose)
        REFERENCES market_literal_occurrences(occurrence_id, purpose) ON DELETE RESTRICT,
    CHECK (length(match_sha256) = 64
           AND match_sha256 NOT GLOB '*[^0-9a-f]*')
);

-- Current V2 is the only contract that may create new demand.
CREATE TRIGGER market_plan_specification_requires_current_contract
BEFORE INSERT ON market_demand_plan_specifications
WHEN NOT EXISTS (
    SELECT 1 FROM market_measurement_specifications specification
    WHERE specification.measurement_spec_sha256 = NEW.measurement_spec_sha256
      AND specification.purpose = NEW.purpose
      AND specification.schema_version = 'market_evidence.measurement_specification.v2'
)
BEGIN SELECT RAISE(ABORT, 'new demand requires the current V2 measurement contract'); END;

CREATE TRIGGER market_consumer_requires_non_completed_campaign
BEFORE INSERT ON market_demand_consumers
WHEN NOT EXISTS (
    SELECT 1 FROM campaign_revisions revision
    WHERE revision.id=NEW.campaign_revision_id
      AND revision.state IN ('planned','active')
)
BEGIN SELECT RAISE(ABORT, 'market demand cannot attach a completed campaign'); END;

CREATE TRIGGER market_window_requires_exact_profile_plan
BEFORE INSERT ON market_measurement_windows
WHEN NOT EXISTS (
    SELECT 1 FROM market_measurement_specifications specification
    WHERE specification.measurement_spec_sha256 = NEW.measurement_spec_sha256
      AND specification.purpose = NEW.purpose
      AND specification.schema_version = 'market_evidence.measurement_specification.v2'
      AND specification.profile_assignment_plan_sha256 = NEW.profile_assignment_plan_sha256
)
BEGIN SELECT RAISE(ABORT, 'window profile plan must match a current exact specification'); END;

CREATE TRIGGER market_logical_observation_requires_frozen_run
BEFORE INSERT ON market_logical_observations
WHEN NOT EXISTS (
    SELECT 1
    FROM market_measurement_windows window
    JOIN market_measurement_specifications specification
      ON specification.measurement_spec_sha256 = window.measurement_spec_sha256
     AND specification.purpose = window.purpose
    WHERE window.window_id = NEW.window_id
      AND window.measurement_spec_sha256 = NEW.measurement_spec_sha256
      AND window.purpose = NEW.purpose
      AND specification.source_platform = NEW.source_platform
      AND (
        (NEW.run_ordinal = 1 AND NEW.planned_profile_id = window.run_1_profile_id
          AND NEW.planned_profile_revision = window.run_1_profile_revision)
        OR
        (NEW.run_ordinal = 2 AND NEW.planned_profile_id = window.run_2_profile_id
          AND NEW.planned_profile_revision = window.run_2_profile_revision)
        OR
        (NEW.run_ordinal = 3 AND NEW.planned_profile_id = window.run_3_profile_id
          AND NEW.planned_profile_revision = window.run_3_profile_revision)
      )
)
BEGIN SELECT RAISE(ABORT, 'logical observation does not match its frozen run'); END;

CREATE TRIGGER market_attempt_requires_runtime_compatibility_truth
BEFORE INSERT ON market_observation_attempts
BEGIN
    SELECT CASE WHEN NEW.compatibility_state <> 'not_observed' AND EXISTS (
        SELECT 1
        FROM market_measurement_specifications specification
        WHERE specification.measurement_spec_sha256 = NEW.measurement_spec_sha256
          AND specification.purpose = NEW.purpose
          AND specification.source_platform = NEW.runtime_source_platform
          AND specification.product_surface = NEW.runtime_product_surface
          AND specification.data_mode = NEW.runtime_data_mode
          AND specification.adapter_version = NEW.runtime_adapter_version
          AND specification.requested_model_contract = NEW.runtime_requested_model_contract
          AND specification.observed_surface_model_epoch_contract =
                NEW.runtime_observed_surface_model_epoch_contract
          AND specification.collection_regime_sha256 =
                NEW.runtime_collection_regime_sha256
          AND NEW.runtime_profile_id = NEW.planned_profile_id
          AND NEW.runtime_profile_revision = NEW.planned_profile_revision
    ) <> (NEW.compatibility_state = 'compatible')
    THEN RAISE(ABORT, 'attempt compatibility does not match exact runtime context') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM market_window_finalizations finalization
        WHERE finalization.window_id = NEW.window_id
    ) THEN RAISE(ABORT, 'finalized windows cannot gain attempts') END;
END;

CREATE TRIGGER market_artifact_binding_requires_open_window
BEFORE INSERT ON market_artifact_bindings
WHEN EXISTS (
    SELECT 1
    FROM market_observation_attempts attempt
    JOIN market_window_finalizations finalization
      ON finalization.window_id = attempt.window_id
    WHERE attempt.attempt_id = NEW.attempt_id
)
BEGIN SELECT RAISE(ABORT, 'finalized windows cannot gain artifact bindings'); END;

CREATE TRIGGER market_acceptance_requires_compatible_raw_attempt
BEFORE INSERT ON market_observation_acceptances
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM market_observation_attempts attempt
        JOIN market_artifact_bindings binding
          ON binding.attempt_id = attempt.attempt_id
         AND binding.logical_observation_id = attempt.logical_observation_id
         AND binding.purpose = attempt.purpose
         AND binding.artifact_role = 'raw_answer'
        JOIN market_artifacts artifact
          ON artifact.artifact_sha256 = binding.artifact_sha256
         AND artifact.purpose = binding.purpose
        WHERE attempt.attempt_id = NEW.attempt_id
          AND attempt.logical_observation_id = NEW.logical_observation_id
          AND attempt.purpose = NEW.purpose
          AND attempt.compatibility_state = 'compatible'
          AND attempt.outcome = 'succeeded'
          AND attempt.send_phase = 'send_acknowledged'
          AND NOT EXISTS (
              SELECT 1 FROM market_artifact_quarantines quarantine
              WHERE quarantine.artifact_sha256 = artifact.artifact_sha256
                AND quarantine.purpose = artifact.purpose
          )
    ) THEN RAISE(ABORT, 'acceptance requires one compatible raw artifact') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM market_window_finalizations finalization
        WHERE finalization.window_id = NEW.window_id
    ) THEN RAISE(ABORT, 'finalized windows cannot gain acceptances') END;
END;

CREATE TRIGGER market_finalization_requires_exact_complete_window
BEFORE INSERT ON market_window_finalizations
BEGIN
    SELECT CASE WHEN (
        SELECT count(*) FROM market_logical_observations observation
        WHERE observation.window_id = NEW.window_id
          AND observation.measurement_spec_sha256 = NEW.measurement_spec_sha256
          AND observation.purpose = NEW.purpose
    ) <> 3 THEN RAISE(ABORT, 'window finalization requires exactly three observations') END;
    SELECT CASE WHEN (
        SELECT count(*)
        FROM market_logical_observations observation
        JOIN market_observation_acceptances acceptance
          ON acceptance.logical_observation_id = observation.logical_observation_id
         AND acceptance.measurement_spec_sha256 = observation.measurement_spec_sha256
         AND acceptance.window_id = observation.window_id
         AND acceptance.run_ordinal = observation.run_ordinal
         AND acceptance.planned_profile_id = observation.planned_profile_id
         AND acceptance.planned_profile_revision = observation.planned_profile_revision
         AND acceptance.purpose = observation.purpose
        WHERE observation.window_id = NEW.window_id
          AND observation.purpose = NEW.purpose
    ) <> 3 THEN RAISE(ABORT, 'window finalization requires accepted runs 1, 2, and 3') END;
    SELECT CASE WHEN NEW.oldest_accepted_at <> (
        SELECT acceptance.accepted_at
        FROM market_observation_acceptances acceptance
        WHERE acceptance.window_id = NEW.window_id
          AND acceptance.purpose = NEW.purpose
        ORDER BY julianday(acceptance.accepted_at),acceptance.acceptance_id
        LIMIT 1
    ) OR NEW.newest_accepted_at <> (
        SELECT acceptance.accepted_at
        FROM market_observation_acceptances acceptance
        WHERE acceptance.window_id = NEW.window_id
          AND acceptance.purpose = NEW.purpose
        ORDER BY julianday(acceptance.accepted_at) DESC,acceptance.acceptance_id DESC
        LIMIT 1
    ) THEN RAISE(ABORT, 'finalization timestamps must equal accepted evidence') END;
    SELECT CASE WHEN (
        SELECT (julianday(NEW.newest_accepted_at) - julianday(NEW.oldest_accepted_at))
               * 86400.0
        FROM market_measurement_specifications specification
        WHERE specification.measurement_spec_sha256 = NEW.measurement_spec_sha256
          AND specification.purpose = NEW.purpose
    ) > (
        SELECT maximum_window_span_seconds + 0.001
        FROM market_measurement_specifications specification
        WHERE specification.measurement_spec_sha256 = NEW.measurement_spec_sha256
          AND specification.purpose = NEW.purpose
    ) THEN RAISE(ABORT, 'accepted evidence exceeds the coherent window span') END;
    SELECT CASE WHEN julianday(NEW.finalized_at) < julianday(NEW.newest_accepted_at)
        THEN RAISE(ABORT, 'window cannot finalize before its accepted evidence') END;
END;

CREATE TRIGGER market_confirmation_matches_preview_bounds
BEFORE INSERT ON market_collection_confirmations
WHEN NOT EXISTS (
    SELECT 1
    FROM market_collection_previews preview
    JOIN campaign_revisions revision ON revision.id=preview.campaign_revision_id
    WHERE preview.preview_id = NEW.preview_id
      AND preview.plan_sha256 = NEW.plan_sha256
      AND preview.purpose = NEW.purpose
      AND preview.currency_code = NEW.currency_code
      AND NEW.acknowledged_observation_count = preview.logical_observation_count
      AND NEW.maximum_cost_minor_units >= preview.estimated_cost_minor_units
      AND julianday(NEW.confirmed_at) < julianday(preview.expires_at)
      AND preview.blockers_json = '[]'
      AND revision.state IN ('planned','active')
)
BEGIN SELECT RAISE(ABORT, 'confirmation does not satisfy exact preview bounds'); END;

CREATE TRIGGER market_occurrence_run_requires_finalized_input
BEFORE INSERT ON market_occurrence_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM market_window_finalizations finalization
    WHERE finalization.window_id = NEW.window_id
      AND finalization.measurement_spec_sha256 = NEW.measurement_spec_sha256
      AND finalization.purpose = NEW.purpose
      AND finalization.accepted_run_manifest_sha256 = NEW.input_manifest_sha256
      AND julianday(NEW.created_at) >= julianday(finalization.finalized_at)
)
BEGIN SELECT RAISE(ABORT, 'occurrence run requires the exact finalized window manifest'); END;

CREATE TRIGGER market_classification_run_requires_occurrence_output
BEFORE INSERT ON market_classification_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM market_occurrence_runs occurrence_run
    WHERE occurrence_run.occurrence_run_id = NEW.occurrence_run_id
      AND occurrence_run.purpose = NEW.purpose
      AND occurrence_run.output_manifest_sha256 = NEW.input_manifest_sha256
      AND julianday(NEW.created_at) >= julianday(occurrence_run.created_at)
)
BEGIN SELECT RAISE(ABORT, 'classification run requires the exact occurrence output manifest'); END;

CREATE TRIGGER market_projection_run_requires_classification_output
BEFORE INSERT ON market_projection_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM market_classification_runs classification_run
    WHERE classification_run.classification_run_id = NEW.classification_run_id
      AND classification_run.purpose = NEW.purpose
      AND classification_run.output_manifest_sha256 = NEW.input_manifest_sha256
      AND julianday(NEW.created_at) >= julianday(classification_run.created_at)
)
BEGIN SELECT RAISE(ABORT, 'projection run requires the exact classification output manifest'); END;

CREATE TRIGGER market_literal_occurrence_lineage_guard
BEFORE INSERT ON market_literal_occurrences
WHEN NOT EXISTS (
    SELECT 1
    FROM market_occurrence_runs occurrence_run
    JOIN market_logical_observations observation
      ON observation.logical_observation_id = NEW.logical_observation_id
     AND observation.window_id = occurrence_run.window_id
     AND observation.measurement_spec_sha256 = occurrence_run.measurement_spec_sha256
     AND observation.purpose = occurrence_run.purpose
    JOIN market_observation_acceptances acceptance
      ON acceptance.logical_observation_id = observation.logical_observation_id
     AND acceptance.purpose = observation.purpose
    JOIN market_artifact_bindings binding
      ON binding.attempt_id = acceptance.attempt_id
     AND binding.logical_observation_id = observation.logical_observation_id
     AND binding.artifact_sha256 = NEW.artifact_sha256
     AND binding.purpose = observation.purpose
    WHERE occurrence_run.occurrence_run_id = NEW.occurrence_run_id
      AND occurrence_run.purpose = NEW.purpose
)
BEGIN SELECT RAISE(ABORT, 'literal occurrence lineage crosses evidence boundaries'); END;

CREATE TRIGGER market_classification_result_lineage_guard
BEFORE INSERT ON market_classification_results
WHEN NOT EXISTS (
    SELECT 1
    FROM market_classification_runs classification_run
    JOIN market_literal_occurrences occurrence
      ON occurrence.occurrence_run_id = classification_run.occurrence_run_id
     AND occurrence.purpose = classification_run.purpose
    WHERE classification_run.classification_run_id = NEW.classification_run_id
      AND occurrence.occurrence_id = NEW.occurrence_id
      AND classification_run.purpose = NEW.purpose
)
BEGIN SELECT RAISE(ABORT, 'classification result crosses occurrence-run lineage'); END;

CREATE TRIGGER market_projection_match_lineage_guard
BEFORE INSERT ON market_projection_matches
WHEN NOT EXISTS (
    SELECT 1
    FROM market_projection_runs projection_run
    JOIN market_classification_runs classification_run
      ON classification_run.classification_run_id = projection_run.classification_run_id
     AND classification_run.purpose = projection_run.purpose
    JOIN market_literal_occurrences occurrence
      ON occurrence.occurrence_run_id = classification_run.occurrence_run_id
     AND occurrence.purpose = classification_run.purpose
    WHERE projection_run.projection_run_id = NEW.projection_run_id
      AND occurrence.occurrence_id = NEW.occurrence_id
      AND projection_run.purpose = NEW.purpose
)
BEGIN SELECT RAISE(ABORT, 'projection match crosses classification lineage'); END;

CREATE TRIGGER market_job_starts_queued_for_confirmed_plan_observation
BEFORE INSERT ON market_collection_jobs
WHEN NEW.state <> 'queued'
  OR NEW.send_phase <> 'not_started'
  OR NOT EXISTS (
      SELECT 1
      FROM market_logical_observations observation
      JOIN market_measurement_windows window
        ON window.window_id = observation.window_id
       AND window.measurement_spec_sha256 = observation.measurement_spec_sha256
       AND window.purpose = observation.purpose
      JOIN market_demand_plan_specifications plan_specification
        ON plan_specification.measurement_spec_sha256 = observation.measurement_spec_sha256
       AND plan_specification.plan_sha256 = NEW.plan_sha256
       AND plan_specification.purpose = observation.purpose
      JOIN market_demand_plans plan
        ON plan.plan_sha256 = plan_specification.plan_sha256
       AND plan.purpose = plan_specification.purpose
      JOIN market_collection_confirmations confirmation
        ON confirmation.confirmation_id = NEW.confirmation_id
       AND confirmation.plan_sha256 = NEW.plan_sha256
       AND confirmation.purpose = NEW.purpose
      JOIN market_collection_previews preview
        ON preview.preview_id = confirmation.preview_id
       AND preview.plan_sha256 = confirmation.plan_sha256
       AND preview.purpose = confirmation.purpose
      JOIN campaign_revisions revision
        ON revision.id = preview.campaign_revision_id
      JOIN market_measurement_specifications specification
        ON specification.measurement_spec_sha256 = observation.measurement_spec_sha256
       AND specification.purpose = observation.purpose
      WHERE observation.logical_observation_id = NEW.logical_observation_id
        AND observation.purpose = NEW.purpose
        AND plan_specification.evidence_status <> 'reused'
        AND specification.data_mode = 'live'
        AND NEW.maximum_attempts = 3
        AND NEW.backoff_policy_version = 'market-retry-v1'
        AND NEW.backoff_policy_version = specification.retry_policy_version
        AND revision.state IN ('planned','active')
        AND window.window_id =
              'market-window-' || NEW.confirmation_id || '-' ||
              observation.measurement_spec_sha256
        AND EXISTS (
          SELECT 1
          FROM json_each(plan.canonical_json, '$.live_collector_actions') action
          WHERE json_extract(action.value, '$.schema_version') =
                  'market_evidence.live_collector_action.v1'
            AND json_extract(action.value, '$.measurement_spec_sha256') =
                  observation.measurement_spec_sha256
            AND CAST(json_extract(action.value, '$.run_ordinal') AS INTEGER) =
                  observation.run_ordinal
            AND json_extract(action.value, '$.planned_profile_id') =
                  observation.planned_profile_id
            AND json_extract(action.value, '$.planned_profile_revision') =
                  observation.planned_profile_revision
            AND json_extract(action.value, '$.source_platform') =
                  observation.source_platform
        )
  )
BEGIN SELECT RAISE(ABORT, 'jobs must start queued for confirmed non-reused demand'); END;

CREATE TRIGGER market_job_attempt_provenance_guard
BEFORE INSERT ON market_observation_attempts
WHEN NEW.job_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM market_collection_jobs job
    WHERE job.job_id=NEW.job_id
      AND job.logical_observation_id=NEW.logical_observation_id
      AND job.purpose=NEW.purpose
      AND job.lease_generation=NEW.lease_generation
      AND job.state='leased'
      AND (
        (NEW.send_phase='not_started'
          AND NEW.attempt_ordinal=job.attempt_count+1
          AND job.send_phase='not_started')
        OR
        (NEW.send_phase='not_started'
          AND NEW.attempt_id=job.reserved_attempt_id
          AND NEW.attempt_ordinal=job.attempt_count
          AND job.send_phase='attempt_reserved')
        OR
        (NEW.send_phase IN ('send_acknowledged','send_unknown')
          AND NEW.attempt_id=job.reserved_attempt_id
          AND NEW.attempt_ordinal=job.attempt_count
          AND job.send_phase='send_started')
      )
)
BEGIN SELECT RAISE(ABORT, 'job-backed attempt provenance does not match its lease'); END;

CREATE TRIGGER market_preview_requires_open_campaign_consumer
BEFORE INSERT ON market_collection_previews
WHEN NOT EXISTS (
    SELECT 1
    FROM market_demand_consumers consumer
    JOIN campaign_revisions revision ON revision.id=consumer.campaign_revision_id
    WHERE consumer.plan_sha256=NEW.plan_sha256
      AND consumer.campaign_revision_id=NEW.campaign_revision_id
      AND consumer.purpose=NEW.purpose
      AND revision.state IN ('planned','active')
)
BEGIN SELECT RAISE(ABORT, 'preview requires a non-completed exact campaign consumer'); END;

CREATE TRIGGER market_campaign_completion_requires_resolved_collection
BEFORE UPDATE OF state ON campaign_revisions
WHEN NEW.state='completed' AND OLD.state<>'completed' AND EXISTS (
    SELECT 1
    FROM market_collection_previews preview
    JOIN market_collection_confirmations confirmation
      ON confirmation.preview_id=preview.preview_id
     AND confirmation.plan_sha256=preview.plan_sha256
     AND confirmation.purpose=preview.purpose
    JOIN market_confirmation_jobs association
      ON association.confirmation_id=confirmation.confirmation_id
    JOIN market_collection_jobs job ON job.job_id=association.job_id
    WHERE preview.campaign_revision_id=NEW.id
      AND (
        job.state IN ('leased','send_unknown')
        OR (
          job.state IN ('queued','retry_wait')
          AND NOT EXISTS (
            SELECT 1
            FROM market_confirmation_jobs other_association
            JOIN market_collection_confirmations other_confirmation
              ON other_confirmation.confirmation_id=other_association.confirmation_id
            JOIN market_collection_previews other_preview
              ON other_preview.preview_id=other_confirmation.preview_id
             AND other_preview.plan_sha256=other_confirmation.plan_sha256
             AND other_preview.purpose=other_confirmation.purpose
            JOIN campaign_revisions other_revision
              ON other_revision.id=other_preview.campaign_revision_id
            WHERE other_association.job_id=job.job_id
              AND other_preview.campaign_revision_id<>preview.campaign_revision_id
              AND other_revision.state<>'completed'
          )
        )
      )
)
BEGIN SELECT RAISE(ABORT, 'campaign collection must be cancelled or resolved before completion'); END;

CREATE TRIGGER market_send_unknown_resolution_requires_unresolved_job
BEFORE INSERT ON market_send_unknown_resolutions
WHEN NOT EXISTS (
    SELECT 1 FROM market_collection_jobs job
    JOIN market_observation_attempts attempt
      ON attempt.attempt_id=job.last_attempt_id
     AND attempt.purpose=job.purpose
    WHERE job.job_id = NEW.job_id
      AND job.purpose = NEW.purpose
      AND job.state = 'send_unknown'
      AND job.send_phase = 'send_unknown'
      AND job.last_attempt_id = NEW.attempt_id
      AND julianday(NEW.resolved_at) >= julianday(job.updated_at)
      AND julianday(NEW.resolved_at) >= julianday(attempt.completed_at)
)
BEGIN SELECT RAISE(ABORT, 'resolution requires the exact unresolved send'); END;

CREATE TRIGGER market_confirmation_job_exact_lineage
BEFORE INSERT ON market_confirmation_jobs
WHEN NOT EXISTS (
    SELECT 1
    FROM market_collection_confirmations confirmation
    JOIN market_collection_previews preview
      ON preview.preview_id=confirmation.preview_id
     AND preview.plan_sha256=confirmation.plan_sha256
     AND preview.purpose=confirmation.purpose
    JOIN campaign_revisions revision
      ON revision.id=preview.campaign_revision_id
    JOIN market_collection_jobs job ON job.job_id=NEW.job_id
    JOIN market_logical_observations observation
      ON observation.logical_observation_id=job.logical_observation_id
     AND observation.purpose=job.purpose
    JOIN market_demand_plan_specifications demanded
      ON demanded.plan_sha256=confirmation.plan_sha256
     AND demanded.measurement_spec_sha256=observation.measurement_spec_sha256
     AND demanded.purpose=confirmation.purpose
    JOIN market_demand_plans plan
      ON plan.plan_sha256=confirmation.plan_sha256
     AND plan.purpose=confirmation.purpose
    JOIN market_measurement_specifications specification
      ON specification.measurement_spec_sha256=observation.measurement_spec_sha256
     AND specification.purpose=observation.purpose
    WHERE confirmation.confirmation_id=NEW.confirmation_id
      AND job.logical_observation_id=NEW.logical_observation_id
      AND job.purpose=NEW.purpose
      AND demanded.evidence_status<>'reused'
      AND specification.data_mode='live'
      AND revision.state IN ('planned','active')
      AND EXISTS (
        SELECT 1
        FROM json_each(plan.canonical_json, '$.live_collector_actions') action
        WHERE json_extract(action.value, '$.schema_version') =
                'market_evidence.live_collector_action.v1'
          AND json_extract(action.value, '$.measurement_spec_sha256') =
                observation.measurement_spec_sha256
          AND CAST(json_extract(action.value, '$.run_ordinal') AS INTEGER) =
                observation.run_ordinal
          AND json_extract(action.value, '$.planned_profile_id') =
                observation.planned_profile_id
          AND json_extract(action.value, '$.planned_profile_revision') =
                observation.planned_profile_revision
          AND json_extract(action.value, '$.source_platform') =
                observation.source_platform
      )
)
BEGIN SELECT RAISE(ABORT, 'confirmation-job association is not demanded exact live work'); END;

CREATE TRIGGER market_job_valid_transition
BEFORE UPDATE OF state ON market_collection_jobs
WHEN NOT (
    (OLD.state = 'queued' AND OLD.send_phase='not_started'
        AND NEW.state IN ('leased','cancelled')
        AND NEW.send_phase='not_started')
    OR (OLD.state = 'retry_wait' AND OLD.send_phase='not_started'
        AND NEW.state IN ('leased','cancelled')
        AND NEW.send_phase='not_started')
    OR (OLD.state = 'leased' AND NEW.state='leased' AND (
        NEW.send_phase=OLD.send_phase
        OR (OLD.send_phase='not_started'
            AND NEW.send_phase='attempt_reserved')
        OR (OLD.send_phase='attempt_reserved'
            AND NEW.send_phase='send_started')
    ))
    OR (OLD.state='leased' AND NEW.state IN ('queued','retry_wait')
        AND OLD.send_phase IN ('not_started','attempt_reserved')
        AND NEW.send_phase='not_started')
    OR (OLD.state='leased' AND NEW.state='terminal_failed'
        AND OLD.send_phase IN ('not_started','attempt_reserved')
        AND NEW.send_phase='not_started')
    OR (OLD.state='leased' AND OLD.send_phase='send_started'
        AND NEW.state IN ('succeeded','terminal_failed')
        AND NEW.send_phase='send_acknowledged')
    OR (OLD.state='leased' AND OLD.send_phase='send_started'
        AND NEW.state='send_unknown' AND NEW.send_phase='send_unknown')
    OR (OLD.state = 'send_unknown' AND NEW.state = 'terminal_failed'
      AND OLD.send_phase='send_unknown' AND NEW.send_phase='send_unknown'
      AND EXISTS (
        SELECT 1 FROM market_send_unknown_resolutions resolution
        WHERE resolution.job_id = OLD.job_id
          AND resolution.attempt_id = OLD.last_attempt_id
          AND resolution.purpose = OLD.purpose
          AND resolution.from_state = 'send_unknown'
          AND resolution.resolution = NEW.state
          AND julianday(resolution.resolved_at) = julianday(NEW.updated_at)
    ))
    OR (OLD.state IN ('succeeded','terminal_failed','cancelled')
        AND NEW.state = OLD.state AND NEW.send_phase=OLD.send_phase)
)
BEGIN SELECT RAISE(ABORT, 'invalid market collection job state transition'); END;

CREATE TRIGGER market_job_send_started_exit_requires_exact_attempt
BEFORE UPDATE ON market_collection_jobs
WHEN OLD.state='leased' AND OLD.send_phase='send_started'
 AND NOT (
    (NEW.state='leased' AND NEW.send_phase='send_started')
    OR (
      NEW.reserved_attempt_id IS OLD.reserved_attempt_id
      AND NEW.last_attempt_id=OLD.reserved_attempt_id
      AND EXISTS (
        SELECT 1 FROM market_observation_attempts attempt
        WHERE attempt.attempt_id=OLD.reserved_attempt_id
          AND attempt.job_id=OLD.job_id
          AND attempt.logical_observation_id=OLD.logical_observation_id
          AND attempt.purpose=OLD.purpose
          AND attempt.attempt_ordinal=OLD.attempt_count
          AND attempt.lease_generation=OLD.lease_generation
          AND (
            (NEW.state='succeeded'
             AND NEW.send_phase='send_acknowledged'
             AND attempt.send_phase='send_acknowledged'
             AND attempt.outcome='succeeded')
            OR
            (NEW.state='terminal_failed'
             AND NEW.send_phase='send_acknowledged'
             AND attempt.send_phase='send_acknowledged'
             AND attempt.outcome IN ('succeeded','incompatible'))
            OR
            (NEW.state='send_unknown'
             AND NEW.send_phase='send_unknown'
             AND attempt.send_phase='send_unknown'
             AND attempt.outcome='send_unknown')
          )
      )
    )
 )
BEGIN SELECT RAISE(ABORT, 'send-started work requires its exact immutable outcome attempt'); END;

CREATE TRIGGER market_job_lease_generation_guard
BEFORE UPDATE ON market_collection_jobs
BEGIN
    SELECT CASE WHEN NEW.attempt_count < OLD.attempt_count
        OR NEW.lease_generation < OLD.lease_generation
        OR NEW.state_version <> OLD.state_version + 1
        OR julianday(NEW.updated_at) < julianday(OLD.updated_at)
        THEN RAISE(ABORT, 'job counters and state version are monotonic') END;
    SELECT CASE WHEN NEW.state = 'leased' AND OLD.state <> 'leased'
        AND NEW.lease_generation <> OLD.lease_generation + 1
        THEN RAISE(ABORT, 'claim must advance the lease generation') END;
    SELECT CASE WHEN NEW.lease_generation <> OLD.lease_generation AND NOT (
        OLD.state IN ('queued','retry_wait')
        AND NEW.state='leased'
        AND NEW.lease_generation=OLD.lease_generation+1
    ) THEN RAISE(ABORT, 'lease generation may change only on claim') END;
    SELECT CASE WHEN NEW.state = 'leased' AND OLD.state = 'leased'
        AND (NEW.lease_generation <> OLD.lease_generation
             OR NEW.lease_token <> OLD.lease_token
             OR NEW.worker_id <> OLD.worker_id)
        THEN RAISE(ABORT, 'heartbeat cannot replace lease ownership') END;
    SELECT CASE WHEN OLD.state = 'leased' AND NEW.state = 'retry_wait'
        AND OLD.send_phase NOT IN ('not_started','attempt_reserved')
        THEN RAISE(ABORT, 'ambiguous sends cannot enter ordinary retry') END;
    SELECT CASE WHEN OLD.state = 'leased' AND NEW.state = 'queued'
        AND OLD.send_phase NOT IN ('not_started','attempt_reserved')
        THEN RAISE(ABORT, 'only untouched leases can return directly to queued') END;
    SELECT CASE WHEN NEW.state = 'succeeded' AND NOT EXISTS (
        SELECT 1 FROM market_observation_acceptances acceptance
        WHERE acceptance.logical_observation_id = NEW.logical_observation_id
          AND acceptance.purpose = NEW.purpose
    ) THEN RAISE(ABORT, 'successful jobs require accepted evidence') END;
END;

CREATE TRIGGER market_job_operational_fields_guard
BEFORE UPDATE ON market_collection_jobs
BEGIN
    SELECT CASE WHEN OLD.state = NEW.state AND OLD.state <> 'leased'
        THEN RAISE(ABORT, 'non-leased jobs cannot receive no-op rewrites') END;
    SELECT CASE WHEN NEW.attempt_count <> OLD.attempt_count AND NOT (
        NEW.attempt_count = OLD.attempt_count + 1
        AND OLD.state = 'leased'
        AND OLD.send_phase = 'not_started'
        AND (
          (NEW.state = 'leased' AND NEW.send_phase = 'attempt_reserved')
          OR (NEW.state IN ('retry_wait','terminal_failed')
              AND NEW.send_phase = 'not_started')
        )
    ) THEN RAISE(ABORT, 'attempt count may advance only for one owned attempt') END;
    SELECT CASE WHEN NEW.last_attempt_id IS NOT OLD.last_attempt_id AND NOT (
        OLD.state = 'leased'
        AND NEW.last_attempt_id IS NOT NULL
        AND NEW.state IN ('queued','retry_wait','succeeded','terminal_failed','send_unknown')
        AND EXISTS (
          SELECT 1 FROM market_observation_attempts attempt
          WHERE attempt.attempt_id=NEW.last_attempt_id
            AND attempt.job_id=NEW.job_id
            AND attempt.logical_observation_id=NEW.logical_observation_id
            AND attempt.lease_generation=OLD.lease_generation
            AND attempt.purpose=NEW.purpose
            AND NEW.last_error_code IS attempt.error_code
        )
    ) THEN RAISE(ABORT, 'last attempt must be an append-only owned outcome') END;
    SELECT CASE WHEN NEW.last_error_code IS NOT OLD.last_error_code
        AND NEW.last_attempt_id IS OLD.last_attempt_id
        THEN RAISE(ABORT, 'job errors may change only with an outcome attempt') END;
    SELECT CASE WHEN NEW.next_attempt_at IS NOT OLD.next_attempt_at AND NOT (
        (OLD.state='leased' AND NEW.state='retry_wait'
          AND NEW.next_attempt_at IS NOT NULL)
        OR (OLD.state='retry_wait' AND NEW.state IN ('leased','cancelled')
          AND NEW.next_attempt_at IS NULL)
    ) THEN RAISE(ABORT, 'next-attempt time may change only at retry boundaries') END;
END;

CREATE TRIGGER market_job_identity_guard
BEFORE UPDATE ON market_collection_jobs
WHEN NEW.job_id IS NOT OLD.job_id
  OR NEW.logical_observation_id IS NOT OLD.logical_observation_id
  OR NEW.confirmation_id IS NOT OLD.confirmation_id
  OR NEW.plan_sha256 IS NOT OLD.plan_sha256
  OR NEW.purpose IS NOT OLD.purpose
  OR NEW.maximum_attempts IS NOT OLD.maximum_attempts
  OR NEW.backoff_policy_version IS NOT OLD.backoff_policy_version
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'market collection job identity is immutable'); END;

CREATE TRIGGER market_job_send_phase_guard
BEFORE UPDATE ON market_collection_jobs
WHEN NOT (
    (NEW.send_phase = OLD.send_phase
        AND NOT (OLD.state='leased' AND OLD.send_phase='send_started'
                 AND NEW.state<>OLD.state))
    OR (OLD.state = 'leased' AND NEW.state = 'leased'
        AND OLD.send_phase = 'not_started' AND NEW.send_phase = 'attempt_reserved')
    OR (OLD.state = 'leased' AND NEW.state = 'leased'
        AND OLD.send_phase = 'attempt_reserved' AND NEW.send_phase = 'send_started')
    OR (OLD.state = 'leased' AND OLD.send_phase = 'send_started'
        AND NEW.send_phase = 'send_acknowledged'
        AND NEW.state IN ('succeeded','terminal_failed'))
    OR (OLD.state = 'leased' AND OLD.send_phase = 'send_started'
        AND NEW.send_phase = 'send_unknown' AND NEW.state = 'send_unknown')
    OR (OLD.state = 'leased' AND NEW.state IN ('queued','retry_wait')
        AND OLD.send_phase IN ('not_started','attempt_reserved')
        AND NEW.send_phase = 'not_started')
    OR (OLD.state = 'leased' AND NEW.state = 'terminal_failed'
        AND OLD.send_phase = 'attempt_reserved'
        AND NEW.send_phase = 'not_started')
)
BEGIN SELECT RAISE(ABORT, 'invalid durable send-phase transition'); END;

CREATE TRIGGER market_job_reserved_attempt_guard
BEFORE UPDATE OF reserved_attempt_id ON market_collection_jobs
WHEN NOT (
    NEW.reserved_attempt_id IS OLD.reserved_attempt_id
    OR (OLD.state = 'leased' AND NEW.state = 'leased'
        AND OLD.send_phase = 'not_started' AND NEW.send_phase = 'attempt_reserved'
        AND OLD.reserved_attempt_id IS NULL AND NEW.reserved_attempt_id IS NOT NULL)
    OR (OLD.state = 'leased' AND NEW.state IN ('queued','retry_wait')
        AND OLD.send_phase IN ('not_started','attempt_reserved')
        AND NEW.send_phase = 'not_started' AND NEW.reserved_attempt_id IS NULL)
    OR (OLD.state = 'leased' AND NEW.state = 'terminal_failed'
        AND OLD.send_phase = 'attempt_reserved'
        AND NEW.send_phase = 'not_started' AND NEW.reserved_attempt_id IS NULL)
)
BEGIN SELECT RAISE(ABORT, 'invalid durable reserved-attempt transition'); END;

-- SQLite's INSERT OR REPLACE is a delete followed by an insert, and the
-- implicit delete does not reliably run DELETE triggers when
-- recursive_triggers is disabled.  Collision guards make immutability a
-- schema property regardless of connection PRAGMAs.
CREATE TRIGGER market_specs_no_identity_reinsert
BEFORE INSERT ON market_measurement_specifications
WHEN EXISTS (SELECT 1 FROM market_measurement_specifications
             WHERE measurement_spec_sha256 = NEW.measurement_spec_sha256)
BEGIN SELECT RAISE(ABORT, 'market specification identity already exists'); END;
CREATE TRIGGER market_plans_no_identity_reinsert
BEFORE INSERT ON market_demand_plans
WHEN EXISTS (SELECT 1 FROM market_demand_plans WHERE plan_sha256 = NEW.plan_sha256)
BEGIN SELECT RAISE(ABORT, 'market demand plan identity already exists'); END;
CREATE TRIGGER market_plan_specs_no_identity_reinsert
BEFORE INSERT ON market_demand_plan_specifications
WHEN EXISTS (SELECT 1 FROM market_demand_plan_specifications
             WHERE plan_sha256 = NEW.plan_sha256
               AND measurement_spec_sha256 = NEW.measurement_spec_sha256)
BEGIN SELECT RAISE(ABORT, 'market plan binding identity already exists'); END;
CREATE TRIGGER market_consumers_no_identity_reinsert
BEFORE INSERT ON market_demand_consumers
WHEN EXISTS (SELECT 1 FROM market_demand_consumers
             WHERE consumer_id = NEW.consumer_id
                OR consumer_identity_sha256 = NEW.consumer_identity_sha256
                OR (plan_sha256 = NEW.plan_sha256
                    AND campaign_revision_id = NEW.campaign_revision_id))
BEGIN SELECT RAISE(ABORT, 'market demand consumer identity already exists'); END;
CREATE TRIGGER market_windows_no_identity_reinsert
BEFORE INSERT ON market_measurement_windows
WHEN EXISTS (SELECT 1 FROM market_measurement_windows WHERE window_id = NEW.window_id)
BEGIN SELECT RAISE(ABORT, 'market measurement window identity already exists'); END;
CREATE TRIGGER market_observations_no_identity_reinsert
BEFORE INSERT ON market_logical_observations
WHEN EXISTS (SELECT 1 FROM market_logical_observations
             WHERE logical_observation_id = NEW.logical_observation_id
                OR identity_sha256 = NEW.identity_sha256
                OR (window_id = NEW.window_id AND run_ordinal = NEW.run_ordinal))
BEGIN SELECT RAISE(ABORT, 'market logical observation identity already exists'); END;
CREATE TRIGGER market_attempts_no_identity_reinsert
BEFORE INSERT ON market_observation_attempts
WHEN EXISTS (SELECT 1 FROM market_observation_attempts
             WHERE attempt_id = NEW.attempt_id
                OR (logical_observation_id = NEW.logical_observation_id
                    AND attempt_ordinal = NEW.attempt_ordinal))
BEGIN SELECT RAISE(ABORT, 'market observation attempt identity already exists'); END;
CREATE TRIGGER market_acceptances_no_identity_reinsert
BEFORE INSERT ON market_observation_acceptances
WHEN EXISTS (SELECT 1 FROM market_observation_acceptances
             WHERE acceptance_id = NEW.acceptance_id
                OR logical_observation_id = NEW.logical_observation_id
                OR attempt_id = NEW.attempt_id)
BEGIN SELECT RAISE(ABORT, 'market observation acceptance identity already exists'); END;
CREATE TRIGGER market_finalizations_no_identity_reinsert
BEFORE INSERT ON market_window_finalizations
WHEN EXISTS (SELECT 1 FROM market_window_finalizations
             WHERE finalization_id = NEW.finalization_id OR window_id = NEW.window_id)
BEGIN SELECT RAISE(ABORT, 'market window finalization identity already exists'); END;
CREATE TRIGGER market_artifacts_no_identity_reinsert
BEFORE INSERT ON market_artifacts
WHEN EXISTS (SELECT 1 FROM market_artifacts
             WHERE artifact_sha256 = NEW.artifact_sha256 AND purpose = NEW.purpose)
BEGIN SELECT RAISE(ABORT, 'market artifact identity already exists'); END;
CREATE TRIGGER market_bindings_no_identity_reinsert
BEFORE INSERT ON market_artifact_bindings
WHEN EXISTS (SELECT 1 FROM market_artifact_bindings
             WHERE binding_id = NEW.binding_id
                OR binding_sha256 = NEW.binding_sha256
                OR (attempt_id = NEW.attempt_id AND artifact_role = NEW.artifact_role))
BEGIN SELECT RAISE(ABORT, 'market artifact binding identity already exists'); END;
CREATE TRIGGER market_quarantines_no_identity_reinsert
BEFORE INSERT ON market_artifact_quarantines
WHEN EXISTS (SELECT 1 FROM market_artifact_quarantines
             WHERE quarantine_id = NEW.quarantine_id)
BEGIN SELECT RAISE(ABORT, 'market quarantine identity already exists'); END;
CREATE TRIGGER market_previews_no_identity_reinsert
BEFORE INSERT ON market_collection_previews
WHEN EXISTS (SELECT 1 FROM market_collection_previews
             WHERE preview_id = NEW.preview_id OR preview_sha256 = NEW.preview_sha256)
BEGIN SELECT RAISE(ABORT, 'market preview identity already exists'); END;
CREATE TRIGGER market_confirmations_no_identity_reinsert
BEFORE INSERT ON market_collection_confirmations
WHEN EXISTS (SELECT 1 FROM market_collection_confirmations
             WHERE confirmation_id = NEW.confirmation_id
                OR confirmation_sha256 = NEW.confirmation_sha256
                OR (purpose = NEW.purpose AND idempotency_key = NEW.idempotency_key))
BEGIN SELECT RAISE(ABORT, 'market confirmation identity already exists'); END;
CREATE TRIGGER market_jobs_no_identity_reinsert
BEFORE INSERT ON market_collection_jobs
WHEN EXISTS (SELECT 1 FROM market_collection_jobs
             WHERE job_id = NEW.job_id
                OR logical_observation_id = NEW.logical_observation_id)
BEGIN SELECT RAISE(ABORT, 'market collection job identity already exists'); END;
CREATE TRIGGER market_resolutions_no_identity_reinsert
BEFORE INSERT ON market_send_unknown_resolutions
WHEN EXISTS (SELECT 1 FROM market_send_unknown_resolutions
             WHERE resolution_id = NEW.resolution_id OR job_id = NEW.job_id)
BEGIN SELECT RAISE(ABORT, 'market unknown-send resolution already exists'); END;
CREATE TRIGGER market_confirmation_jobs_no_identity_reinsert
BEFORE INSERT ON market_confirmation_jobs
WHEN EXISTS (SELECT 1 FROM market_confirmation_jobs
             WHERE (confirmation_id=NEW.confirmation_id AND job_id=NEW.job_id)
                OR (confirmation_id=NEW.confirmation_id
                    AND logical_observation_id=NEW.logical_observation_id))
BEGIN SELECT RAISE(ABORT, 'market confirmation-job association already exists'); END;
CREATE TRIGGER market_occurrence_runs_no_identity_reinsert
BEFORE INSERT ON market_occurrence_runs
WHEN EXISTS (SELECT 1 FROM market_occurrence_runs
             WHERE occurrence_run_id = NEW.occurrence_run_id)
BEGIN SELECT RAISE(ABORT, 'market occurrence run identity already exists'); END;
CREATE TRIGGER market_occurrences_no_identity_reinsert
BEFORE INSERT ON market_literal_occurrences
WHEN EXISTS (SELECT 1 FROM market_literal_occurrences
             WHERE occurrence_id = NEW.occurrence_id
                OR occurrence_sha256 = NEW.occurrence_sha256)
BEGIN SELECT RAISE(ABORT, 'market occurrence identity already exists'); END;
CREATE TRIGGER market_classification_runs_no_identity_reinsert
BEFORE INSERT ON market_classification_runs
WHEN EXISTS (SELECT 1 FROM market_classification_runs
             WHERE classification_run_id = NEW.classification_run_id)
BEGIN SELECT RAISE(ABORT, 'market classification run identity already exists'); END;
CREATE TRIGGER market_classification_results_no_identity_reinsert
BEFORE INSERT ON market_classification_results
WHEN EXISTS (SELECT 1 FROM market_classification_results
             WHERE classification_result_id = NEW.classification_result_id
                OR result_sha256 = NEW.result_sha256)
BEGIN SELECT RAISE(ABORT, 'market classification result identity already exists'); END;
CREATE TRIGGER market_projection_runs_no_identity_reinsert
BEFORE INSERT ON market_projection_runs
WHEN EXISTS (SELECT 1 FROM market_projection_runs
             WHERE projection_run_id = NEW.projection_run_id)
BEGIN SELECT RAISE(ABORT, 'market projection run identity already exists'); END;
CREATE TRIGGER market_projection_matches_no_identity_reinsert
BEFORE INSERT ON market_projection_matches
WHEN EXISTS (SELECT 1 FROM market_projection_matches
             WHERE projection_match_id = NEW.projection_match_id
                OR match_sha256 = NEW.match_sha256)
BEGIN SELECT RAISE(ABORT, 'market projection match identity already exists'); END;

-- Immutable evidence, derived foundations, previews, confirmations, and
-- quarantine events cannot be rewritten or removed.  The jobs table above is
-- intentionally excluded because its guarded operational state is mutable.
CREATE TRIGGER market_specs_no_update BEFORE UPDATE ON market_measurement_specifications
BEGIN SELECT RAISE(ABORT, 'market specifications are immutable'); END;
CREATE TRIGGER market_specs_no_delete BEFORE DELETE ON market_measurement_specifications
BEGIN SELECT RAISE(ABORT, 'market specifications are immutable'); END;
CREATE TRIGGER market_plans_no_update BEFORE UPDATE ON market_demand_plans
BEGIN SELECT RAISE(ABORT, 'market demand plans are immutable'); END;
CREATE TRIGGER market_plans_no_delete BEFORE DELETE ON market_demand_plans
BEGIN SELECT RAISE(ABORT, 'market demand plans are immutable'); END;
CREATE TRIGGER market_plan_specs_no_update BEFORE UPDATE ON market_demand_plan_specifications
BEGIN SELECT RAISE(ABORT, 'market plan bindings are immutable'); END;
CREATE TRIGGER market_plan_specs_no_delete BEFORE DELETE ON market_demand_plan_specifications
BEGIN SELECT RAISE(ABORT, 'market plan bindings are immutable'); END;
CREATE TRIGGER market_consumers_no_update BEFORE UPDATE ON market_demand_consumers
BEGIN SELECT RAISE(ABORT, 'market demand consumers are immutable'); END;
CREATE TRIGGER market_consumers_no_delete BEFORE DELETE ON market_demand_consumers
BEGIN SELECT RAISE(ABORT, 'market demand consumers are immutable'); END;
CREATE TRIGGER market_windows_no_update BEFORE UPDATE ON market_measurement_windows
BEGIN SELECT RAISE(ABORT, 'market measurement windows are immutable'); END;
CREATE TRIGGER market_windows_no_delete BEFORE DELETE ON market_measurement_windows
BEGIN SELECT RAISE(ABORT, 'market measurement windows are immutable'); END;
CREATE TRIGGER market_observations_no_update BEFORE UPDATE ON market_logical_observations
BEGIN SELECT RAISE(ABORT, 'market logical observations are immutable'); END;
CREATE TRIGGER market_observations_no_delete BEFORE DELETE ON market_logical_observations
BEGIN SELECT RAISE(ABORT, 'market logical observations are immutable'); END;
CREATE TRIGGER market_attempts_no_update BEFORE UPDATE ON market_observation_attempts
BEGIN SELECT RAISE(ABORT, 'market observation attempts are append-only'); END;
CREATE TRIGGER market_attempts_no_delete BEFORE DELETE ON market_observation_attempts
BEGIN SELECT RAISE(ABORT, 'market observation attempts are append-only'); END;
CREATE TRIGGER market_acceptances_no_update BEFORE UPDATE ON market_observation_acceptances
BEGIN SELECT RAISE(ABORT, 'market observation acceptances are immutable'); END;
CREATE TRIGGER market_acceptances_no_delete BEFORE DELETE ON market_observation_acceptances
BEGIN SELECT RAISE(ABORT, 'market observation acceptances are immutable'); END;
CREATE TRIGGER market_finalizations_no_update BEFORE UPDATE ON market_window_finalizations
BEGIN SELECT RAISE(ABORT, 'market window finalizations are immutable'); END;
CREATE TRIGGER market_finalizations_no_delete BEFORE DELETE ON market_window_finalizations
BEGIN SELECT RAISE(ABORT, 'market window finalizations are immutable'); END;
CREATE TRIGGER market_artifacts_no_update BEFORE UPDATE ON market_artifacts
BEGIN SELECT RAISE(ABORT, 'market artifact metadata is immutable'); END;
CREATE TRIGGER market_artifacts_no_delete BEFORE DELETE ON market_artifacts
BEGIN SELECT RAISE(ABORT, 'referenced market artifacts cannot be removed'); END;
CREATE TRIGGER market_bindings_no_update BEFORE UPDATE ON market_artifact_bindings
BEGIN SELECT RAISE(ABORT, 'market artifact bindings are immutable'); END;
CREATE TRIGGER market_bindings_no_delete BEFORE DELETE ON market_artifact_bindings
BEGIN SELECT RAISE(ABORT, 'market artifact bindings are immutable'); END;
CREATE TRIGGER market_quarantines_no_update BEFORE UPDATE ON market_artifact_quarantines
BEGIN SELECT RAISE(ABORT, 'market quarantine events are append-only'); END;
CREATE TRIGGER market_quarantines_no_delete BEFORE DELETE ON market_artifact_quarantines
BEGIN SELECT RAISE(ABORT, 'market quarantine events are append-only'); END;
CREATE TRIGGER market_previews_no_update BEFORE UPDATE ON market_collection_previews
BEGIN SELECT RAISE(ABORT, 'market collection previews are immutable'); END;
CREATE TRIGGER market_previews_no_delete BEFORE DELETE ON market_collection_previews
BEGIN SELECT RAISE(ABORT, 'market collection previews are immutable'); END;
CREATE TRIGGER market_confirmations_no_update BEFORE UPDATE ON market_collection_confirmations
BEGIN SELECT RAISE(ABORT, 'market collection confirmations are immutable'); END;
CREATE TRIGGER market_confirmations_no_delete BEFORE DELETE ON market_collection_confirmations
BEGIN SELECT RAISE(ABORT, 'market collection confirmations are immutable'); END;
CREATE TRIGGER market_jobs_no_delete BEFORE DELETE ON market_collection_jobs
BEGIN SELECT RAISE(ABORT, 'market collection jobs cannot be deleted'); END;
CREATE TRIGGER market_resolutions_no_update BEFORE UPDATE ON market_send_unknown_resolutions
BEGIN SELECT RAISE(ABORT, 'market unknown-send resolutions are immutable'); END;
CREATE TRIGGER market_resolutions_no_delete BEFORE DELETE ON market_send_unknown_resolutions
BEGIN SELECT RAISE(ABORT, 'market unknown-send resolutions are immutable'); END;
CREATE TRIGGER market_confirmation_jobs_no_update BEFORE UPDATE ON market_confirmation_jobs
BEGIN SELECT RAISE(ABORT, 'market confirmation-job associations are immutable'); END;
CREATE TRIGGER market_confirmation_jobs_no_delete BEFORE DELETE ON market_confirmation_jobs
BEGIN SELECT RAISE(ABORT, 'market confirmation-job associations are immutable'); END;
CREATE TRIGGER market_occurrence_runs_no_update BEFORE UPDATE ON market_occurrence_runs
BEGIN SELECT RAISE(ABORT, 'market occurrence runs are immutable'); END;
CREATE TRIGGER market_occurrence_runs_no_delete BEFORE DELETE ON market_occurrence_runs
BEGIN SELECT RAISE(ABORT, 'market occurrence runs are immutable'); END;
CREATE TRIGGER market_occurrences_no_update BEFORE UPDATE ON market_literal_occurrences
BEGIN SELECT RAISE(ABORT, 'market literal occurrences are immutable'); END;
CREATE TRIGGER market_occurrences_no_delete BEFORE DELETE ON market_literal_occurrences
BEGIN SELECT RAISE(ABORT, 'market literal occurrences are immutable'); END;
CREATE TRIGGER market_classification_runs_no_update BEFORE UPDATE ON market_classification_runs
BEGIN SELECT RAISE(ABORT, 'market classification runs are immutable'); END;
CREATE TRIGGER market_classification_runs_no_delete BEFORE DELETE ON market_classification_runs
BEGIN SELECT RAISE(ABORT, 'market classification runs are immutable'); END;
CREATE TRIGGER market_classification_results_no_update BEFORE UPDATE ON market_classification_results
BEGIN SELECT RAISE(ABORT, 'market classification results are immutable'); END;
CREATE TRIGGER market_classification_results_no_delete BEFORE DELETE ON market_classification_results
BEGIN SELECT RAISE(ABORT, 'market classification results are immutable'); END;
CREATE TRIGGER market_projection_runs_no_update BEFORE UPDATE ON market_projection_runs
BEGIN SELECT RAISE(ABORT, 'market projection runs are immutable'); END;
CREATE TRIGGER market_projection_runs_no_delete BEFORE DELETE ON market_projection_runs
BEGIN SELECT RAISE(ABORT, 'market projection runs are immutable'); END;
CREATE TRIGGER market_projection_matches_no_update BEFORE UPDATE ON market_projection_matches
BEGIN SELECT RAISE(ABORT, 'market projection matches are immutable'); END;
CREATE TRIGGER market_projection_matches_no_delete BEFORE DELETE ON market_projection_matches
BEGIN SELECT RAISE(ABORT, 'market projection matches are immutable'); END;
