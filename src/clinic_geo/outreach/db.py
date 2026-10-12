"""SQLite storage for the mutable founder outreach workspace.

The research database remains immutable.  This module creates a separate local
database and applies forward-only SQL migrations to it.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from importlib import resources
import os
from pathlib import Path
import fcntl
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Iterator


DEFAULT_RELATIVE_DB = Path("data/outreach/outreach.sqlite3")
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class OutreachError(RuntimeError):
    """Base error for the outreach workspace."""


class NotFoundError(OutreachError):
    """Raised when a requested outreach entity does not exist."""


class ConflictError(OutreachError):
    """Raised when an optimistic update or duplicate action conflicts."""


class IntegrityError(OutreachError):
    """Raised when an immutable research release fails verification."""


def default_db_path(root: Path | str) -> Path:
    return Path(root).resolve() / DEFAULT_RELATIVE_DB


def connect(path: Path | str) -> sqlite3.Connection:
    destination = Path(path)
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed or destination.parent.name == "outreach":
        os.chmod(destination.parent, PRIVATE_DIRECTORY_MODE)
    connection = sqlite3.connect(
        destination,
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 30000")
    _secure_database_files(destination)
    return connection


def _secure_database_files(destination: Path) -> None:
    """Keep the database and SQLite sidecars private regardless of umask."""
    for path in (
        destination,
        destination.with_name(f"{destination.name}-wal"),
        destination.with_name(f"{destination.name}-shm"),
    ):
        if path.exists():
            os.chmod(path, PRIVATE_FILE_MODE)


def _backup_before_migration(
    connection: sqlite3.Connection,
    destination: Path,
    *,
    pending_version: str,
) -> Path:
    """Create and validate a consistent SQLite backup before schema changes."""
    backup_dir = destination.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, PRIVATE_DIRECTORY_MODE)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final_path = backup_dir / f"{destination.stem}-{stamp}-before-{Path(pending_version).stem}.sqlite3"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=backup_dir,
        prefix=f".{final_path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary_path)) as backup_connection:
            connection.backup(backup_connection)
            check = backup_connection.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise IntegrityError(f"pre-migration backup failed integrity check: {check}")
        os.chmod(temporary_path, PRIVATE_FILE_MODE)
        os.replace(temporary_path, final_path)
        os.chmod(final_path, PRIVATE_FILE_MODE)
        return final_path
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _migration_resources() -> list[tuple[str, str]]:
    package = resources.files("clinic_geo.outreach.migrations")
    migrations: list[tuple[str, str]] = []
    for entry in package.iterdir():
        if entry.name.endswith(".sql") and entry.name[:3].isdigit():
            migrations.append((entry.name, entry.read_text(encoding="utf-8")))
    return sorted(migrations)


def initialize_database(path: Path | str) -> Path:
    destination = Path(path)
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed or destination.parent.name == "outreach":
        os.chmod(destination.parent, PRIVATE_DIRECTORY_MODE)
    existed_with_data = destination.is_file() and destination.stat().st_size > 0
    lock_path = destination.with_name(f".{destination.name}.migrate.lock")
    # First API requests can arrive concurrently.  A filesystem advisory lock
    # serializes migration discovery + application across threads/processes;
    # each version is re-read only after the lock is held.
    with lock_path.open("a+b") as lock_handle:
        os.chmod(lock_path, PRIVATE_FILE_MODE)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            with closing(connect(destination)) as connection:
                migration_names = [version for version, _ in _migration_resources()]
                has_migration_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                applied = {
                    row[0]
                    for row in connection.execute("SELECT version FROM schema_migrations")
                } if has_migration_table else set()
                pending = [version for version in migration_names if version not in applied]
                if existed_with_data and pending:
                    _backup_before_migration(
                        connection,
                        destination,
                        pending_version=pending[-1],
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                for version, script in _migration_resources():
                    already_applied = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                    ).fetchone()
                    if already_applied:
                        continue
                    # executescript controls its own transaction boundary, so
                    # include the marker in the same transaction as the DDL.
                    escaped = version.replace("'", "''")
                    connection.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\n"
                        f"INSERT INTO schema_migrations(version) VALUES ('{escaped}');\nCOMMIT;"
                    )
                _secure_database_files(destination)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return destination


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        for row in connection.execute("PRAGMA database_list"):
            if row[2]:
                _secure_database_files(Path(row[2]))
        raise
    else:
        connection.commit()
        for row in connection.execute("PRAGMA database_list"):
            if row[2]:
                _secure_database_files(Path(row[2]))


def table_names(path: Path | str) -> set[str]:
    with closing(connect(path)) as connection:
        return {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
