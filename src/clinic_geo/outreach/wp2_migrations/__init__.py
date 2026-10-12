"""Explicit, non-auto-discovered WP2 preview migrations.

These migrations are intentionally outside ``outreach/migrations`` while a
long-running process still has the legacy dynamic migration loader. Callers
must apply them explicitly to a temporary database.
"""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path
import sqlite3
import stat
import tempfile


WP2_MIGRATION_007 = "007_treatment_catalog_releases.sql"
WP2_MIGRATION_008 = "008_query_geographies_and_campaigns.sql"
WP2_MIGRATION_009 = "009_market_evidence_ledger_and_jobs.sql"
WP2_MIGRATION_010 = "010_processing_projection_treatment_review.sql"
WP2_MIGRATION_011 = "011_campaign_runtime.sql"
WP2_MIGRATION_012 = "012_market_raw_response_segments.sql"
WP2_MIGRATION_009_SHA256 = (
    "6a972708693b450983db6fa99e04b407551a298d9d439dc4dc32022af9932dec"
)
WP2_MIGRATION_010_SHA256 = (
    "85c471022a61021e431c3951aea6ab9d7f1bca27567fa1aef8fde4d6b3f603f4"
)
WP2_MIGRATION_011_SHA256 = (
    "9c16fcde128bd895949b652539e7fcc455d733c1e9b10a176670f51b776c855a"
)
WP2_MIGRATION_012_SHA256 = (
    "ce4443488d0e096549cad3f4485a872b5e2135a0bf656908237bfc9ccbf62d0e"
)


class TemporaryDatabaseRequired(RuntimeError):
    """Raised when gated WP2 schema is aimed outside the declared temp root."""


class WP2MigrationConflict(RuntimeError):
    """Raised when an applied preview migration no longer matches its SQL."""


def _validated_temporary_database_path(
    connection: sqlite3.Connection,
    temporary_root: Path | str,
) -> None:
    database_rows = connection.execute("PRAGMA database_list").fetchall()
    main_rows = [row for row in database_rows if row[1] == "main"]
    if len(main_rows) != 1 or not main_rows[0][2]:
        raise TemporaryDatabaseRequired(
            "WP2 migrations require a file-backed temporary database"
        )
    database_path = Path(main_rows[0][2]).resolve()
    try:
        database_metadata = database_path.stat()
    except OSError as exc:
        raise TemporaryDatabaseRequired(
            "WP2 migrations require an inspectable regular database file"
        ) from exc
    if not stat.S_ISREG(database_metadata.st_mode):
        raise TemporaryDatabaseRequired(
            "WP2 migrations require a regular database file"
        )
    if database_metadata.st_nlink != 1:
        raise TemporaryDatabaseRequired(
            "WP2 migrations reject hard-linked database aliases"
        )
    allowed_root = Path(temporary_root).resolve()
    system_temporary_root = Path(tempfile.gettempdir()).resolve()
    if not (
        allowed_root == system_temporary_root
        or allowed_root.is_relative_to(system_temporary_root)
    ):
        raise TemporaryDatabaseRequired(
            "WP2 migrations require a root inside the system temporary directory"
        )
    if database_path == allowed_root or not database_path.is_relative_to(allowed_root):
        raise TemporaryDatabaseRequired(
            "WP2 migration target is outside the declared temporary root"
        )


