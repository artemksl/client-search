CREATE TABLE client_search_workspaces (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, country_code TEXT NOT NULL,
 country_name TEXT NOT NULL, language TEXT NOT NULL, timezone TEXT NOT NULL,
 directory_revision TEXT, config_revision TEXT, version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(id,directory_revision) REFERENCES client_search_directory_revisions(workspace_id,id),
 FOREIGN KEY(id,config_revision) REFERENCES client_search_config_revisions(workspace_id,id)
);
CREATE TABLE client_search_directory_revisions (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES client_search_workspaces(id),
 file_name TEXT NOT NULL, raw_csv BLOB NOT NULL, raw_hash TEXT NOT NULL,
 normalized_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(workspace_id,id)
);
CREATE TABLE client_search_accounts (
 workspace_id TEXT NOT NULL REFERENCES client_search_workspaces(id), supplied_id TEXT NOT NULL,
 internal_account_id TEXT NOT NULL REFERENCES sales_accounts(id), data_json TEXT NOT NULL,
 active INTEGER NOT NULL CHECK(active IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(workspace_id,supplied_id), UNIQUE(workspace_id,internal_account_id)
);
CREATE TABLE client_search_branches (
 workspace_id TEXT NOT NULL, supplied_id TEXT NOT NULL, account_id TEXT NOT NULL,
 data_json TEXT NOT NULL, identity_fingerprint TEXT NOT NULL, matching_fingerprint TEXT NOT NULL,
 active INTEGER NOT NULL CHECK(active IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(workspace_id,supplied_id), UNIQUE(workspace_id,supplied_id,account_id),
 FOREIGN KEY(workspace_id,account_id) REFERENCES client_search_accounts(workspace_id,supplied_id)
);
CREATE TABLE client_search_config_revisions (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES client_search_workspaces(id),
 config_json TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(workspace_id,id)
);
CREATE TABLE client_search_previews (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES client_search_workspaces(id),
 kind TEXT NOT NULL CHECK(kind IN ('import','checks')), version INTEGER NOT NULL,
 payload_json TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, consumed_at TEXT
);
CREATE TABLE client_search_rounds (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES client_search_workspaces(id),
 kind TEXT NOT NULL CHECK(kind IN ('initial','identification','recommendations')),
 state TEXT NOT NULL CHECK(state IN ('queued','running','waiting_attention','succeeded','partial','failed','cancelled','skipped')),
 request_id TEXT NOT NULL UNIQUE, schedule_revision TEXT, slot INTEGER,
 manifest_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(schedule_revision,slot), UNIQUE(workspace_id,id)
);
CREATE UNIQUE INDEX client_search_one_active_round ON client_search_rounds(workspace_id)
 WHERE state IN ('queued','running','waiting_attention');
CREATE TABLE client_search_run_links (
 round_id TEXT NOT NULL REFERENCES client_search_rounds(id), kind TEXT NOT NULL,
 runner_run_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE, job_id TEXT,
 cursor TEXT NOT NULL CHECK(cursor IN ('reserved','dispatch_requested','attached','terminal')),
 manifest_json TEXT NOT NULL, manifest_hash TEXT NOT NULL, result_json TEXT, error TEXT,
 PRIMARY KEY(round_id,kind)
);
CREATE TABLE client_search_evidence (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, round_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('identification','recommendations')),
 runner_result_id TEXT NOT NULL UNIQUE, runner_run_id TEXT NOT NULL,
 branch_id TEXT, cell_id TEXT, provider TEXT NOT NULL, profile_id TEXT NOT NULL,
 collected_at TEXT NOT NULL, data_json TEXT NOT NULL,
 FOREIGN KEY(workspace_id,round_id) REFERENCES client_search_rounds(workspace_id,id),
 FOREIGN KEY(workspace_id,branch_id) REFERENCES client_search_branches(workspace_id,supplied_id)
);
CREATE INDEX client_search_evidence_round ON client_search_evidence(workspace_id,round_id);
CREATE TABLE client_search_schedules (
 workspace_id TEXT PRIMARY KEY REFERENCES client_search_workspaces(id), revision TEXT NOT NULL UNIQUE,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), anchor TEXT NOT NULL, next_due_at TEXT NOT NULL,
 interval_hours INTEGER NOT NULL DEFAULT 72 CHECK(interval_hours=72), grant_json TEXT NOT NULL,
 execution_fingerprint TEXT NOT NULL, pause_reason TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE client_search_interaction_contexts (
 interaction_id TEXT PRIMARY KEY REFERENCES interactions(id), workspace_id TEXT NOT NULL,
 account_id TEXT NOT NULL, branch_id TEXT, directory_revision TEXT NOT NULL,
 reason_json TEXT NOT NULL,
 FOREIGN KEY(workspace_id,account_id) REFERENCES client_search_accounts(workspace_id,supplied_id),
 FOREIGN KEY(workspace_id,branch_id,account_id) REFERENCES client_search_branches(workspace_id,supplied_id,account_id),
 FOREIGN KEY(workspace_id,directory_revision) REFERENCES client_search_directory_revisions(workspace_id,id)
);
CREATE TABLE client_search_request_receipts (
 scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, request_id TEXT NOT NULL,
 payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope_kind,scope_id,request_id)
);
CREATE TABLE client_search_review_annotations (
 id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES client_search_evidence(id),
 reason TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER client_search_directory_immutable_update BEFORE UPDATE ON client_search_directory_revisions
 BEGIN SELECT RAISE(ABORT,'client directory revisions are immutable'); END;
CREATE TRIGGER client_search_round_manifest_immutable BEFORE UPDATE OF workspace_id,kind,request_id,schedule_revision,slot,manifest_json,created_at ON client_search_rounds
 BEGIN SELECT RAISE(ABORT,'client research round manifests are immutable'); END;
CREATE TRIGGER client_search_run_manifest_immutable BEFORE UPDATE OF round_id,kind,runner_run_id,request_id,manifest_json,manifest_hash ON client_search_run_links
 BEGIN SELECT RAISE(ABORT,'client research run manifests are immutable'); END;
CREATE TRIGGER client_search_schedule_grant_immutable BEFORE UPDATE OF grant_json,execution_fingerprint,anchor ON client_search_schedules
 WHEN NEW.revision=OLD.revision AND (NEW.grant_json!=OLD.grant_json OR NEW.execution_fingerprint!=OLD.execution_fingerprint OR NEW.anchor!=OLD.anchor)
 BEGIN SELECT RAISE(ABORT,'client research grants require a new revision'); END;
CREATE TRIGGER client_search_directory_immutable_delete BEFORE DELETE ON client_search_directory_revisions
 BEGIN SELECT RAISE(ABORT,'client directory revisions are immutable'); END;
CREATE TRIGGER client_search_config_immutable_update BEFORE UPDATE ON client_search_config_revisions
 BEGIN SELECT RAISE(ABORT,'client configuration revisions are immutable'); END;
CREATE TRIGGER client_search_config_immutable_delete BEFORE DELETE ON client_search_config_revisions
 BEGIN SELECT RAISE(ABORT,'client configuration revisions are immutable'); END;
CREATE TRIGGER client_search_evidence_immutable_update BEFORE UPDATE ON client_search_evidence
 BEGIN SELECT RAISE(ABORT,'client evidence is immutable'); END;
CREATE TRIGGER client_search_evidence_immutable_delete BEFORE DELETE ON client_search_evidence
 BEGIN SELECT RAISE(ABORT,'client evidence is immutable'); END;
CREATE TRIGGER client_search_review_immutable_update BEFORE UPDATE ON client_search_review_annotations
 BEGIN SELECT RAISE(ABORT,'client evidence review annotations are immutable'); END;
CREATE TRIGGER client_search_review_immutable_delete BEFORE DELETE ON client_search_review_annotations
 BEGIN SELECT RAISE(ABORT,'client evidence review annotations are immutable'); END;
