from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clinic_geo.outreach.api import (
    SuppliedClinicDirectory,
    SuppliedDirectoryValidationError,
    load_supplied_clinic_directory,
    parse_supplied_clinic_directory,
)


REQUIRED_COLUMNS = (
    "account_id",
    "account_name",
    "branch_id",
    "branch_name",
    "country_code",
    "city",
    "area",
    "address",
    "website",
    "treatment_ids",
)
COLUMNS = (*REQUIRED_COLUMNS, "account_aliases", "branch_aliases")


def _row(**changes: str) -> dict[str, str]:
    row = {
        "account_id": "account-001",
        "account_name": "Example Clinics",
        "branch_id": "branch-001",
        "branch_name": "Example Clinics Soho",
        "country_code": "GB",
        "city": "London",
        "area": "Soho",
        "address": "1 Example Street, London",
        "website": "https://example.test/soho",
        "treatment_ids": "skin-treatment|laser-treatment",
        "account_aliases": "",
        "branch_aliases": "",
    }
    row.update(changes)
    return row


def _csv(
    rows: list[dict[str, str]],
    columns: tuple[str, ...] = COLUMNS,
) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows([row.get(column, "") for column in columns] for row in rows)
    return output.getvalue()


def _assert_rejected(
    source: str,
    *,
    field: str | None = None,
    line: int | None = None,
) -> SuppliedDirectoryValidationError:
    with pytest.raises(SuppliedDirectoryValidationError) as caught:
        parse_supplied_clinic_directory(source)
    error = caught.value
    assert isinstance(error, ValueError)
    assert isinstance(error.issues, tuple)
    assert error.issues
    assert all(issue.message for issue in error.issues)
    assert all(isinstance(issue.field, str) for issue in error.issues)
    assert all(
        issue.line_number is None or isinstance(issue.line_number, int)
        for issue in error.issues
    )
    matching = error.issues
    if field is not None:
        matching = tuple(issue for issue in matching if issue.field == field)
        assert matching, f"No validation issue for {field!r}: {error.issues!r}"
    if line is not None:
        assert any(issue.line_number == line for issue in matching)
    return error


def test_explicit_ids_group_branches_without_merging_shared_brands_or_domains() -> None:
    directory = parse_supplied_clinic_directory(
        _csv(
            [
                _row(),
                _row(
                    branch_id="branch-002",
                    branch_name="Example Clinics Camden",
                    area="Camden",
                    address="2 Example Street, London",
                    website="https://example.test/camden",
                ),
                _row(
                    account_id="independent-franchise",
                    branch_id="branch-003",
                    branch_name="Example Clinics Chelsea",
                    area="Chelsea",
                    address="3 Example Street, London",
                    website="https://example.test/chelsea",
                ),
            ]
        )
    )

    assert isinstance(directory, SuppliedClinicDirectory)
    assert directory.schema_version == "client_search.directory.v1"
    assert (directory.country_code, directory.city) == ("GB", "London")
    assert isinstance(directory.accounts, tuple)
    assert isinstance(directory.branches, tuple)
    assert {account.account_id for account in directory.accounts} == {
        "account-001",
        "independent-franchise",
    }
    assert {branch.branch_id: branch.account_id for branch in directory.branches} == {
        "branch-001": "account-001",
        "branch-002": "account-001",
        "branch-003": "independent-franchise",
    }
    assert directory.duplicate_rows == 0


def test_optional_alias_columns_can_be_omitted() -> None:
    directory = parse_supplied_clinic_directory(_csv([_row()], REQUIRED_COLUMNS))

    assert directory.accounts[0].account_aliases == ()
    assert directory.branches[0].branch_aliases == ()
    assert directory.branches[0].treatment_ids == (
        "laser-treatment",
        "skin-treatment",
    )


