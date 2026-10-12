"""Public application facade for the founder outreach bounded context.

The web control plane and other application lanes use this module instead of
reaching into outreach persistence, release-import, migration, or branch
derivation internals.  Outreach continues to own its mutable SQLite state and
the verification rules for immutable research releases.
"""

from __future__ import annotations

import os as _os
import sqlite3 as _sqlite3
import stat as _stat
import tempfile as _tempfile
from pathlib import Path
from typing import Mapping

from .branching import location_key as _derive_branch_location_key
from .db import (
    ConflictError,
    IntegrityError,
    NotFoundError,
    OutreachError,
    default_db_path,
    initialize_database as _initialize_database,
)
from .importer import VerifiedRelease, verify_current_release
from .service import OutreachService
from .client_research_service import ClientResearchService, ROUTINE_PROFILE_IDS
from .client_research_assessment import (
    build_matching_context, direct_branch_context, project_client_research,
)
from .scope_options import load_paid_audit_scope_options
from .snapshot_catalog import (
    SNAPSHOT_PROCEDURES_RELATIVE_PATH,
    SnapshotCatalogError,
    build_snapshot_selection_catalog,
)
from .supplied_directory import (
    SUPPLIED_DIRECTORY_OPTIONAL_COLUMNS,
    SUPPLIED_DIRECTORY_REQUIRED_COLUMNS,
    SUPPLIED_DIRECTORY_SCHEMA_VERSION,
    SuppliedClinicDirectory,
    SuppliedDirectoryAccount,
    SuppliedDirectoryBranch,
    SuppliedDirectoryIssue,
    SuppliedDirectoryValidationError,
    load_supplied_clinic_directory,
    parse_supplied_clinic_directory,
)
from .visibility_evidence import (
    OUTREACH_EVIDENCE_PURPOSE,
    EXPECTED_PROMPT_IDS,
    RUN_IDS,
    TEMPLATE_PROMPT_IDS,
    VISIBILITY_CONTRACT_ID,
    VisibilityEvidenceError,
    build_prompt_basket,
    create_visibility_check,
    default_artifacts_root,
    finalize_visibility_check,
    prompt_ids_for_run,
    record_response,
    validate_visibility_check,
    validate_profile_proof,
    write_logged_out_profile_config,
)
from .wp2_migrations import (
    TemporaryDatabaseRequired,
    apply_p3_migrations as _apply_p3_migrations,
)


def _validated_campaign_preview_path(
    path: Path | str,
    *,
    temporary_root: Path | str,
) -> tuple[Path, Path]:
    """Resolve a preview target without creating or following an alias."""

    root_input = Path(temporary_root).expanduser()
    root_lexical = Path(_os.path.abspath(_os.fspath(root_input)))
    if root_lexical.is_symlink():
        raise TemporaryDatabaseRequired(
            "campaign preview root cannot be a symbolic link"
        )
    try:
        root = root_lexical.resolve(strict=True)
    except OSError as exc:
        raise TemporaryDatabaseRequired(
            "campaign preview root must already exist"
        ) from exc
    if not root.is_dir():
        raise TemporaryDatabaseRequired(
            "campaign preview root must be a directory"
        )

    system_temporary_root = Path(_tempfile.gettempdir()).resolve()
    if not (
        root == system_temporary_root
        or root.is_relative_to(system_temporary_root)
    ):
        raise TemporaryDatabaseRequired(
            "campaign preview root must be inside the system temporary directory"
        )

    candidate_input = Path(path).expanduser()
    candidate_lexical = Path(_os.path.abspath(_os.fspath(candidate_input)))
    try:
        candidate_metadata = candidate_lexical.lstat()
    except FileNotFoundError:
        candidate_metadata = None
    except OSError as exc:
        raise TemporaryDatabaseRequired(
            "campaign preview database must be inspectable before preparation"
        ) from exc
    if candidate_metadata is not None:
        if _stat.S_ISLNK(candidate_metadata.st_mode):
            raise TemporaryDatabaseRequired(
                "campaign preview database cannot be a symbolic link"
            )
        if not _stat.S_ISREG(candidate_metadata.st_mode):
            raise TemporaryDatabaseRequired(
                "campaign preview database must be a regular file"
            )
        if candidate_metadata.st_nlink != 1:
            raise TemporaryDatabaseRequired(
                "campaign preview database cannot be a hard-linked alias"
            )
    try:
        lexical_relative = candidate_lexical.relative_to(root_lexical)
    except ValueError as exc:
        raise TemporaryDatabaseRequired(
            "campaign preview database is outside the declared temporary root"
        ) from exc
    if not lexical_relative.parts:
        raise TemporaryDatabaseRequired(
            "campaign preview database is outside the declared temporary root"
        )

    candidate = candidate_lexical.resolve(strict=False)
    if candidate == root or not candidate.is_relative_to(root):
        raise TemporaryDatabaseRequired(
            "campaign preview database is outside the declared temporary root"
        )

    # A symlinked parent can still resolve beneath ``root``. Reject it rather
    # than permitting aliases with surprising ownership or cleanup semantics.
    probe = root_lexical
    for part in lexical_relative.parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            raise TemporaryDatabaseRequired(
                "campaign preview database cannot use a symbolic-link parent"
            )
    if candidate_metadata is not None:
        try:
            resolved_metadata = candidate.stat()
        except OSError as exc:
            raise TemporaryDatabaseRequired(
                "campaign preview database changed during validation"
            ) from exc
        if (
            resolved_metadata.st_dev,
            resolved_metadata.st_ino,
        ) != (
            candidate_metadata.st_dev,
            candidate_metadata.st_ino,
        ):
            raise TemporaryDatabaseRequired(
                "campaign preview database changed during validation"
            )
    return candidate, root


