"""Independent offline checks of supplied-directory storage and CRM boundaries."""
import base64
from datetime import datetime, timezone
import sqlite3

import pytest

from clinic_geo.outreach.api import ClientResearchService, ConflictError

CSV = (
    "account_id,account_name,branch_id,branch_name,country_code,city,area,address,website,treatment_ids\n"
    "a,Alpha,b,Alpha Central,FR,Paris,Central,1 Rue Test,https://alpha.example,skin\n"
)
NOW = "2026-10-11T10:00:00+00:00"


@pytest.fixture
def imported(tmp_path):
    service = ClientResearchService(tmp_path, clock=lambda: datetime.fromisoformat(NOW))
    workspace = service.create_workspace(city="Paris", country_code="FR", country_name="France")
    preview = service.preview_import(workspace["id"], file_name="clinic.csv",
        content_base64=base64.b64encode(CSV.encode()).decode())
    service.commit_import(workspace["id"], preview_id=preview["preview_id"],
        expected_workspace_version=workspace["version"], request_id="first-import")
    return service, service.get_workspace(workspace["id"])


def _legacy_branch_contact(service, internal):
    with service.store.session(write=True) as c:
        c.execute("INSERT INTO research_releases(release_id,manifest_sha256,release_path,imported_at,account_count,manifest_json) VALUES('legacy','hash','fixture',?,1,'{}')", (NOW,))
        c.execute("INSERT INTO clinic_branches VALUES('legacy-branch',?,'legacy',?)", (internal, NOW))
        c.execute("INSERT INTO contact_points(id,account_id,channel,value,normalized_value,route_scope,verification_status,version,created_at,updated_at,branch_id) VALUES('route',?,'email','clinic@example.fr','clinic@example.fr','organization','verified',1,?,?,'legacy-branch')", (internal, NOW, NOW))


def test_account_panel_cannot_rewrite_auto_discovered_legacy_branch_route(imported):
    service, workspace = imported
    account = service.get_account(workspace["id"], "a")
    _legacy_branch_contact(service, account["internal_account_id"])
    with pytest.raises((ValueError, ConflictError), match="scope|branch"):
        service.account_action(workspace["id"], "a", action="save_contact", request_id="collision",
            expected_account_version=account["version"], expected_contact_version=1,
            channel="email", value="clinic@example.fr", notes="Account route")
    with service.store.session() as c:
        route = c.execute("SELECT * FROM contact_points WHERE id='route'").fetchone()
        assert route["verification_status"] == "verified"
        assert route["version"] == 1


def test_destination_suppression_does_not_label_entire_buying_account_suppressed(imported):
    service, workspace = imported
    account = service.get_account(workspace["id"], "a")
    with service.store.session(write=True) as c:
        c.execute("INSERT INTO suppressions(id,account_id,scope_type,scope_value,channel,reason,source,created_by,created_at) VALUES('destination',?,'destination','old@example.fr','email','Old route blocked','fixture','user',?)", (account["internal_account_id"], NOW))
    current = service.get_account(workspace["id"], "a")
    assert current["contact_status"] == "uncontacted"
    assert service.list_accounts(workspace["id"])["total"] == 1


def test_changed_action_under_same_request_id_does_not_write_second_event(imported):
    service, workspace = imported
    account = service.get_account(workspace["id"], "a")
    kwargs = {"action": "add_note", "request_id": "same", "expected_account_version": account["version"], "notes": "First"}
    service.account_action(workspace["id"], "a", **kwargs)
    with pytest.raises(ConflictError, match="different content"):
        service.account_action(workspace["id"], "a", **{**kwargs, "notes": "Changed"})
    assert len(service.get_account(workspace["id"], "a")["interactions"]) == 1


def test_failed_event_context_rolls_back_event_and_request_receipt(imported, monkeypatch):
    service, workspace = imported
    account = service.get_account(workspace["id"], "a")
    # A failure after inserting the CRM event must not leave it without context.
    def fail_projection(*args):
        raise RuntimeError("Injected projection failure")
    monkeypatch.setattr(service, "_projection", fail_projection)
    with pytest.raises(RuntimeError, match="Injected"):
        service.account_action(workspace["id"], "a", action="add_note", request_id="atomic",
            expected_account_version=account["version"], notes="Must roll back")
    with service.store.session() as c:
        assert c.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM client_search_request_receipts WHERE request_id='atomic'").fetchone()[0] == 0
        assert c.execute("SELECT version FROM sales_accounts WHERE id=?", (account["internal_account_id"],)).fetchone()[0] == account["version"]


def test_noop_reimport_retains_explicit_schedule_grant(imported):
    service, workspace = imported
    service.set_schedule(workspace["id"], enabled=True, grant={"cells": ["cell"]},
        execution_fingerprint="fixture", expected_workspace_version=workspace["version"])
    before = service.get_workspace(workspace["id"])["schedule"]
    preview = service.preview_import(workspace["id"], file_name="renamed.csv",
        content_base64=base64.b64encode(CSV.replace("Alpha Central", " Alpha Central ").encode()).decode(),
        base_directory_revision=workspace["directory_revision"])
    result = service.commit_import(workspace["id"], preview_id=preview["preview_id"],
        expected_workspace_version=workspace["version"], request_id="noop")
    assert result["unchanged"] is True
    assert service.get_workspace(workspace["id"])["schedule"] == before


def test_branch_scope_change_keeps_contact_history_and_invalidates_direct_hash(imported):
    service, workspace = imported
    account = service.get_account(workspace["id"], "a")
    original_hash = workspace["configuration"]["direct_fingerprints"]["b"]
    service.account_action(workspace["id"], "a", action="add_note", request_id="note",
        expected_account_version=account["version"], notes="Retain this history")
    csv = CSV.replace("1 Rue Test", "2 Rue Test")
    preview = service.preview_import(workspace["id"], file_name="changed.csv",
        content_base64=base64.b64encode(csv.encode()).decode(), base_directory_revision=workspace["directory_revision"])
    service.commit_import(workspace["id"], preview_id=preview["preview_id"],
        expected_workspace_version=workspace["version"], request_id="change")
    updated = service.get_workspace(workspace["id"])
    assert updated["configuration"]["direct_fingerprints"]["b"] != original_hash
    current = service.get_account(workspace["id"], "a")
    assert current["internal_account_id"] == account["internal_account_id"]
    assert len(current["interactions"]) == 1


def test_directory_and_configuration_revisions_are_immutable(imported):
    service, workspace = imported
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with service.store.session(write=True) as c:
            c.execute("UPDATE client_search_directory_revisions SET file_name='rewritten' WHERE id=?", (workspace["directory_revision"],))
