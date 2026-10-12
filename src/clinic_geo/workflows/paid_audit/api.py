"""Public Python facade for the standalone paid Diagnostic Audit application.

Consumers should import paid-audit capabilities from this module.  The
implementation remains split between scope construction, engine discovery, and
the durable CLI adapter, while callers see one deliberate application surface.
"""

from __future__ import annotations

from .engine import (
    CHROME_PATH_ENV_VARS,
    MIN_NODE_MAJOR,
    NODE_BIN_ENV_VARS,
    PAID_AUDIT_DATA_DIRNAME,
    PAID_AUDIT_ENGINE_DIRNAME,
    chrome_path_cache_file,
    discover_chrome_path,
    discover_node_bin,
    node_binary_satisfies,
    paid_audit_cli_path,
    paid_audit_data_dir,
    paid_audit_engine_dir,
    paid_audit_engine_env,
    required_node_major,
)
from .scope_builder import (
    ScopeError,
    build_engine_scope,
    build_scope,
    scope_identity,
    slugify,
    write_scope,
)
from .client_report import (
    CLIENT_REPORT_SCHEMA_VERSION,
    ClientReportError,
    build_document,
    compose_clause_wording,
    edit_clause,
    remove_clause,
    render_model,
    restore_clause,
    set_clause_not_checked,
)
from .store import (
    AI_ASSISTANCE_DISCLOSURE_ALIAS,
    AI_ASSISTANCE_DISCLOSURE_FILENAME,
    PRODUCTION_ARTIFACT_KINDS,
    PRODUCTION_REVIEW_ATTESTATION,
    OPERATOR_REPORT_ARTIFACT_KIND,
    REPORT_KINDS,
    PaidAuditConflictError,
    PaidAuditEngineUnavailableError,
    PaidAuditError,
    PaidAuditNotFoundError,
    PaidAuditStore,
    PaidAuditUnprocessableError,
)


__all__ = [
    "AI_ASSISTANCE_DISCLOSURE_ALIAS",
    "AI_ASSISTANCE_DISCLOSURE_FILENAME",
    "CHROME_PATH_ENV_VARS",
    "MIN_NODE_MAJOR",
    "NODE_BIN_ENV_VARS",
    "OPERATOR_REPORT_ARTIFACT_KIND",
    "PAID_AUDIT_DATA_DIRNAME",
    "PAID_AUDIT_ENGINE_DIRNAME",
    "PRODUCTION_ARTIFACT_KINDS",
    "PRODUCTION_REVIEW_ATTESTATION",
    "REPORT_KINDS",
    "PaidAuditConflictError",
    "PaidAuditEngineUnavailableError",
    "PaidAuditError",
    "PaidAuditNotFoundError",
    "PaidAuditStore",
    "PaidAuditUnprocessableError",
    "CLIENT_REPORT_SCHEMA_VERSION",
    "ClientReportError",
    "ScopeError",
    "build_engine_scope",
    "build_document",
    "build_scope",
    "chrome_path_cache_file",
    "discover_chrome_path",
    "discover_node_bin",
    "compose_clause_wording",
    "edit_clause",
    "node_binary_satisfies",
    "paid_audit_cli_path",
    "paid_audit_data_dir",
    "paid_audit_engine_dir",
    "paid_audit_engine_env",
    "required_node_major",
    "remove_clause",
    "render_model",
    "restore_clause",
    "set_clause_not_checked",
    "scope_identity",
    "slugify",
    "write_scope",
]
