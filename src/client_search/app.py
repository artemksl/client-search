"""Serve client research with the existing guarded runner and job controls.

The copied clinic_geo modules keep their original contracts. This composition
only exposes the research panel, diagnostic checks and saved measurement jobs.
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from clinic_geo.outreach.api import ClientResearchService
from clinic_geo.web import app as host
from clinic_geo.web.client_research_api import ClientResearchAPI
from clinic_geo.web.client_research_execution import ClientResearchCoordinator
from clinic_geo.web.client_research_scheduler import ClientResearchScheduler
from clinic_geo.web.jobs import JobStore, WORKFLOWS


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
JOB_ACTIONS = frozenset({
    "stop", "cancel", "resume", "refetch-preview", "refetch", "finalize",
    "recovery-link", "verification-control", "open-record", "release-ownership", "inspect",
})


class Handler(host.Handler):
    server_version = "ClientSearch/0.1"

    def _not_found(self):
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/visibility-testing#clientResearch")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/api/client-research/directory-template":
            self._send_file(PACKAGE_ROOT / "docs/examples/client-search-directory-template.csv")
        elif (path in {"/visibility-testing", "/health", "/api/testing/visibility", "/api/jobs", "/api/profiles"}
              or path.startswith(("/api/client-research/", "/api/jobs/", "/static/"))):
            super().do_GET()
        else:
            self._not_found()

    def do_POST(self):
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        job_action = len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in JOB_ACTIONS
        if path.startswith(("/api/client-research/", "/api/testing/visibility/")) or job_action:
            super().do_POST()
        else:
            self._not_found()


def create_server(port=8766, *, root=PACKAGE_ROOT):
    """Compose local persistence without starting a scheduler or measurement."""
    root = Path(root).resolve()
    host.REPO_ROOT = root
    host.STORE = JobStore(root, workflows={key: WORKFLOWS[key] for key in ("client_research", "free_audit_stage1")})

    def coordinator(service):
        return ClientResearchCoordinator(root, service, host.STORE)

    host.CLIENT_RESEARCH_API = ClientResearchAPI(root, coordinator_factory=coordinator)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local client search panel.")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    server = create_server(args.port)
    service = ClientResearchService(PACKAGE_ROOT)
    scheduler = ClientResearchScheduler(service, ClientResearchCoordinator(PACKAGE_ROOT, service, host.STORE)).start()
    print(f"Client search: http://127.0.0.1:{server.server_port}/visibility-testing#clientResearch", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.close()
        server.server_close()


if __name__ == "__main__":
    main()