def _apply_explicit_migration(
    connection: sqlite3.Connection,
    migration_name: str,
    *,
    temporary_root: Path | str,
) -> bool:
    _validated_temporary_database_path(connection, temporary_root)
    if connection.in_transaction:
        raise TemporaryDatabaseRequired(
            "WP2 migrations require a connection with no open transaction"
        )
    sql = (
        resources.files("clinic_geo.outreach.wp2_migrations")
        .joinpath(migration_name)
        .read_text(encoding="utf-8")
    )
    sql_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    pinned_hashes = {
        WP2_MIGRATION_009: WP2_MIGRATION_009_SHA256,
        WP2_MIGRATION_010: WP2_MIGRATION_010_SHA256,
        WP2_MIGRATION_011: WP2_MIGRATION_011_SHA256,
        WP2_MIGRATION_012: WP2_MIGRATION_012_SHA256,
    }
    if migration_name in pinned_hashes and sql_sha256 != pinned_hashes[migration_name]:
        raise WP2MigrationConflict(
            f"WP2 migration {migration_name} no longer matches its pinned loader hash"
        )
    marker_table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='wp2_preview_schema_migrations'
        """
    ).fetchone()
    if marker_table_exists:
        applied = connection.execute(
            """
            SELECT sql_sha256 FROM wp2_preview_schema_migrations
            WHERE version=?
            """,
            (migration_name,),
        ).fetchone()
        if applied is not None:
            if applied[0] != sql_sha256:
                raise WP2MigrationConflict(
                    f"WP2 preview migration hash changed after apply: {migration_name}"
                )
            return False

    try:
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            "CREATE TABLE IF NOT EXISTS wp2_preview_schema_migrations (\n"
            "  version TEXT PRIMARY KEY,\n"
            "  sql_sha256 TEXT NOT NULL CHECK (length(sql_sha256) = 64),\n"
            "  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
            ");\n"
            f"{sql}\n"
        )
        connection.execute(
            """
            INSERT INTO wp2_preview_schema_migrations (version, sql_sha256)
            VALUES (?, ?)
            """,
            (migration_name, sql_sha256),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    return True


def apply_wp2_migration_007(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> bool:
    """Apply treatment migration 007 to a declared temporary database."""

    return _apply_explicit_migration(
        connection,
        WP2_MIGRATION_007,
        temporary_root=temporary_root,
    )


def apply_wp2_migration_008(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> bool:
    """Apply geography/campaign migration 008 to a declared temporary database."""

    return _apply_explicit_migration(
        connection,
        WP2_MIGRATION_008,
        temporary_root=temporary_root,
    )


def apply_market_evidence_migration_009(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> bool:
    """Apply the gated Market Evidence ledger migration to a temp database."""

    _validated_temporary_database_path(connection, temporary_root)
    marker_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='wp2_preview_schema_migrations'
        """
    ).fetchone()
    if marker_exists is None:
        raise WP2MigrationConflict(
            "Market Evidence migration 009 requires explicit migrations 007 and 008"
        )
    for predecessor in (WP2_MIGRATION_007, WP2_MIGRATION_008):
        source = (
            resources.files("clinic_geo.outreach.wp2_migrations")
            .joinpath(predecessor)
            .read_text(encoding="utf-8")
        )
        expected_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        applied = connection.execute(
            """
            SELECT sql_sha256 FROM wp2_preview_schema_migrations
            WHERE version=?
            """,
            (predecessor,),
        ).fetchone()
        if applied is None or applied[0] != expected_sha256:
            raise WP2MigrationConflict(
                f"Market Evidence migration 009 requires exact predecessor: {predecessor}"
            )
    predecessor_order = connection.execute(
        """
        SELECT version,rowid FROM wp2_preview_schema_migrations
        WHERE version IN (?,?) ORDER BY rowid
        """,
        (WP2_MIGRATION_007, WP2_MIGRATION_008),
    ).fetchall()
    if [row[0] for row in predecessor_order] != [
        WP2_MIGRATION_007,
        WP2_MIGRATION_008,
    ]:
        raise WP2MigrationConflict(
            "Market Evidence migration 009 requires predecessor order 007 then 008"
        )

    return _apply_explicit_migration(
        connection,
        WP2_MIGRATION_009,
        temporary_root=temporary_root,
    )


# The numbered spelling is kept beside the descriptive public API so callers
# that sequence explicit preview migrations do not need a special case.
apply_wp2_migration_009 = apply_market_evidence_migration_009