def test_ai_research_example_roundtrips_through_the_real_directory_contract() -> None:
    prompt = (ROOT / "src/clinic_geo/web/static/client_research_directory_prompt.txt").read_text(encoding="utf-8")
    source = prompt.split("BEGIN FICTIONAL CSV EXAMPLE\n", 1)[1].split("END FICTIONAL CSV EXAMPLE", 1)[0]
    rows = list(csv.reader(io.StringIO(source, newline=""), strict=True))
    assert tuple(rows[0]) == COLUMNS
    assert all(len(row) == len(COLUMNS) for row in rows[1:])

    directory = parse_supplied_clinic_directory(source)
    assert f"CONTRACT VERSION: {directory.schema_version}" in prompt
    assert (directory.country_code, directory.city) == ("GB", "London")
    assert len(directory.accounts) == 1 and len(directory.branches) == 2
    assert directory.accounts[0].account_name == 'Example Skin, "Étoile"'
    assert directory.accounts[0].account_aliases == ("Example Skin Clinics", "Étoile Skin")
    assert directory.branches[0].treatment_ids == ("laser-hair-removal", "skin-consultation")
    assert directory.branches[1].branch_aliases == ()
    assert directory.duplicate_rows == 0

    # The documented profile can be produced by a standard CSV writer exactly,
    # including comma/quote escaping, Unicode, pipe lists and empty alias cells.
    serialized = io.StringIO(newline="")
    serialized.write(",".join(COLUMNS) + "\n")
    csv.writer(serialized, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows[1:])
    assert serialized.getvalue() == source
    assert parse_supplied_clinic_directory(serialized.getvalue()) == directory


def test_header_order_does_not_change_supplied_relationships() -> None:
    source = [_row(), _row(branch_id="branch-002")]
    normal = parse_supplied_clinic_directory(_csv(source))
    reordered = parse_supplied_clinic_directory(_csv(source, tuple(reversed(COLUMNS))))

    assert normal == reordered


def test_utf8_bom_and_quoted_unicode_fields_preserve_clinic_identity() -> None:
    row = _row(
        account_name='Cliniques "Étoile"',
        branch_name="Étoile Montmartre",
        country_code="FR",
        city="Paris",
        area="Montmartre",
        address="12 rue des Abbesses, deuxième étage",
        website="https://etoile.test/montmartre",
        account_aliases="Étoile Santé | Étoile",
        branch_aliases="Étoile Paris | Étoile Montmartre",
    )
    directory = parse_supplied_clinic_directory("\ufeff" + _csv([row]))

    assert directory.accounts[0].account_name == row["account_name"]
    assert directory.accounts[0].account_aliases == ("Étoile", "Étoile Santé")
    branch = directory.branches[0]
    assert (branch.country_code, branch.city) == ("FR", "Paris")
    assert branch.branch_name == row["branch_name"]
    assert branch.area == row["area"]
    assert branch.address == row["address"]
    assert branch.website == row["website"]
    assert branch.branch_aliases == ("Étoile Montmartre", "Étoile Paris")


def test_pipe_lists_are_trimmed_and_sorted_consistently_between_account_rows() -> None:
    directory = parse_supplied_clinic_directory(
        _csv(
            [
                _row(
                    treatment_ids=" z-treatment | a-treatment ",
                    account_aliases=" Z Clinics | A Clinics ",
                    branch_aliases=" Z Soho | A Soho ",
                ),
                _row(
                    branch_id="branch-002",
                    account_aliases="A Clinics|Z Clinics",
                ),
            ]
        )
    )

    assert directory.accounts[0].account_aliases == ("A Clinics", "Z Clinics")
    first = next(branch for branch in directory.branches if branch.branch_id == "branch-001")
    assert first.treatment_ids == ("a-treatment", "z-treatment")
    assert first.branch_aliases == ("A Soho", "Z Soho")


def test_repeated_identical_branch_rows_collapse_and_report_duplicate_count() -> None:
    directory = parse_supplied_clinic_directory(_csv([_row(), _row(), _row()]))

    assert len(directory.accounts) == 1
    assert len(directory.branches) == 1
    assert directory.duplicate_rows == 2


@pytest.mark.parametrize("field", ["branch_name", "address", "website", "treatment_ids"])
def test_conflicting_rows_for_one_branch_id_are_rejected(field: str) -> None:
    values = {
        "branch_name": "Changed branch name",
        "address": "99 Different Street, London",
        "website": "https://different.test/soho",
        "treatment_ids": "another-treatment",
    }
    _assert_rejected(_csv([_row(), _row(**{field: values[field]})]), line=3)


