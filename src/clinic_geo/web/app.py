"""Localhost web control UI."""

from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from clinic_geo.execution.worker_registry import RegistryError
from clinic_geo.visibility_reports.host_job_artifacts import SnapshotReportPreparationError
from clinic_geo.web.config import github_copilot_defaults, profile_payload, save_logged_in_profiles
from clinic_geo.web.data_browser import (
    DEFAULT_PAGE_SIZE,
    list_data_files,
    preview_data_file,
    resolve_data_path,
    table_view,
)
from clinic_geo.persistence.json_store import read_json_or_none
from clinic_geo.web.jobs import JobStore, REPO_ROOT, free_audit_config_path, jobs_to_dicts
from clinic_geo.web.start_receipts import StartRequestConflict
from clinic_geo.web.record_navigation import RecordNavigationError
from clinic_geo.web.outreach_api import OutreachAPI
from clinic_geo.web.client_research_api import ClientResearchAPI, MAX_HTTP_BODY_BYTES
from clinic_geo.web.snapshot_api import SnapshotAPI
from clinic_geo.visibility_reports.api import prepare_snapshot_report
from clinic_geo.workflows.free_audit.publication import (
    SnapshotConflictError,
    SnapshotNotFoundError,
    SnapshotPublicationError,
)
from clinic_geo.web.paid_audit_api import PaidAuditAPI
from clinic_geo.web.campaign_api import open_campaign_api_session
from clinic_geo.outreach.api import prepare_campaign_preview_database
from clinic_geo.workflows.paid_audit.api import (
    PRODUCTION_ARTIFACT_KINDS,
    PaidAuditError,
    REPORT_KINDS,
)
from clinic_geo.web.snapshot_production import (
    SnapshotReleaseConflict,
    preview_area_snapshot_selection,
    preview_database_snapshot_selection,
    snapshot_area_production_options,
)
from clinic_geo.web.snapshot_report_targets import (
    resolve_snapshot_report_target,
    snapshot_report_targets,
)
from clinic_geo.web.prompt_catalog import (
    build_generated_prompts,
    prompt_catalog,
    read_resource,
    save_resource,
)
from clinic_geo.web.visibility_testing import (
    build_visibility_test_config,
    preview_visibility_test,
    visibility_test_options,
)


ASSET_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = ASSET_DIR / "templates"
STATIC_DIR = ASSET_DIR / "static"
STORE = JobStore()
OUTREACH_API = OutreachAPI(REPO_ROOT)


def _client_research_coordinator(service):
    from clinic_geo.web.client_research_execution import ClientResearchCoordinator

    return ClientResearchCoordinator(REPO_ROOT, service, STORE)


CLIENT_RESEARCH_API = ClientResearchAPI(REPO_ROOT, coordinator_factory=_client_research_coordinator)

# The campaign runtime UI operates only on a temporary, loopback-scoped preview
# database under the system temporary directory.  It never opens the protected
# outreach database and never queues collection or external work.
_CAMPAIGN_STATE = {"database_path": None}
_CAMPAIGN_LOCK = threading.Lock()


def _campaign_database_path():
    """Lazily initialize the preview schema and cache only its database path."""

    if _CAMPAIGN_STATE["database_path"] is not None:
        return _CAMPAIGN_STATE["database_path"]
    with _CAMPAIGN_LOCK:
        if _CAMPAIGN_STATE["database_path"] is not None:
            return _CAMPAIGN_STATE["database_path"]
        try:
            import tempfile

            root = Path(tempfile.gettempdir()) / "clinic-geo-campaign-preview"
            root.mkdir(parents=True, exist_ok=True)
            database_path = root / "campaigns.sqlite3"
            _CAMPAIGN_STATE["database_path"] = prepare_campaign_preview_database(
                database_path,
                temporary_root=root,
            )
        except Exception:  # noqa: BLE001 - the campaign UI is optional/preview
            _CAMPAIGN_STATE["database_path"] = None
        return _CAMPAIGN_STATE["database_path"]


