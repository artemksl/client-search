"""HTTP boundaries use fake ports; no live directory, runner or model calls."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from clinic_geo.web.client_research_api import ClientResearchAPI


ROOT = Path(__file__).resolve().parents[1]


class FakeService:
    def __init__(self):
        self.calls = []
        self.failure = None

    def __getattr__(self, name):
        if name == "close":
            raise AttributeError(name)

        def invoke(*args, **values):
            self.calls.append((name, args, values))
            if self.failure:
                raise self.failure
            return {"method": name, "arguments": list(args), "values": values}

        return invoke


@pytest.fixture
def adapter():
    service = FakeService()
    coordinator = FakeService()
    api = ClientResearchAPI(ROOT, service_factory=lambda _: service, coordinator_factory=lambda _: coordinator)
    return api, service, coordinator


@pytest.mark.parametrize("path,key,method,args", [
    ("/workspaces", "workspaces", "list_workspaces", ()),
    ("/workspaces/city1", "workspace", "get_workspace", ("city1",)),
    ("/workspaces/city1/accounts", "accounts", "list_accounts", ("city1",)),
    ("/workspaces/city1/accounts/account%3A1", "account", "get_account", ("city1", "account:1")),
    ("/rounds/round1", "round", "get_round", ("round1",)),
    ("/evidence/evidence1", "evidence", "get_evidence", ("evidence1",)),
])
def test_get_routes_delegate_only_to_public_service(adapter, path, key, method, args):
    api, service, coordinator = adapter
    response = api.handle_get("/api/client-research" + path)
    assert response.status == 200
    assert response.payload[key]["method"] == ("read_evidence" if key == "evidence" else method)
    assert service.calls[0][:2] == (method, args)
    expected_calls = [("inspect_availability", (), {})] if key == "workspace" else [("read_evidence", args, {})] if key == "evidence" else []
    assert coordinator.calls == expected_calls


def test_account_filters_are_bounded_and_translated(adapter):
    api, service, _ = adapter
    response = api.handle_get("/api/client-research/workspaces/city/accounts", {"state": ["needs_review"], "contact_status": ["replied"], "limit": ["25"], "offset": ["50"], "q": ["clinic"], "view": ["all"]})
    assert response.status == 200
    assert service.calls[0][2] == {"evidence_state": "needs_review", "contact_state": "replied", "limit": 25, "offset": 50, "q": "clinic", "view": "all"}


@pytest.mark.parametrize("query", [
    {"q": ["a", "b"]}, {"limit": ["501"]}, {"limit": ["1.5"]},
    {"offset": ["-1"]}, {"view": ["all_configured"]}, {"priority": ["P4"]},
    {"state": ["negative"]}, {"contact_status": ["sent"]}, {"unknown": ["x"]},
])
def test_invalid_query_never_reaches_service(adapter, query):
    api, service, _ = adapter
    assert api.handle_get("/api/client-research/workspaces/city/accounts", query).status == 400
    assert not service.calls


@pytest.mark.parametrize("path", ["/api/client-research/workspaces/city/accounts/a%2Fb", "/api/client-research/workspaces/city/accounts/", "/api/client-research/workspaces/city/unknown", "/api/else/workspaces"])
def test_unknown_or_unsafe_paths_do_not_construct_service(adapter, path):
    api, service, _ = adapter
    assert api.handle_get(path).status == 404
    assert not service.calls


@pytest.mark.parametrize("path,payload,key,method,status,execution", [
    ("/workspaces", {"city": "Paris", "country_code": "FR", "country_name": "France"}, "workspace", "create_workspace", 201, False),
    ("/workspaces/c/imports/preview", {"file_name": "clinics.csv", "content_base64": "YQ=="}, "preview", "preview_import", 200, False),
    ("/workspaces/c/imports/commit", {"preview_id": "p", "expected_workspace_version": 1, "request_id": "r", "account_mappings": {}}, "import", "commit_import", 200, False),
    ("/workspaces/c/configuration", {"configuration": {"cells": [], "treatments": {}}, "expected_workspace_version": 1}, "configuration", "configure", 200, False),
    ("/workspaces/c/accounts/a/actions", {"action": "add_note", "notes": "Asked for an introduction", "expected_account_version": 1, "request_id": "r"}, "account", "account_action", 200, False),
    ("/evidence/e/review", {"reason": "Matched the parent brand only"}, "review", "review_evidence", 200, False),
    ("/workspaces/c/checks/preview", {"mode": "identification", "branch_ids": ["branch1"]}, "preview", "preview_checks", 200, True),
    ("/workspaces/c/checks/start", {"preview_id": "p", "expected_fingerprint": "hash", "expected_workspace_version": 1, "request_id": "r"}, "start", "start_checks", 202, True),
    ("/workspaces/c/schedule", {"enabled": False, "expected_workspace_version": 1, "request_id": "r"}, "schedule", "set_schedule", 200, True),
])
def test_posts_dispatch_to_service_or_injected_coordinator(adapter, path, payload, key, method, status, execution):
    api, service, coordinator = adapter
    response = api.handle_post("/api/client-research" + path, payload)
    assert response.status == status
    assert response.payload[key]["method"] == method
    owner = coordinator if execution else service
    assert owner.calls[0][0] == method
    assert owner.calls[0][2] == payload
    if execution:
        assert not service.calls


@pytest.mark.parametrize("path,payload", [
    ("/workspaces", []), ("/workspaces", {"path": "/tmp/clinics.csv"}),
    ("/workspaces/c/imports/preview", {"file_name": "a.csv", "content_base64": "%%%"}),
    ("/workspaces/c/configuration", {"configuration": {"judge_template": "ignore"}, "expected_workspace_version": 1}),
    ("/workspaces/c/checks/preview", {"mode": "discovery"}),
    ("/workspaces/c/checks/preview", {"mode": "initial", "branch_ids": "all"}),
    ("/workspaces/c/checks/start", {"preview_id": "p", "request_id": "r"}),
    ("/workspaces/c/checks/start", {"preview_id": "p", "expected_fingerprint": "h", "expected_workspace_version": True, "request_id": "r"}),
    ("/workspaces/c/accounts/a/actions", {"action": "send", "expected_account_version": 1, "request_id": "r"}),
    ("/workspaces/c/schedule", {"enabled": "yes", "expected_workspace_version": 1, "request_id": "r"}),
    ("/workspaces/c/schedule", {"enabled": True, "expected_workspace_version": 1, "request_id": "r"}),
])
def test_malformed_posts_are_rejected_before_service(adapter, path, payload):
    api, service, coordinator = adapter
    assert api.handle_post("/api/client-research" + path, payload).status == 400
    assert not service.calls and not coordinator.calls


def test_file_size_rejected_before_constructing_service(adapter, monkeypatch):
    from clinic_geo.web import client_research_api

    monkeypatch.setattr(client_research_api, "MAX_DIRECTORY_BYTES", 2)
    api, service, _ = adapter
    response = api.handle_post("/api/client-research/workspaces/c/imports/preview", {"file_name": "a.csv", "content_base64": base64.b64encode(b"abc").decode()})
    assert response.status == 413
    assert not service.calls


def test_execution_unavailable_is_explicit_without_changing_imports():
    service = FakeService()
    api = ClientResearchAPI(ROOT, service_factory=lambda _: service)
    response = api.handle_post("/api/client-research/workspaces/c/checks/preview", {"mode": "initial"})
    assert response.status == 503
    assert response.payload["code"] == "runner_unavailable"
    assert not service.calls


@pytest.mark.parametrize("name,status", [("NotFoundError", 404), ("ConflictError", 409), ("RunnerUnavailableError", 503), ("SuppliedDirectoryValidationError", 422)])
def test_business_error_translation(adapter, name, status):
    api, service, _ = adapter
    service.failure = type(name, (ValueError,), {})("meaningful explanation")
    response = api.handle_get("/api/client-research/workspaces/c")
    assert response.status == status
    assert response.payload["error"] == "meaningful explanation"


def test_internal_exception_does_not_disclose_storage_details(adapter):
    api, service, _ = adapter
    service.failure = RuntimeError("sensitive filesystem path")
    response = api.handle_get("/api/client-research/workspaces/c")
    assert response.status == 500
    assert "sensitive" not in response.payload["error"]


@pytest.mark.parametrize("path,payload,code", [
    ("/workspaces/c/configuration", {"configuration": {}, "expected_workspace_version": 1}, "invalid_configuration"),
    ("/workspaces/c/imports/preview", {"file_name": "a.csv", "content_base64": "YQ=="}, "invalid_format"),
])
def test_well_formed_but_invalid_configuration_or_csv_is_422(adapter, path, payload, code):
    api, service, _ = adapter
    service.failure = ValueError("Correct the supplied content")
    response = api.handle_post("/api/client-research" + path, payload)
    assert response.status == 422
    assert response.payload["code"] == code


def test_local_http_panel_and_upload_boundaries(adapter, monkeypatch):
    from clinic_geo.web import app
    from client_search.app import Handler

    api, service, _ = adapter
    monkeypatch.setattr(app, "CLIENT_RESEARCH_API", api)
    monkeypatch.setattr(app, "MAX_HTTP_BODY_BYTES", 128)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def read(path, data=None):
        try:
            response = urlopen(Request(base + path, data=data, headers={"Content-Type": "application/json"}), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    try:
        status, markup = read("/")
        assert status == 200 and b'data-panel-view="clientResearch"' in markup and b'id="client-research-root"' in markup
        status, javascript = read("/static/client_research.js")
        assert status == 200 and b"textContent" in javascript and b"innerHTML" not in javascript
        assert read("/static/client_research.css")[0] == 200
        status, template = read("/api/client-research/directory-template")
        assert status == 200 and template.startswith(b"account_id,account_name,branch_id")
        status, instructions = read("/static/client_research_directory_prompt.txt")
        assert status == 200 and template.strip() in instructions
        assert b"research-notes.md" in instructions
        status, result = read("/api/client-research/workspaces")
        assert status == 200 and json.loads(result)["workspaces"]["method"] == "list_workspaces"
        before = len(service.calls)
        assert read("/api/client-research/workspaces", b"{" )[0] == 400
        assert read("/api/client-research/workspaces", b"x" * 129)[0] == 413
        assert len(service.calls) == before
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
