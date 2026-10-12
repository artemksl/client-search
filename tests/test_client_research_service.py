from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import base64
import json
import sqlite3

import pytest

from clinic_geo.outreach.api import ClientResearchService, ConflictError
from clinic_geo.outreach.db import connect, initialize_database
from clinic_geo.outreach.wp2_migrations import apply_p3_migrations


CSV = 'account_id,account_name,branch_id,branch_name,country_code,city,area,address,website,treatment_ids\na,Alpha,b,Alpha Central,FR,Paris,Central,1 Rue Test,https://alpha.example,skin\n'


@pytest.fixture
def service(tmp_path):
    clock = [datetime(2026, 10, 11, tzinfo=timezone.utc)]
    service = ClientResearchService(tmp_path, clock=lambda: clock[0])
    service.test_clock = clock
    return service


def workspace(service):
    return service.create_workspace(city="Paris", country_code="FR", country_name="France", language="French", timezone="Europe/Paris")


def preview(service, w, text=CSV, mappings=None):
    return service.preview_import(w["id"], file_name="directory.csv", content_base64=base64.b64encode(text.encode()).decode(), base_directory_revision=w["directory_revision"], account_mappings=mappings)


def import_directory(service, w, text=CSV, mappings=None):
    p = preview(service, w, text, mappings)
    return service.commit_import(w["id"], preview_id=p["preview_id"], expected_workspace_version=w["version"], request_id=p["preview_id"], account_mappings=mappings)


def setup(service):
    w = workspace(service)
    import_directory(service, w)
    return service.get_workspace(w["id"])


def test_import_no_legacy_release_and_idempotent_delivery(service):
    w = workspace(service)
    p = preview(service, w)
    kwargs = dict(preview_id=p["preview_id"], expected_workspace_version=w["version"], request_id="import-request-1")
    first = service.commit_import(w["id"], **kwargs)
    assert service.commit_import(w["id"], **kwargs) == first
    with service.store.session() as c:
        assert c.execute("SELECT COUNT(*) FROM research_releases").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM clinic_branches").fetchone()[0] == 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    account = service.get_account(w["id"], "a")
    assert account["branches"][0]["branch_id"] == "b"
    assert account["contact_status"] == "uncontacted"
    assert account["priority"] is None


def test_reordering_format_noop_is_receipted_and_retains_version(service):
    w = setup(service)
    p = preview(service, w, CSV.replace("Alpha Central", " Alpha Central "))
    args = dict(preview_id=p["preview_id"], expected_workspace_version=w["version"], request_id="noop-request")
    result = service.commit_import(w["id"], **args)
    assert result["unchanged"] is True
    assert result["version"] == w["version"]
    assert service.commit_import(w["id"], **args) == result
    with pytest.raises(ConflictError):
        service.commit_import(w["id"], **{**args, "account_mappings": {"a": "other"}})


def test_invalid_expired_stale_preview_and_reparent_are_atomic(service):
    w = setup(service)
    with pytest.raises(ConflictError, match="buying account"):
        preview(service, w, CSV.replace("\na,Alpha,", "\nx,Other,"))
    p = preview(service, w)
    service.test_clock[0] += timedelta(minutes=31)
    with pytest.raises(ConflictError, match="expired"):
        service.commit_import(w["id"], preview_id=p["preview_id"], expected_workspace_version=w["version"], request_id="expired-request")
    assert service.get_workspace(w["id"])["directory_revision"] == w["directory_revision"]


def test_retire_reactivate_preserves_account_contact_and_suppression(service):
    w = setup(service)
    a = service.get_account(w["id"], "a")
    service.account_action(w["id"], "a", action="suppress", expected_account_version=a["version"], request_id="suppress-1", reason="Already a customer")
    replacement = CSV.replace("a,Alpha,b,Alpha Central", "x,Other,z,Other Central").replace("alpha.example", "other.example")
    import_directory(service, w, replacement)
    w = service.get_workspace(w["id"])
    import_directory(service, w)
    returned = service.get_account(w["id"], "a")
    assert returned["internal_account_id"] == a["internal_account_id"]
    assert returned["contact_status"] == "suppressed"
    assert service.list_accounts(w["id"])["total"] == 0