@contextmanager
def _campaign_api():
    """Yield a request-scoped API instead of sharing SQLite across threads."""

    database_path = _campaign_database_path()
    if database_path is None:
        yield None
        return
    with open_campaign_api_session(database_path) as api:
        yield api


def queue_snapshot_retry(report_id: str):
    """Request explicit runner-owned recovery for an existing Snapshot run.

    A publication retry must never create replacement measurement work.  New
    runner-backed Snapshot jobs use the job ID as their runner run ID, so the
    publication report ID addresses the already-reserved run directly.
    """
    report_id = str(report_id or "").strip()
    if not report_id or Path(report_id).name != report_id:
        raise ValueError("Invalid Snapshot report ID")
    return STORE.resume(report_id)


SNAPSHOT_API = SnapshotAPI(REPO_ROOT, retry_callback=queue_snapshot_retry)
PAID_AUDIT_API = PaidAuditAPI(REPO_ROOT)
# UI download aliases the paid-audit file routes accept (packet.html/pdf, workbench.html).
PAID_AUDIT_DOWNLOAD_KINDS = set(REPORT_KINDS)
PAID_AUDIT_PRODUCTION_DOWNLOAD_KINDS = set(PRODUCTION_ARTIFACT_KINDS)

REFETCH_BLOCKED_MESSAGE = (
    "The execution request is blocked or its preview has changed. "
    "Refresh the failure readout before retrying."
)
# Engine refusal codes that the UI can explain in plain words. Any other
# engine error keeps its code so the log and the readout can be compared.
ENGINE_REFUSAL_MESSAGES = {
    "run_owned": "Another process currently owns this run. Wait for it to finish and refresh the job.",
    "previous_executor_alive": "The previous worker is still alive. Stop it and wait for it to exit before continuing.",
    "execution_owner_corrupt": "The engine cannot verify the saved ownership record. Execution remains blocked; see the ownership details.",
    "frozen_execution_policy_mismatch": (
        "This run's frozen execution settings differ from the current settings, so the "
        "engine refuses to execute it. The failure readout still works; see its "
        "'execution policy' section."
    ),
    "runner_execution_already_owned": "This process already owns the run; nothing was changed.",
}
RELEASE_REFUSAL_MESSAGES = {
    "run_owned": "Another process holds this run's lock; nothing was released.",
    "previous_executor_alive": "The recorded worker process is still alive; nothing was released.",
    "owner_record_missing": "There is no owner record to release.",
}

# Only named, reviewed refusal codes reach the readout. Arbitrary exception
# messages can contain local paths or provider evidence and stay private.
READOUT_REFUSAL_MESSAGES = {
    "frozen_execution_policy_mismatch": "This run's frozen execution settings do not match the runtime. Recovery remains blocked until compatible settings are restored.",
    "frozen_execution_policy_hash_mismatch": "The saved execution settings no longer match their integrity binding. Restore the original settings file; recovery remains blocked.",
    "frozen_execution_policy_missing": "The run's saved execution settings are missing. Restore the original file before recovery.",
    "frozen_execution_policy_missing_or_changed": "The run's saved execution settings are missing or changed. Restore the original file before recovery.",
    "frozen_execution_policy_unreadable": "The run's saved execution settings cannot be read. Recovery remains blocked.",
    "frozen_execution_policy_invalid": "The run's saved execution settings have an unsupported or damaged format. Recovery remains blocked.",
    "frozen_execution_policy_changed": "The run's saved execution settings have changed. Recovery remains blocked.",
    "execution_control_invalid": "The run's cancellation and execution control record is missing or damaged. Recovery remains blocked.",
    "recovery_journal_unavailable": "The recovery journal is missing or cannot be read. Restore the original journal before recovery.",
    "recovery_journal_history_truncated": "The recovery journal has lost recorded history. Restore the complete original journal before recovery.",
    "recovery_journal_corrupt": "The recovery journal failed its integrity check. Restore the original journal before recovery.",
    "recovery_journal_binding_unavailable": "The recovery journal's saved identity cannot be verified. Recovery remains blocked.",
    "recovery_journal_anchor_missing": "The recovery journal's integrity anchor is missing. Restore the original journal before recovery.",
    "recovery_journal_path_escapes_run": "The recovery journal references a location outside this run. Recovery remains blocked; restore the original run files.",
}


