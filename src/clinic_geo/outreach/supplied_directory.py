"""Read-only client-search directory contract with user-declared branch identity.

This boundary validates supplied relationships. It does not derive account or
branch IDs, merge businesses, verify websites, or import research releases.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import io
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit


SUPPLIED_DIRECTORY_SCHEMA_VERSION = "client_search.directory.v1"
SUPPLIED_DIRECTORY_REQUIRED_COLUMNS = (
    "account_id", "account_name", "branch_id", "branch_name", "country_code",
    "city", "area", "address", "website", "treatment_ids",
)
SUPPLIED_DIRECTORY_OPTIONAL_COLUMNS = ("account_aliases", "branch_aliases")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_COUNTRY_RE = re.compile(r"[A-Z]{2}\Z")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


@dataclass(frozen=True)
class SuppliedDirectoryIssue:
    line_number: int | None
    field: str
    message: str


class SuppliedDirectoryValidationError(ValueError):
    """A rejected file, with field and source-line diagnostics."""

    def __init__(self, issues: tuple[SuppliedDirectoryIssue, ...]):
        self.issues = issues
        super().__init__("; ".join(
            f"{('Line ' + str(issue.line_number) + ': ') if issue.line_number else ''}"
            f"{issue.field}: {issue.message}"
            for issue in issues
        ))


@dataclass(frozen=True)
class SuppliedDirectoryAccount:
    account_id: str
    account_name: str
    account_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuppliedDirectoryBranch:
    branch_id: str
    account_id: str
    branch_name: str
    country_code: str
    city: str
    area: str
    address: str
    website: str
    treatment_ids: tuple[str, ...]
    branch_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuppliedClinicDirectory:
    country_code: str
    city: str
    accounts: tuple[SuppliedDirectoryAccount, ...]
    branches: tuple[SuppliedDirectoryBranch, ...]
    duplicate_rows: int = 0
    schema_version: str = field(default=SUPPLIED_DIRECTORY_SCHEMA_VERSION, init=False)


def _list_value(
    value: str, column: str, line: int, issues: list[SuppliedDirectoryIssue],
) -> tuple[str, ...]:
    if not value:
        return ()
    items = tuple(item.strip() for item in value.split("|"))
    if any(not item for item in items):
        issues.append(SuppliedDirectoryIssue(line, column, "List entries cannot be empty"))
    if len(set(items)) != len(items):
        issues.append(SuppliedDirectoryIssue(line, column, "List entries must be unique"))
    if column == "treatment_ids" and any(not _ID_RE.fullmatch(item) for item in items):
        issues.append(SuppliedDirectoryIssue(line, column, "Treatment IDs must use the stable ID format"))
    return tuple(sorted(items))


def _valid_website(value: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        parts = urlsplit(value)
        # Accessing hostname/port also validates malformed bracket/port syntax.
        hostname = parts.hostname
        parts.port
        if (
            parts.scheme not in {"http", "https"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
        ):
            return False
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            ascii_host = hostname.encode("idna").decode("ascii").removesuffix(".")
            if len(ascii_host) > 253 or any(
                not _HOST_LABEL_RE.fullmatch(label) for label in ascii_host.split(".")
            ):
                return False
        return True
    except (ValueError, UnicodeError):
        return False


def parse_supplied_clinic_directory(csv_text: str) -> SuppliedClinicDirectory:
    """Validate one city-scoped CSV and group only by its explicit account IDs.

    Field-edge whitespace and list order are immaterial. Identical repeated
    branch rows collapse; conflicting IDs reject the whole file. Accepted output
    is ordered by stable IDs, independent of the order of rows in the source.
    """
    reader = csv.reader(io.StringIO(csv_text.removeprefix("\ufeff"), newline=""), strict=True)
    issues: list[SuppliedDirectoryIssue] = []
    try:
        headers = next(reader, [])
    except csv.Error as exc:
        raise SuppliedDirectoryValidationError((
            SuppliedDirectoryIssue(1, "csv", str(exc)),
        )) from exc
    required = set(SUPPLIED_DIRECTORY_REQUIRED_COLUMNS)
    allowed = required | set(SUPPLIED_DIRECTORY_OPTIONAL_COLUMNS)
    for column in sorted(required - set(headers)):
        issues.append(SuppliedDirectoryIssue(1, column, "Required column is missing"))
    for column in sorted(set(headers) - allowed):
        issues.append(SuppliedDirectoryIssue(1, column, "Unknown column"))
    for column in sorted({name for name in headers if headers.count(name) > 1}):
        issues.append(SuppliedDirectoryIssue(1, column, "Column appears more than once"))
    if issues:
        raise SuppliedDirectoryValidationError(tuple(issues))

    accounts: dict[str, SuppliedDirectoryAccount] = {}
    branches: dict[str, SuppliedDirectoryBranch] = {}
    market: tuple[str, str] | None = None
    duplicate_rows = 0
    while True:
        line = reader.line_num + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            issues.append(SuppliedDirectoryIssue(line, "csv", str(exc)))
            break
        if not cells:
            continue
        if len(cells) != len(headers):
            issues.append(SuppliedDirectoryIssue(
                line, "csv", f"Expected {len(headers)} fields; received {len(cells)}",
            ))
            continue
        row = dict(zip(headers, (value.strip() for value in cells)))
        row_issues: list[SuppliedDirectoryIssue] = []
        for column in SUPPLIED_DIRECTORY_REQUIRED_COLUMNS:
            if not row[column]:
                row_issues.append(SuppliedDirectoryIssue(line, column, "Required value is missing"))
        for column in ("account_id", "branch_id"):
            if row[column] and not _ID_RE.fullmatch(row[column]):
                row_issues.append(SuppliedDirectoryIssue(
                    line, column, "Use 1–128 ASCII letters, digits, '.', '_', ':', or '-', starting with a letter or digit",
                ))
        if row["country_code"] and not _COUNTRY_RE.fullmatch(row["country_code"]):
            row_issues.append(SuppliedDirectoryIssue(line, "country_code", "Use two uppercase letters"))
        if row["website"] and not _valid_website(row["website"]):
            row_issues.append(SuppliedDirectoryIssue(line, "website", "Use an HTTP(S) URL with a host and no credentials"))
        treatment_ids = _list_value(row["treatment_ids"], "treatment_ids", line, row_issues)
        account_aliases = _list_value(row.get("account_aliases", ""), "account_aliases", line, row_issues)
        branch_aliases = _list_value(row.get("branch_aliases", ""), "branch_aliases", line, row_issues)
        if row_issues:
            issues.extend(row_issues)
            continue

        row_market = (row["country_code"], row["city"])
        if market is None:
            market = row_market
        else:
            for column, supplied, expected in zip(("country_code", "city"), row_market, market):
                if supplied != expected:
                    row_issues.append(SuppliedDirectoryIssue(
                        line, column, f"Every row must use the same {column} ({expected})",
                    ))
        account = SuppliedDirectoryAccount(row["account_id"], row["account_name"], account_aliases)
        previous_account = accounts.get(account.account_id)
        if previous_account is not None:
            for column in ("account_name", "account_aliases"):
                if getattr(previous_account, column) != getattr(account, column):
                    row_issues.append(SuppliedDirectoryIssue(
                        line, column, f"Account {account.account_id!r} has conflicting {column} across rows",
                    ))
        branch = SuppliedDirectoryBranch(
            branch_id=row["branch_id"], account_id=row["account_id"],
            branch_name=row["branch_name"], country_code=row["country_code"],
            city=row["city"], area=row["area"], address=row["address"],
            website=row["website"], treatment_ids=treatment_ids, branch_aliases=branch_aliases,
        )
        previous_branch = branches.get(branch.branch_id)
        if previous_branch is not None and previous_branch != branch:
            row_issues.append(SuppliedDirectoryIssue(
                line, "branch_id", "Repeated branch ID has conflicting details or belongs to another account",
            ))
        if row_issues:
            issues.extend(row_issues)
            continue
        accounts[account.account_id] = account
        branches[branch.branch_id] = branch
        if previous_branch is not None:
            duplicate_rows += 1

    if not branches and not issues:
        issues.append(SuppliedDirectoryIssue(None, "csv", "At least one branch row is required"))
    if issues:
        raise SuppliedDirectoryValidationError(tuple(issues))
    assert market is not None
    return SuppliedClinicDirectory(
        country_code=market[0], city=market[1],
        accounts=tuple(accounts[key] for key in sorted(accounts)),
        branches=tuple(branches[key] for key in sorted(branches)),
        duplicate_rows=duplicate_rows,
    )


def load_supplied_clinic_directory(path: Path | str) -> SuppliedClinicDirectory:
    """Read a UTF-8 file without modifying it or the application database."""
    try:
        csv_text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SuppliedDirectoryValidationError((
            SuppliedDirectoryIssue(None, "file", f"Unable to read a UTF-8 CSV: {exc}"),
        )) from exc
    return parse_supplied_clinic_directory(csv_text)