def test_branch_id_cannot_be_assigned_to_another_account() -> None:
    _assert_rejected(
        _csv([_row(), _row(account_id="another-account")]),
        line=3,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("account_name", "Different organisation"), ("account_aliases", "Different alias")],
)
def test_repeated_account_metadata_must_agree(field: str, value: str) -> None:
    _assert_rejected(
        _csv([_row(), _row(branch_id="branch-002", **{field: value})]),
        field=field,
        line=3,
    )


def test_ids_are_case_sensitive_and_preserved_when_supplied_names_change() -> None:
    first = parse_supplied_clinic_directory(
        _csv([_row(), _row(account_id="ACCOUNT-001", branch_id="BRANCH-001")])
    )
    changed = parse_supplied_clinic_directory(
        _csv([_row(account_name="Renamed organisation", branch_name="Renamed clinic")])
    )

    assert {account.account_id for account in first.accounts} == {"account-001", "ACCOUNT-001"}
    assert {branch.branch_id for branch in first.branches} == {"branch-001", "BRANCH-001"}
    assert changed.accounts[0].account_id == "account-001"
    assert changed.branches[0].branch_id == "branch-001"
    assert changed.accounts[0].account_name == "Renamed organisation"
    assert changed.branches[0].branch_name == "Renamed clinic"


@pytest.mark.parametrize("field", REQUIRED_COLUMNS)
def test_required_values_report_the_field_and_source_row(field: str) -> None:
    _assert_rejected(_csv([_row(**{field: "   "})]), field=field, line=2)


@pytest.mark.parametrize("field", ["account_id", "branch_id"])
@pytest.mark.parametrize("value", ["bad id", "ümlaut", "_leading", "a/b", "a" * 129])
def test_ids_reject_unstable_or_unsupported_forms(field: str, value: str) -> None:
    _assert_rejected(_csv([_row(**{field: value})]), field=field, line=2)


def test_ids_accept_the_documented_ascii_characters_and_length_limit() -> None:
    directory = parse_supplied_clinic_directory(
        _csv([_row(account_id="A.0_:x-1", branch_id="B" * 128)])
    )

    assert directory.accounts[0].account_id == "A.0_:x-1"
    assert directory.branches[0].branch_id == "B" * 128


@pytest.mark.parametrize("value", ["gb", "GBR", "1B", "G", "GÉ"])
def test_country_requires_two_uppercase_ascii_letters(value: str) -> None:
    _assert_rejected(_csv([_row(country_code=value)]), field="country_code", line=2)


@pytest.mark.parametrize(("field", "value"), [("country_code", "FR"), ("city", "Paris")])
def test_one_file_cannot_mix_cities_or_countries(field: str, value: str) -> None:
    _assert_rejected(
        _csv([_row(), _row(branch_id="branch-002", **{field: value})]),
        field=field,
        line=3,
    )


@pytest.mark.parametrize(
    "value",
    [
        "example.test",
        "//example.test",
        "ftp://example.test",
        "http://",
        "https:///missing-host",
        "https://user:password@example.test",
        "https://user@example.test",
        "https://example.test\\oops",
        "https://example.test|oops",
    ],
)
def test_website_requires_http_host_without_credentials(value: str) -> None:
    _assert_rejected(_csv([_row(website=value)]), field="website", line=2)


@pytest.mark.parametrize("value", ["https://münchen.example/klinik", "http://[2001:db8::1]:8080/"])
def test_website_supports_international_domains_and_ip_addresses(value: str) -> None:
    directory = parse_supplied_clinic_directory(_csv([_row(website=value)]))
    assert directory.branches[0].website == value


@pytest.mark.parametrize("field", ["treatment_ids", "account_aliases", "branch_aliases"])
@pytest.mark.parametrize("value", ["one|one", "one| one ", "one||two", "|one", "one|"])
def test_pipe_lists_reject_duplicate_or_empty_entries(field: str, value: str) -> None:
    _assert_rejected(_csv([_row(**{field: value})]), field=field, line=2)