def _failure_readout_error(exc: Exception) -> dict:
    code = str(exc)
    message = READOUT_REFUSAL_MESSAGES.get(code)
    if message is not None:
        return {"error": message, "reason": code, "readout_message": message}
    return {"error": "Failure readout unavailable; ownership and recovery remain blocked.",
            "reason": "failure_readout_unavailable"}


def _release_refusal_message(code: str) -> str:
    return RELEASE_REFUSAL_MESSAGES.get(code, f"Ownership was not released ({code or 'unknown reason'}).")


GIT_INFO_TTL_SECONDS = 300
_git_info_lock = threading.Lock()
_git_info_cache: tuple[float, dict] | None = None


def git_value(args):
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        return proc.stdout.strip()
    except Exception:
        return None


def git_info() -> dict:
    """Branch/commit, memoized: /api/status is polled every 2.5s and these never change."""
    global _git_info_cache
    now = time.monotonic()
    with _git_info_lock:
        cached = _git_info_cache
    if cached and (now - cached[0]) < GIT_INFO_TTL_SECONDS:
        return cached[1]
    # Deliberately outside the lock: never serialize requests behind a subprocess.
    info = {
        "branch": git_value(["branch", "--show-current"]),
        "commit": git_value(["rev-parse", "--short", "HEAD"]),
    }
    with _git_info_lock:
        _git_info_cache = (now, info)
    return info