def prepare_campaign_preview_database(
    path: Path | str,
    *,
    temporary_root: Path | str,
) -> Path:
    """Prepare an explicitly temporary campaign preview database.

    Outreach retains ownership of its base schema and ordered preview
    migrations. The caller receives only the canonical database path, not a
    shared writable connection; request-scoped adapters must open and close
    their own thread-confined sessions. Production outreach storage is
    deliberately unsupported.
    """

    database_path, root = _validated_campaign_preview_path(
        path,
        temporary_root=temporary_root,
    )
    _initialize_database(database_path)
    connection = _sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = _sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _apply_p3_migrations(connection, temporary_root=root)
    finally:
        connection.close()
    return database_path


def branch_location_key(
    member_uid: str,
    research_row: Mapping,
) -> tuple[str, bool]:
    """Return the outreach-owned stable branch-location projection."""

    return _derive_branch_location_key(member_uid, research_row)


__all__ = [
    "ClientResearchService",
    "ROUTINE_PROFILE_IDS",
    "build_matching_context",
    "direct_branch_context",
    "project_client_research",
    "ConflictError",
    "EXPECTED_PROMPT_IDS",
    "IntegrityError",
    "NotFoundError",
    "OUTREACH_EVIDENCE_PURPOSE",
    "OutreachError",
    "OutreachService",
    "RUN_IDS",
    "SNAPSHOT_PROCEDURES_RELATIVE_PATH",
    "SUPPLIED_DIRECTORY_OPTIONAL_COLUMNS",
    "SUPPLIED_DIRECTORY_REQUIRED_COLUMNS",
    "SUPPLIED_DIRECTORY_SCHEMA_VERSION",
    "TEMPLATE_PROMPT_IDS",
    "SnapshotCatalogError",
    "SuppliedClinicDirectory",
    "SuppliedDirectoryAccount",
    "SuppliedDirectoryBranch",
    "SuppliedDirectoryIssue",
    "SuppliedDirectoryValidationError",
    "TemporaryDatabaseRequired",
    "VISIBILITY_CONTRACT_ID",
    "VerifiedRelease",
    "VisibilityEvidenceError",
    "branch_location_key",
    "build_prompt_basket",
    "build_snapshot_selection_catalog",
    "create_visibility_check",
    "default_db_path",
    "default_artifacts_root",
    "finalize_visibility_check",
    "load_paid_audit_scope_options",
    "load_supplied_clinic_directory",
    "parse_supplied_clinic_directory",
    "prepare_campaign_preview_database",
    "prompt_ids_for_run",
    "record_response",
    "validate_visibility_check",
    "validate_profile_proof",
    "verify_current_release",
    "write_logged_out_profile_config",
]
