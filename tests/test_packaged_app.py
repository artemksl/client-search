"""Real loopback HTTP against temporary state; no browser or provider work."""
from contextlib import contextmanager
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from client_search.app import create_server


@contextmanager
def running_server(root):
    server = create_server(0, root=root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_packaged_assets_and_workspace_persist_without_starting_checks(tmp_path):
    with running_server(tmp_path) as url:
        for path, expected in (
            ("/health", b'"ok": true'),
            ("/visibility-testing", b"client-research-root"),
            ("/static/client_research.js", b'Research prompt'),
            ("/static/client_research_directory_prompt.txt", b"BEGIN CSV DATA CONTRACT"),
            ("/api/client-research/directory-template", b"account_id,account_name,branch_id"),
        ):
            with urlopen(url + path) as response:
                assert response.status == 200
                assert expected in response.read()
        payload = {"city": "Paris", "country_code": "FR", "country_name": "France", "language": "en", "timezone": "Europe/Paris"}
        request = Request(url + "/api/client-research/workspaces", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with urlopen(request) as response:
            workspace = json.load(response)["workspace"]
        with urlopen(url + "/api/jobs") as response:
            assert json.load(response)["jobs"] == []
    with running_server(tmp_path) as url:
        with urlopen(url + "/api/client-research/workspaces") as response:
            assert json.load(response)["workspaces"][0]["id"] == workspace["id"]


def test_packaged_server_rejects_other_application_lanes(tmp_path):
    with running_server(tmp_path) as url:
        for path, method in (("/api/status", "GET"), ("/api/jobs", "POST"), ("/api/profiles", "POST"), ("/api/outreach/clinics", "GET"), ("/api/paid-audits", "POST")):
            request = Request(url + path, method=method)
            try:
                urlopen(request)
            except HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError(f"Unexpectedly exposed {method} {path}")