def test_same_supplied_ids_other_workspace_isolate_without_mapping(service):
    first = setup(service)
    second = workspace(service)
    import_directory(service, second)
    one = service.get_account(first["id"], "a")["internal_account_id"]
    assert service.get_account(second["id"], "a")["internal_account_id"] != one
    third = workspace(service)
    import_directory(service, third, mappings={"a": one})
    assert service.get_account(third["id"], "a")["internal_account_id"] == one
    with pytest.raises(ConflictError):
        preview(service, service.get_workspace(second["id"]), mappings={"a": one})


def test_nonenglish_configuration_requires_wording_templates_and_shared_cells(service):
    w = setup(service)
    assert w["configuration"]["source_template"] == ""
    assert len(w["available_cells"]) == 1
    with pytest.raises(ValueError, match="wording"):
        service.configure(w["id"], configuration={}, expected_workspace_version=w["version"])
    updated = service.configure(w["id"], configuration={"treatments": {"skin": "soins de peau"}, "source_template": "Identifiez {branch_name} à {area}, {city}, {country_name}.", "recommendation_template": "Quels établissements recommandez-vous pour {treatment_label} à {area}, {city}, {country_name}?"}, expected_workspace_version=w["version"])
    assert len(updated["configuration"]["required_samples"]) == 21
    assert updated["configuration"]["cells"][0]["treatment_label"] == "soins de peau"


def test_external_contact_and_context_atomic_receipt_no_opportunity(service):
    w = setup(service)
    a = service.get_account(w["id"], "a")
    args = dict(action="log_external_contact", expected_account_version=a["version"], request_id="external-contact-1", branch_id="b", notes="Called yesterday", channel="phone", occurred_at="2026-10-10T10:00:00+00:00")
    result = service.account_action(w["id"], "a", **args)
    assert service.account_action(w["id"], "a", **args) == result
    assert service.get_account(w["id"], "a")["contact_status"] == "contacted"
    with service.store.session() as c:
        assert c.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM client_search_interaction_contexts").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0
        assert c.execute("SELECT branch_id FROM interactions").fetchone()[0] is None


def test_notes_do_not_mark_contacted_and_international_phone_required(service):
    w = setup(service)
    a = service.get_account(w["id"], "a")
    with pytest.raises(ValueError, match="country-code"):
        service.account_action(w["id"], "a", action="save_contact", expected_account_version=a["version"], request_id="phone-national", channel="phone", value="01 22 33 44 55")
    service.account_action(w["id"], "a", action="save_contact", expected_account_version=a["version"], request_id="phone-international", channel="phone", value="+33 1 22 33 44 55")
    a = service.get_account(w["id"], "a")
    assert a["contacts"][0]["verification_status"] == "unverified"
    assert a["contacts"][0]["normalized_value"].startswith("+33")
    service.account_action(w["id"], "a", action="add_note", expected_account_version=a["version"], request_id="note-1", notes="Research note")
    assert service.get_account(w["id"], "a")["contact_status"] == "uncontacted"


def test_future_external_activity_rejected_and_inherited_stage_excludes_queue(service):
    w = setup(service)
    a = service.get_account(w["id"], "a")
    with pytest.raises(ValueError, match="already"):
        service.account_action(w["id"], "a", action="log_external_contact", expected_account_version=a["version"], request_id="future-event", notes="Not yet", occurred_at="2027-01-01T00:00:00+00:00")
    with service.store.session(write=True) as c:
        c.execute("UPDATE sales_accounts SET stage='contacted' WHERE id=?", (a["internal_account_id"],))
    assert service.list_accounts(w["id"])["total"] == 0
    assert service.get_account(w["id"], "a")["contact_date_label"] == "Contact date not recorded"


def test_migration_013_preserves_approved_wp2_clone(tmp_path, monkeypatch):
    import clinic_geo.outreach.db as db
    path = tmp_path / "rehearsal.sqlite3"
    migrations = db._migration_resources()
    monkeypatch.setattr(db, "_migration_resources", lambda: migrations[:-1])
    initialize_database(path)
    with closing(connect(path)) as c:
        apply_p3_migrations(c, temporary_root=tmp_path)
        wp2_before = c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    monkeypatch.setattr(db, "_migration_resources", lambda: migrations)
    initialize_database(path)
    with closing(connect(path)) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT version FROM schema_migrations WHERE version!='013_client_search_workspace.sql' ORDER BY version").fetchall() == wp2_before
        assert c.execute("SELECT sql FROM sqlite_master WHERE name='idx_current_rationale'").fetchone()[0].endswith("WHERE is_current = 1 AND branch_id IS NULL")