def apply_wp2_migrations(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> tuple[bool, bool]:
    """Apply the complete WP2 preview schema, in order, to a temp database."""

    return (
        apply_wp2_migration_007(connection, temporary_root=temporary_root),
        apply_wp2_migration_008(connection, temporary_root=temporary_root),
    )


def apply_market_evidence_migrations(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> tuple[bool, bool, bool]:
    """Apply temporary migrations 007 through 009 in their approved order.

    ``apply_wp2_migrations`` intentionally retains its historical two-result
    contract.  P1 callers opt into this separate API, so adding migration 009
    cannot silently alter existing 007/008 behavior.
    """

    first, second = apply_wp2_migrations(
        connection,
        temporary_root=temporary_root,
    )
    third = apply_market_evidence_migration_009(
        connection,
        temporary_root=temporary_root,
    )
    return first, second, third


apply_p1_migrations = apply_market_evidence_migrations


def apply_p2_processing_migration_010(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> bool:
    """Apply P2 processing and treatment-review schema after exact 009."""

    _validated_temporary_database_path(connection, temporary_root)
    marker_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='wp2_preview_schema_migrations'
        """
    ).fetchone()
    if marker_exists is None:
        raise WP2MigrationConflict("P2 migration 010 requires migrations 007 through 009")
    expected_order = (WP2_MIGRATION_007, WP2_MIGRATION_008, WP2_MIGRATION_009)
    applied_rows = connection.execute(
        """
        SELECT version,sql_sha256 FROM wp2_preview_schema_migrations
        WHERE version IN (?,?,?) ORDER BY rowid
        """,
        expected_order,
    ).fetchall()
    if tuple(row[0] for row in applied_rows) != expected_order:
        raise WP2MigrationConflict("P2 migration 010 requires exact order 007, 008, 009")
    for migration_name, applied_sha256 in applied_rows:
        source = (
            resources.files("clinic_geo.outreach.wp2_migrations")
            .joinpath(migration_name)
            .read_text(encoding="utf-8")
        )
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != applied_sha256:
            raise WP2MigrationConflict(
                f"P2 migration 010 requires exact predecessor: {migration_name}"
            )
    return _apply_explicit_migration(
        connection,
        WP2_MIGRATION_010,
        temporary_root=temporary_root,
    )


apply_wp2_migration_010 = apply_p2_processing_migration_010


def apply_p2_migrations(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> tuple[bool, bool, bool, bool]:
    """Apply the complete temporary P2 sequence without changing P1 APIs."""

    first, second, third = apply_market_evidence_migrations(
        connection,
        temporary_root=temporary_root,
    )
    fourth = apply_p2_processing_migration_010(
        connection,
        temporary_root=temporary_root,
    )
    return first, second, third, fourth


def apply_p3_campaign_runtime_migration_011(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> bool:
    """Apply the additive P3 campaign-runtime schema after exact 007-010."""

    _validated_temporary_database_path(connection, temporary_root)
    marker_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='wp2_preview_schema_migrations'
        """
    ).fetchone()
    if marker_exists is None:
        raise WP2MigrationConflict("P3 migration 011 requires migrations 007 through 010")
    expected_order = (
        WP2_MIGRATION_007,
        WP2_MIGRATION_008,
        WP2_MIGRATION_009,
        WP2_MIGRATION_010,
    )
    applied_rows = connection.execute(
        """
        SELECT version,sql_sha256 FROM wp2_preview_schema_migrations
        WHERE version IN (?,?,?,?) ORDER BY rowid
        """,
        expected_order,
    ).fetchall()
    if tuple(row[0] for row in applied_rows) != expected_order:
        raise WP2MigrationConflict(
            "P3 migration 011 requires exact order 007, 008, 009, 010"
        )
    for migration_name, applied_sha256 in applied_rows:
        source = (
            resources.files("clinic_geo.outreach.wp2_migrations")
            .joinpath(migration_name)
            .read_text(encoding="utf-8")
        )
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != applied_sha256:
            raise WP2MigrationConflict(
                f"P3 migration 011 requires exact predecessor: {migration_name}"
            )
    return _apply_explicit_migration(
        connection,
        WP2_MIGRATION_011,
        temporary_root=temporary_root,
    )


apply_wp2_migration_011 = apply_p3_campaign_runtime_migration_011


def apply_p3_migrations(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> tuple[bool, bool, bool, bool, bool]:
    """Apply the complete temporary P1+P2+P3 sequence without changing prior APIs."""

    first, second, third, fourth = apply_p2_migrations(
        connection,
        temporary_root=temporary_root,
    )
    fifth = apply_p3_campaign_runtime_migration_011(
        connection,
        temporary_root=temporary_root,
    )
    return first, second, third, fourth, fifth


def apply_p5_segment_classification_migration_012(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> bool:
    """Apply the additive segment-classification schema after exact 007-011."""

    _validated_temporary_database_path(connection, temporary_root)
    marker_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='wp2_preview_schema_migrations'
        """
    ).fetchone()
    if marker_exists is None:
        raise WP2MigrationConflict(
            "P5 segment migration 012 requires migrations 007 through 011"
        )
    expected_order = (
        WP2_MIGRATION_007,
        WP2_MIGRATION_008,
        WP2_MIGRATION_009,
        WP2_MIGRATION_010,
        WP2_MIGRATION_011,
    )
    applied_rows = connection.execute(
        """
        SELECT version,sql_sha256 FROM wp2_preview_schema_migrations
        WHERE version IN (?,?,?,?,?) ORDER BY rowid
        """,
        expected_order,
    ).fetchall()
    if tuple(row[0] for row in applied_rows) != expected_order:
        raise WP2MigrationConflict(
            "P5 segment migration 012 requires exact order "
            "007, 008, 009, 010, 011"
        )
    for migration_name, applied_sha256 in applied_rows:
        source = (
            resources.files("clinic_geo.outreach.wp2_migrations")
            .joinpath(migration_name)
            .read_text(encoding="utf-8")
        )
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != applied_sha256:
            raise WP2MigrationConflict(
                "P5 segment migration 012 requires exact predecessor: "
                f"{migration_name}"
            )
    return _apply_explicit_migration(
        connection,
        WP2_MIGRATION_012,
        temporary_root=temporary_root,
    )


apply_wp2_migration_012 = apply_p5_segment_classification_migration_012


def apply_p5_segment_migrations(
    connection: sqlite3.Connection,
    *,
    temporary_root: Path | str,
) -> tuple[bool, bool, bool, bool, bool, bool]:
    """Apply the complete temporary sequence through segment schema 012."""

    first, second, third, fourth, fifth = apply_p3_migrations(
        connection,
        temporary_root=temporary_root,
    )
    sixth = apply_p5_segment_classification_migration_012(
        connection,
        temporary_root=temporary_root,
    )
    return first, second, third, fourth, fifth, sixth


__all__ = [
    "TemporaryDatabaseRequired",
    "WP2MigrationConflict",
    "WP2_MIGRATION_007",
    "WP2_MIGRATION_008",
    "WP2_MIGRATION_009",
    "WP2_MIGRATION_009_SHA256",
    "WP2_MIGRATION_010",
    "WP2_MIGRATION_010_SHA256",
    "WP2_MIGRATION_011",
    "WP2_MIGRATION_011_SHA256",
    "WP2_MIGRATION_012",
    "WP2_MIGRATION_012_SHA256",
    "apply_market_evidence_migration_009",
    "apply_market_evidence_migrations",
    "apply_p1_migrations",
    "apply_p2_migrations",
    "apply_p2_processing_migration_010",
    "apply_p3_campaign_runtime_migration_011",
    "apply_p3_migrations",
    "apply_p5_segment_classification_migration_012",
    "apply_p5_segment_migrations",
    "apply_wp2_migration_007",
    "apply_wp2_migration_008",
    "apply_wp2_migration_009",
    "apply_wp2_migration_010",
    "apply_wp2_migration_011",
    "apply_wp2_migration_012",
    "apply_wp2_migrations",
]