@pytest.mark.parametrize(
    "columns",
    [
        COLUMNS[:-1] + ("Branch_Aliases",),
        COLUMNS + ("unexpected",),
        COLUMNS + ("branch_id",),
        tuple(column for column in COLUMNS if column != "account_id"),
    ],
)
def test_unknown_duplicate_or_missing_headers_are_rejected(columns: tuple[str, ...]) -> None:
    _assert_rejected(_csv([_row()], columns))


@pytest.mark.parametrize("source", ["", "\ufeff", _csv([])])
def test_directory_requires_at_least_one_clinic_row(source: str) -> None:
    _assert_rejected(source)


@pytest.mark.parametrize("extra_columns", [-1, 1])
def test_incorrect_row_width_is_rejected(extra_columns: int) -> None:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(COLUMNS)
    values = [_row()[column] for column in COLUMNS]
    writer.writerow(values[:-1] if extra_columns == -1 else [*values, "surplus"])
    _assert_rejected(output.getvalue(), line=2)


def test_unterminated_quoted_csv_is_rejected() -> None:
    _assert_rejected(
        ",".join(COLUMNS) + '\naccount-001,"unterminated account name\n',
        line=2,
    )


def test_loading_utf8_file_leaves_original_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "clinics.csv"
    original = ("\ufeff" + _csv([_row(branch_name="Clinique Étoile")])).encode("utf-8")
    path.write_bytes(original)

    directory = load_supplied_clinic_directory(path)

    assert directory.branches[0].branch_name == "Clinique Étoile"
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_non_utf8_file_has_a_validation_error_without_writes(
    tmp_path: Path, missing: bool
) -> None:
    path = tmp_path / "clinics.csv"
    original = b"\xff\xfe\x00\x00"
    if not missing:
        path.write_bytes(original)

    with pytest.raises(SuppliedDirectoryValidationError) as caught:
        load_supplied_clinic_directory(path)

    assert caught.value.issues
    assert any(issue.field == "file" for issue in caught.value.issues)
    if missing:
        assert not path.exists()
        assert list(tmp_path.iterdir()) == []
    else:
        assert path.read_bytes() == original
        assert list(tmp_path.iterdir()) == [path]


def test_documented_example_has_three_buyers_and_four_branches() -> None:
    path = ROOT / "docs" / "examples" / "client-search-directory-example.csv"
    original = path.read_bytes()

    directory = load_supplied_clinic_directory(path)

    assert len(directory.accounts) == 3
    assert len(directory.branches) == 4
    assert {branch.branch_id: branch.account_id for branch in directory.branches} == {
        "harbour-marylebone": "harbour-skin",
        "harbour-soho": "harbour-skin",
        "luma-camden": "luma-franchise-north",
        "luma-brixton": "luma-franchise-south",
    }
    assert path.read_bytes() == original


def test_documented_blank_template_requires_user_rows() -> None:
    path = ROOT / "docs" / "examples" / "client-search-directory-template.csv"

    with pytest.raises(SuppliedDirectoryValidationError) as caught:
        load_supplied_clinic_directory(path)

    assert any(issue.field == "csv" for issue in caught.value.issues)


@pytest.mark.parametrize("valid", [True, False])
def test_validation_cli_returns_json_and_does_not_modify_source(
    tmp_path: Path, valid: bool
) -> None:
    path = tmp_path / "clinics.csv"
    source = _csv([_row()] if valid else [_row(branch_id="bad id")]).encode("utf-8")
    path.write_bytes(source)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_client_search_directory.py"), str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == (0 if valid else 2), result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    if valid:
        assert payload == {
            "status": "valid",
            "schema_version": "client_search.directory.v1",
            "country_code": "GB",
            "city": "London",
            "accounts": 1,
            "branches": 1,
            "duplicate_rows": 0,
        }
    else:
        assert payload["status"] == "invalid"
        assert any(
            issue["line_number"] == 2 and issue["field"] == "branch_id"
            for issue in payload["issues"]
        )
    assert path.read_bytes() == source
    assert list(tmp_path.iterdir()) == [path]