def app_status():
    jobs = STORE.list()
    running = sum(1 for job in jobs if job.status in {"queued", "running", "finalizing", "stopping"})
    return {
        "repo": str(REPO_ROOT),
        **git_info(),
        "running_jobs": running,
        "workflows": STORE.workflows(),
        "recent_jobs": jobs_to_dicts(jobs),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ClinicGeoControl/0.2"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_text((TEMPLATES_DIR / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif path == "/visibility-testing":
            self._send_text((TEMPLATES_DIR / "visibility_testing.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif path == "/campaigns":
            self._send_text((TEMPLATES_DIR / "campaigns.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif path == "/client-report":
            self._send_text((TEMPLATES_DIR / "client_report.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif path == "/api/client-research/directory-template":
            self._send_file(REPO_ROOT / "docs" / "examples" / "client-search-directory-template.csv")
        elif path.startswith("/api/client-research/"):
            response = CLIENT_RESEARCH_API.handle_get(path, parse_qs(urlparse(self.path).query, keep_blank_values=True))
            self._send_json(response.payload, status=response.status)
        elif path.startswith("/api/campaigns/") or path == "/api/campaigns":
            self._handle_campaign_get(path)
        elif path == "/health":
            self._send_json({"ok": True})
        elif path == "/api/testing/visibility":
            self._send_json(visibility_test_options())
        elif path == "/api/status":
            self._send_json(app_status())
        elif path == "/api/jobs":
            query = urlparse(self.path).query
            if not query:
                self._send_json({"jobs": jobs_to_dicts(STORE.list())})
                return
            params = parse_qs(query, keep_blank_values=True)
            scopes = params.get("scope", [])
            if set(params) != {"scope"} or len(scopes) != 1 or scopes[0] not in {"visibility", "automation"}:
                self._send_json(
                    {"error": "Invalid job history scope. Use visibility or automation.", "reason": "invalid_scope"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"jobs": jobs_to_dicts(STORE.list(limit=None, scope=scopes[0]))})
        elif path == "/api/profiles":
            self._send_json(profile_payload(REPO_ROOT))
        elif path == "/api/prompts":
            self._send_json(prompt_catalog(REPO_ROOT))
        elif path == "/api/snapshot-production":
            config = read_json_or_none(free_audit_config_path(REPO_ROOT))
            if not isinstance(config, dict):
                self._send_json(
                    {"error": "Free Snapshot source config is unavailable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            else:
                try:
                    options = snapshot_area_production_options(REPO_ROOT, base_config=config)
                    defaults = github_copilot_defaults(REPO_ROOT)
                    if defaults is not None:
                        options["github_copilot_defaults"] = defaults
                    self._send_json(options)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                except Exception as exc:
                    self._send_json(
                        {"error": f"Measurement choices are unavailable: {exc}"},
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
        elif path == "/api/snapshot-report-targets":
            try:
                self._send_json(snapshot_report_targets(REPO_ROOT))
            except Exception as exc:
                self._send_json({"error": f"Clinic report choices are unavailable: {exc}"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
        elif path == "/api/prompts/resource":
            self._handle_prompt_resource_get()
        elif path == "/api/data":
            self._handle_data_list()
        elif path == "/api/data/file":
            self._handle_data_file()
        elif path == "/api/data/table":
            self._handle_data_table()
        elif path == "/api/data/download":
            self._handle_data_download()
        elif path.startswith("/api/snapshots/releases/"):
            self._handle_snapshot_release_download(path)
        elif path.startswith("/api/snapshots/") and "/candidate/" in path:
            self._handle_snapshot_candidate_download(path)
        elif path == "/api/snapshots" or path.startswith("/api/snapshots/"):
            self._handle_snapshot_get(path)
        elif path == "/api/paid-audits" or path.startswith("/api/paid-audits/"):
            self._handle_paid_audit_get(path)
        elif path.startswith("/api/outreach/"):
            self._handle_outreach_get(path)
        elif path.startswith("/api/jobs/"):
            self._handle_job_get(path)
        elif path.startswith("/static/"):
            self._send_static(path)
        else:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/client-research/"):
            self._handle_client_research_post(path)
        elif path == "/api/jobs":
            self._handle_job_start()
        elif path.startswith("/api/testing/visibility/"):
            self._handle_visibility_test(path)
        elif path == "/api/snapshot-production/preview":
            self._handle_snapshot_production_preview()
        elif path == "/api/profiles":
            self._handle_profiles_save()
        elif path == "/api/prompts/resource":
            self._handle_prompt_resource_save()
        elif path == "/api/prompts/build":
            self._handle_prompt_build()
        elif path.startswith("/api/snapshots/"):
            self._handle_snapshot_post(path)
        elif path.startswith("/api/paid-audits/"):
            self._handle_paid_audit_post(path)
        elif path.startswith("/api/outreach/"):
            self._handle_outreach_post(path)
        elif path.startswith("/api/campaigns/"):
            self._handle_campaign_post(path)
        elif path.startswith("/api/jobs/"):
            self._handle_job_action(path)
        else:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _handle_visibility_test(self, path):
        parts = path.removeprefix("/api/testing/visibility/").split("/")
        if (len(parts) not in (1, 2)
            or parts[0] not in {"chatgpt", "gemini", "copilot", "copilot-gemini"}
            or (len(parts) == 2 and parts[1] != "preview")):
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json_body()
            if len(parts) == 2:
                self._send_json(preview_visibility_test(parts[0], payload, REPO_ROOT))
                return
            run_config = build_visibility_test_config(parts[0], payload, REPO_ROOT)
            request_id = run_config.pop("request_id")
            job = STORE.start("free_audit_stage1", run_config, request_id=request_id)
        except StartRequestConflict as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        except (KeyError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception:
            self._send_json(
                {"error": "Visibility test start failed; retry with the same request_id to check its outcome."},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_json({
            "job": jobs_to_dicts([job])[0],
            "status_url": f"/api/jobs/{job.id}",
            "readout_url": f"/api/jobs/{job.id}/failures",
        }, status=HTTPStatus.CREATED)

    def _handle_job_start(self):
        try:
            payload = self._read_json_body()
            workflow_id = str(payload.get("workflow_id") or "")
            run_config = payload.get("run_config") if isinstance(payload.get("run_config"), dict) else {}
            request_id = payload.get("request_id")
            job = STORE.start(workflow_id, run_config, request_id=request_id) if request_id is not None else STORE.start(workflow_id, run_config)
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except SnapshotReleaseConflict as exc:
            self._send_json({"error": str(exc), "start_rejected": True}, status=HTTPStatus.CONFLICT)
            return
        except StartRequestConflict as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"job": jobs_to_dicts([job])[0]}, status=HTTPStatus.CREATED)

    def _handle_snapshot_production_preview(self):
        try:
            payload = self._read_json_body()
            selection = (
                payload.get("selection")
                if isinstance(payload.get("selection"), dict)
                else payload
            )
            config = read_json_or_none(free_audit_config_path(REPO_ROOT))
            if not isinstance(config, dict):
                self._send_json(
                    {"error": "Free Snapshot source config is unavailable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            preview_builder = preview_area_snapshot_selection if selection.get("scope_kind") == "area_treatments" else preview_database_snapshot_selection
            preview = preview_builder(REPO_ROOT, config, selection)
        except SnapshotReleaseConflict as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json(
                {"error": f"Snapshot preview is unavailable: {exc}"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json({"preview": preview})

    def _handle_job_action(self, path):
        parts = [p for p in path.split("/") if p]
        if len(parts) != 4:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        job_id = unquote(parts[2])
        action = parts[3]
        try:
            if action == "stop":
                payload = self._read_json_body()
                job = STORE.stop(job_id, execution_id=payload.get("execution_id"))
                self._send_json({"job": jobs_to_dicts([job])[0]})
            elif action == "cancel":
                payload = self._read_json_body()
                job = STORE.cancel(job_id, execution_id=payload.get("execution_id"))
                self._send_json({"job": jobs_to_dicts([job])[0]})
            elif action == "resume":
                job = STORE.resume(job_id)
                self._send_json({"job": jobs_to_dicts([job])[0]})
            elif action == "refetch-preview":
                payload = self._read_json_body()
                preview = STORE.plan_refetch(job_id, request_id=payload.get("request_id", ""))
                self._send_json({"preview": preview})
            elif action == "refetch":
                payload = self._read_json_body()
                job = STORE.refetch(job_id, payload.get("request", payload))
                self._send_json({"job": jobs_to_dicts([job])[0], "accepted_execution_id": job.accepted_execution_id})
            elif action == "finalize":
                payload = self._read_json_body()
                job = STORE.finalize_output(job_id, request_id=payload.get("request_id"),
                                            expected_execution_id=payload.get("expected_execution_id"))
                self._send_json({"job": jobs_to_dicts([job])[0], "accepted_execution_id": job.accepted_execution_id})
            elif action == "recovery-link":
                payload = self._read_json_body()
                self._send_json({"link": STORE.resolve_recovery_link(job_id, payload.get("reference"))})
            elif action == "verification-control":
                payload = self._read_json_body()
                if (not isinstance(payload, dict) or set(payload) != {
                        "episode_id", "revision", "execution_id", "request_id", "action"}):
                    self._send_json({"error": "Refresh this verification before acting.",
                        "reason": "invalid_verification_command"}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"outcome": STORE.verification_control(job_id, **payload)})
            elif action == "open-record":
                payload = self._read_json_body()
                if (not isinstance(payload, dict) or set(payload) != {"item_id"}
                        or not isinstance(payload["item_id"], str) or not payload["item_id"]
                        or len(payload["item_id"]) > 500):
                    self._send_json({"error": "Choose one saved measurement record.", "reason": "invalid_item"}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"navigation": STORE.open_record(job_id, item_id=payload["item_id"])})
            elif action == "release-ownership":
                payload = self._read_json_body()
                self._send_json({"outcome": STORE.release_stale_ownership(job_id, reason=payload.get("reason", ""))})
            elif action == "prepare-report":
                if STORE.get(job_id) is None:
                    raise KeyError(job_id)
                payload = self._read_json_body()
                options = {}
                if payload:
                    options["report_target"] = resolve_snapshot_report_target(STORE.repo_root, payload)
                report = prepare_snapshot_report(
                    job_id, repo_root=STORE.repo_root, run_root=STORE.run_root,
                    **options,
                )
                self._send_json({"report": report})
            elif action == "inspect":
                job = STORE.inspect(job_id)
                self._send_json({"job": jobs_to_dicts([job])[0]})
            else:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except RecordNavigationError as exc:
            self._send_json({"error": str(exc), "reason": exc.reason}, status=HTTPStatus.CONFLICT)
        except RegistryError as exc:
            reason = str(exc)
            explanations = {
                "execution_changed": "The displayed execution has changed. Refresh the job before acting.",
                "finalizer_cannot_be_stopped": "Saved output is being finalized; this local publication step cannot be stopped.",
                "finalization_unavailable": "Saved output cannot be finalized until the worker has exited and the engine confirms completion.",
                "recovery_preview_stale": "The recovery preview has changed. Refresh it before retrying.",
                "active_execution_incompatible": "Another execution is already active for a different request.",
                "request_identity_conflict": "This request ID was already used with different contents.",
            }
            self._send_json({"error": explanations.get(reason, REFETCH_BLOCKED_MESSAGE), "reason": reason}, status=HTTPStatus.CONFLICT)
        except SnapshotReportPreparationError as exc:
            self._send_json({"error": str(exc), "reason": "frozen_input_unavailable"}, status=HTTPStatus.CONFLICT)
        except ValueError as exc:
            if action == "verification-control":
                self._send_json({"error": "Verification changed or its ownership could not be confirmed. Refresh the job before acting.",
                    "reason": "verification_control_refused"}, status=HTTPStatus.CONFLICT)
            elif action == "prepare-report":
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            elif action == "release-ownership":
                # Host-authored validation text ("a reason of 8 to 500
                # characters is required", "the worker is still alive").
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            else:
                self._send_json({"error": REFETCH_BLOCKED_MESSAGE}, status=HTTPStatus.CONFLICT)
        except SnapshotConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except RuntimeError as exc:
            if action == "verification-control":
                self._send_json({"error": "Verification changed or its ownership could not be confirmed. Refresh the job before acting.",
                    "reason": "verification_control_refused"}, status=HTTPStatus.CONFLICT)
            elif action == "prepare-report":
                self._send_json({"error": "Report preparation did not complete. The saved measurement is unchanged; check the local renderer and try again."}, status=HTTPStatus.CONFLICT)
            elif action == "release-ownership":
                self._send_json({"error": _release_refusal_message(str(exc))}, status=HTTPStatus.CONFLICT)
            else:
                self._send_json({"error": REFETCH_BLOCKED_MESSAGE}, status=HTTPStatus.CONFLICT)
        except Exception as exc:
            if action == "verification-control":
                self._send_json({"error": "Verification changed or its ownership could not be confirmed. Refresh the job before acting.",
                    "reason": "verification_control_refused"}, status=HTTPStatus.CONFLICT)
                return
            # Engine state errors (visibility_runner.errors) are not ValueError
            # or RuntimeError subclasses; name the known refusals instead of
            # answering 500 with a bare code.
            message = ENGINE_REFUSAL_MESSAGES.get(str(exc))
            if message is not None:
                self._send_json({"error": message}, status=HTTPStatus.CONFLICT)
            else:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_job_get(self, path):
        parts = [p for p in path.split("/") if p]
        if len(parts) < 3:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        job_id = unquote(parts[2])
        if len(parts) == 4 and parts[3] == "failures":
            try:
                self._send_json({"readout": STORE.failure_readout(job_id)})
            except KeyError:
                self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json(_failure_readout_error(exc), status=HTTPStatus.CONFLICT)
            return
        if len(parts) == 4 and parts[3] == "log":
            try:
                self._send_text(STORE.log_text(job_id), "text/plain; charset=utf-8")
            except KeyError:
                self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if len(parts) != 3:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            job = STORE.inspect(job_id)
        except KeyError:
            self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"job": jobs_to_dicts([job])[0]})

    def _handle_profiles_save(self):
        try:
            payload = self._read_json_body()
            profiles = payload.get("profiles")
            if not isinstance(profiles, list):
                raise ValueError("profiles must be a list")
            save_logged_in_profiles(profiles, REPO_ROOT)
            self._send_json(profile_payload(REPO_ROOT))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_prompt_resource_get(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            self._send_json(
                read_resource(
                    query.get("workflow_id", [""])[0],
                    query.get("resource_id", [""])[0],
                    REPO_ROOT,
                )
            )
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_prompt_resource_save(self):
        try:
            payload = self._read_json_body()
            self._send_json(
                save_resource(
                    str(payload.get("workflow_id") or ""),
                    str(payload.get("resource_id") or ""),
                    str(payload.get("content") or ""),
                    REPO_ROOT,
                )
            )
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_prompt_build(self):
        try:
            payload = self._read_json_body()
            self._send_json(build_generated_prompts(str(payload.get("workflow_id") or ""), REPO_ROOT))
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_data_list(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            payload = list_data_files(
                root=REPO_ROOT,
                process=query.get("process", [""])[0],
                extension=query.get("extension", [""])[0],
                query=query.get("q", [""])[0],
                include_files=query.get("mode", ["advanced"])[0] != "curated",
            )
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    def _handle_data_file(self):
        query = parse_qs(urlparse(self.path).query)
        path = query.get("path", [""])[0]
        try:
            self._send_json(preview_data_file(path, REPO_ROOT))
        except (FileNotFoundError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _int_param(self, query, name, default):
        raw = (query.get(name, [""])[0] or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None

    def _handle_data_table(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            payload = table_view(
                query.get("path", [""])[0],
                root=REPO_ROOT,
                sort=query.get("sort", [""])[0],
                direction=query.get("direction", ["asc"])[0],
                page=self._int_param(query, "page", 1),
                page_size=self._int_param(query, "page_size", DEFAULT_PAGE_SIZE),
                query=query.get("q", [""])[0],
            )
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except (OSError, RecursionError, MemoryError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    def _handle_data_download(self):
        query = parse_qs(urlparse(self.path).query)
        path = query.get("path", [""])[0]
        try:
            candidate = resolve_data_path(path, REPO_ROOT)
            self._send_file(candidate)
        except (FileNotFoundError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_outreach_get(self, path):
        response = OUTREACH_API.handle_get(path, parse_qs(urlparse(self.path).query))
        self._send_json(response.payload, status=response.status)

    def _handle_client_research_post(self, path):
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0:
                raise ValueError("Invalid Content-Length")
            if length > MAX_HTTP_BODY_BYTES:
                self.close_connection = True
                self._send_json({"error": "The request exceeds 16 MiB.", "code": "body_too_large"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            payload = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json({"error": "The request body must be valid JSON.", "code": "invalid_json"}, status=HTTPStatus.BAD_REQUEST)
            return
        response = CLIENT_RESEARCH_API.handle_post(path, payload)
        self._send_json(response.payload, status=response.status)

    def _handle_snapshot_get(self, path):
        response = SNAPSHOT_API.handle_get(path)
        self._send_json(response.payload, status=response.status)

    def _handle_snapshot_post(self, path):
        try:
            payload = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(
                {"error": "The request body must be valid JSON.", "code": "invalid_json"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        response = SNAPSHOT_API.handle_post(path, payload)
        self._send_json(response.payload, status=response.status)

    def _handle_snapshot_release_download(self, path):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 5 or parts[:3] != ["api", "snapshots", "releases"]:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            artifact = SNAPSHOT_API.store.current_release_artifact(parts[3], parts[4])
            self._send_file(artifact)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)

    def _handle_snapshot_candidate_download(self, path):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 5 or parts[:2] != ["api", "snapshots"] or parts[3] != "candidate":
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(urlparse(self.path).query)
        try:
            body, filename = SNAPSHOT_API.store.read_candidate_artifact(
                parts[2], parts[4],
                expected_review_bundle_sha256=query.get("review", [""])[0],
            )
        except SnapshotNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except (SnapshotPublicationError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(filename)[0] or "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_paid_audit_get(self, path):
        parts = [unquote(part) for part in path.split("/") if part]
        # File downloads stream from the store directly (mirrors the snapshot
        # release-download route); the packet HTML is served inline so it is safe
        # to embed in an iframe, the PDF as an attachment.
        try:
            if (
                len(parts) == 8
                and parts[:2] == ["api", "paid-audits"]
                and parts[5:7] == ["client-report", "produced"]
                and parts[7].endswith(".pdf")
                and parts[7][:-4].isdigit()
            ):
                artifact = PAID_AUDIT_API.store.produced_pdf_path(
                    parts[2], parts[3], parts[4], int(parts[7][:-4])
                )
                self._send_file(artifact, attachment=True)
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "paid-audits"]
                and parts[5] in PAID_AUDIT_DOWNLOAD_KINDS
            ):
                artifact = PAID_AUDIT_API.store.report_artifact(parts[2], parts[3], parts[4], parts[5])
                self._send_file(artifact, attachment=parts[5].endswith(".pdf"))
                return
            if (
                len(parts) == 8
                and parts[:2] == ["api", "paid-audits"]
                and parts[5] == "productions"
                and parts[7] in PAID_AUDIT_PRODUCTION_DOWNLOAD_KINDS
            ):
                artifact = PAID_AUDIT_API.store.production_artifact(
                    parts[2], parts[3], parts[4], parts[6], parts[7]
                )
                self._send_file(artifact, attachment=parts[7].endswith(".pdf"))
                return
        except PaidAuditError as exc:
            self._send_json(
                {"error": str(exc), "code": getattr(exc, "code", "paid_audit_error")},
                status=getattr(exc, "status", HTTPStatus.NOT_FOUND),
            )
            return
        response = PAID_AUDIT_API.handle_get(path)
        self._send_json(response.payload, status=response.status)

    def _handle_paid_audit_post(self, path):
        try:
            payload = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(
                {"error": "The request body must be valid JSON.", "code": "invalid_json"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        response = PAID_AUDIT_API.handle_post(path, payload)
        self._send_json(response.payload, status=response.status)

    def _handle_outreach_post(self, path):
        try:
            payload = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(
                {"error": "The request body must be valid JSON.", "code": "invalid_json"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        response = OUTREACH_API.handle_post(path, payload)
        self._send_json(response.payload, status=response.status)

    def _client_is_loopback(self):
        address = getattr(self, "client_address", None) or ()
        host = address[0] if address else ""
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _handle_campaign_get(self, path):
        try:
            with _campaign_api() as api:
                response = (
                    None
                    if api is None
                    else api.handle_get(
                        path, client_is_loopback=self._client_is_loopback()
                    )
                )
        except Exception:  # noqa: BLE001 - the campaign UI is optional/preview
            response = None
        if response is None:
            self._send_json(
                {"error": "campaign preview is unavailable", "code": "unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(response.body, status=response.status)

    def _handle_campaign_post(self, path):
        try:
            payload = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send_json(
                {"error": "The request body must be valid JSON.", "code": "invalid_json"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            with _campaign_api() as api:
                response = (
                    None
                    if api is None
                    else api.handle_post(
                        path,
                        payload,
                        client_is_loopback=self._client_is_loopback(),
                        idempotency_key=self.headers.get("Idempotency-Key"),
                    )
                )
        except Exception:  # noqa: BLE001 - the campaign UI is optional/preview
            response = None
        if response is None:
            self._send_json(
                {"error": "campaign preview is unavailable", "code": "unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(response.body, status=response.status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _send_static(self, path):
        rel = unquote(path[len("/static/"):])
        candidate = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in candidate.parents or not candidate.is_file():
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            self._send_file(candidate, attachment=False)
        except FileNotFoundError:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_file(self, path, *, attachment=True):
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        # This is a localhost control panel whose Python routes and JavaScript are deployed
        # together. Caching one side can leave a newly restarted backend paired with old controls.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, content_type, status=HTTPStatus.OK):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host="127.0.0.1", port=8765):
    server = ThreadingHTTPServer((host, port), Handler)
    from clinic_geo.outreach.api import ClientResearchService
    from clinic_geo.web.client_research_scheduler import ClientResearchScheduler
    from clinic_geo.web.client_research_execution import ClientResearchCoordinator
    service = ClientResearchService(REPO_ROOT)
    scheduler = ClientResearchScheduler(service, ClientResearchCoordinator(REPO_ROOT, service, STORE)).start()
    print(f"Clinic GEO Control UI running at http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.close()
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local Clinic GEO control UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    run(args.host, args.port)


if __name__ == "__main__":
    main()
