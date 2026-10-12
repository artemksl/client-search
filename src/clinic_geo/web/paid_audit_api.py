"""HTTP-independent API adapter for the paid Diagnostic Audit.

Mirrors :mod:`clinic_geo.web.snapshot_api`: ``handle_get`` / ``handle_post`` return
an :class:`APIResponse` (payload + status) so routing is testable without an HTTP
server. File downloads (packet HTML/PDF, workbench, produced copies, and editable
client-report PDFs) are streamed
by ``app.py`` directly from :attr:`PaidAuditAPI.store`, exactly like the snapshot
release-download route.

Launching a paid run is *not* handled here — it goes through the existing
``POST /api/jobs`` background-job seam with ``workflow_id="paid_audit"``. This
adapter covers review/detail, decisions, one-click produce, and delete.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import unquote

from clinic_geo.outreach.api import default_db_path, load_paid_audit_scope_options
from clinic_geo.reporting.client_report_render import (
    render_client_report_html,
    render_client_report_pdf,
)
from clinic_geo.reporting.renderer import ReportRenderError
from clinic_geo.workflows.paid_audit.api import (
    OPERATOR_REPORT_ARTIFACT_KIND,
    ClientReportError,
    PaidAuditError,
    PaidAuditStore,
    build_document,
    edit_clause,
    remove_clause,
    render_model,
    restore_clause,
    set_clause_not_checked,
)
from clinic_geo.page_audit.api import (
    PageAuditResultsError,
    PageAuditResultsNotReadyError,
    PageAuditService,
)


# Compatibility name for callers of the former web-owned projection.
load_scope_options = load_paid_audit_scope_options


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class APIResponse:
    payload: dict
    status: int = HTTPStatus.OK


class PaidAuditAPI:
    """Route paid-audit list/detail/decide/produce/delete without HTTP coupling."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        store: Optional[PaidAuditStore] = None,
        outreach_db_path: Path | str | None = None,
        page_audit_service: Optional[PageAuditService] = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.store = store or PaidAuditStore(repo_root)
        self.outreach_db_path = (
            Path(outreach_db_path)
            if outreach_db_path is not None
            else default_db_path(self.repo_root)
        )
        self.page_audit = page_audit_service or PageAuditService(
            self.repo_root, launcher=self._default_page_audit_launcher
        )

    @staticmethod
    def _default_page_audit_launcher(
        client_id: str,
        engagement_id: str,
        run_id: str,
        *,
        harvest_only: bool = False,
    ) -> int:
        """Launch the one-page audit worker in the background (subprocess seam).

        Mirrors the paid-audit launch pattern (a detached child process); no new
        jobs.py workflow id is required. GET never reaches this path.
        """
        import subprocess
        import sys
        command = [
            sys.executable,
            "-m",
            "clinic_geo.page_audit.run",
            client_id,
            engagement_id,
            run_id,
        ]
        if harvest_only:
            command.append("--harvest-only")
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return process.pid

    def handle_get(self, path: str) -> APIResponse:
        parts = self._parts(path)
        try:
            if parts == ["api", "paid-audits", "scope-options"]:
                return APIResponse({"scope_options": load_scope_options(self.outreach_db_path)})
            if parts == ["api", "paid-audits"]:
                return APIResponse(
                    {
                        "paid_audits": self.store.list_paid_audits(),
                        "availability": self.store.availability(),
                    }
                )
            if (
                len(parts) == 6
                and parts[:2] == ["api", "paid-audits"]
                and parts[5] == "client-report"
            ):
                client_id, engagement_id, run_id = (
                    unquote(parts[2]),
                    unquote(parts[3]),
                    unquote(parts[4]),
                )
                return self._get_client_report(client_id, engagement_id, run_id)
            if len(parts) == 5 and parts[:2] == ["api", "paid-audits"]:
                client_id, engagement_id, run_id = unquote(parts[2]), unquote(parts[3]), unquote(parts[4])
                detail = self.store.get_paid_audit(client_id, engagement_id, run_id)
                # Read-only AI-review progress. GET never starts work or enqueues.
                ai_review = self.page_audit.status(client_id, engagement_id, run_id)
                return APIResponse({"paid_audit": detail, "ai_review": ai_review})
            return self._not_found()
        except PaidAuditError as exc:
            return self._error(exc)
        except ClientReportError as exc:
            return self._client_report_error(exc)

    def handle_post(self, path: str, payload: Mapping | None) -> APIResponse:
        parts = self._parts(path)
        payload = payload if isinstance(payload, Mapping) else {}
        if len(parts) < 6 or parts[:2] != ["api", "paid-audits"]:
            return self._not_found()
        client_id = unquote(parts[2])
        engagement_id = unquote(parts[3])
        run_id = unquote(parts[4])
        if len(parts) >= 7 and parts[5] == "client-report":
            try:
                return self._handle_client_report_post(
                    parts,
                    client_id,
                    engagement_id,
                    run_id,
                    payload,
                )
            except PaidAuditError as exc:
                return self._error(exc)
            except ClientReportError as exc:
                return self._client_report_error(exc)
            except ReportRenderError as exc:
                return APIResponse(
                    {"error": str(exc), "code": "client_report_render_error"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            except (OSError, TypeError, ValueError) as exc:
                return APIResponse(
                    {"error": str(exc), "code": "invalid_request"},
                    HTTPStatus.BAD_REQUEST,
                )
        if len(parts) != 6:
            return self._not_found()
        action = parts[5]
        try:
            if action == "decide":
                return self._decide(client_id, engagement_id, run_id, payload)
            if action == "cancel":
                result = self.store.cancel(
                    client_id, engagement_id, run_id, reason=str(payload.get("reason") or "")
                )
                return APIResponse({"cancel": result, "paid_audit": self._safe_detail(client_id, engagement_id, run_id)})
            if action == "produce":
                attestation = payload.get("attestation")
                production = self.store.produce(
                    client_id,
                    engagement_id,
                    run_id,
                    actor=str(payload.get("actor") or "founder"),
                    note=str(payload.get("note") or ""),
                    attestation=(str(attestation) if attestation is not None else None),
                )
                return APIResponse(
                    {"production": production, "paid_audit": self._safe_detail(client_id, engagement_id, run_id)},
                    HTTPStatus.CREATED,
                )
            if action == "generate-ai-review-brief":
                # ADR-002 one-page AI audit lane: explicitly start (or resume) the
                # run's page-audit worker. Idempotent — a repeated POST while the
                # run is active or complete relaunches nothing.
                started = self.page_audit.start(client_id, engagement_id, run_id)
                return APIResponse(
                    {"ai_review": started,
                     "paid_audit": self._safe_detail(client_id, engagement_id, run_id)},
                    HTTPStatus.ACCEPTED,
                )
            if action == "ai-review-reopen-page":
                # Founder-triggered, one page at a time. Nothing is sent here:
                # the page is put back in line for the collection pass.
                return self._retry_failed_page(
                    "reopen", client_id, engagement_id, run_id, payload
                )
            if action == "ai-review-resend-page":
                # Founder-triggered. Nothing is sent here either: one new job
                # joins the run's queue and the worker sends it when it next
                # runs, under the unchanged pacing.
                return self._retry_failed_page(
                    "resend", client_id, engagement_id, run_id, payload
                )
            if action == "delete":
                result = self.store.delete(client_id, engagement_id, run_id)
                return APIResponse({"delete": result})
            return self._not_found()
        except PaidAuditError as exc:
            return self._error(exc)
        except (TypeError, ValueError) as exc:
            return APIResponse({"error": str(exc), "code": "invalid_request"}, HTTPStatus.BAD_REQUEST)

    def _retry_failed_page(
        self,
        action: str,
        client_id: str,
        engagement_id: str,
        run_id: str,
        payload: Mapping,
    ) -> APIResponse:
        page_id = payload.get("page_id")
        if not isinstance(page_id, str) or not page_id.strip():
            return APIResponse(
                {"error": "page_id is required", "code": "invalid_request"},
                HTTPStatus.BAD_REQUEST,
            )
        if action == "reopen":
            outcome = self.page_audit.reopen_failed_page(
                client_id, engagement_id, run_id, page_id.strip()
            )
            done = outcome["reopened"]
        else:
            outcome = self.page_audit.resend_failed_page(
                client_id, engagement_id, run_id, page_id.strip()
            )
            done = outcome["resent"]
        return APIResponse(
            {
                "ai_review": outcome,
                "paid_audit": self._safe_detail(client_id, engagement_id, run_id),
            },
            HTTPStatus.OK if done else HTTPStatus.CONFLICT,
        )

    @staticmethod
    def _source_from_export(export_document: Mapping, report: Mapping | None = None) -> dict:
        artifacts = export_document.get("artifacts")
        locator = (
            artifacts.get(OPERATOR_REPORT_ARTIFACT_KIND)
            if isinstance(artifacts, Mapping)
            else None
        )
        generated_at = export_document.get("generated_at")
        if (
            not isinstance(locator, Mapping)
            or not isinstance(locator.get("artifact_id"), str)
            or not locator.get("artifact_id")
            or not isinstance(locator.get("sha256"), str)
            or not locator.get("sha256")
            or not isinstance(generated_at, str)
            or not generated_at
        ):
            raise PaidAuditError(
                "Paid audit engine export has no valid structured-report source locator",
                code="engine_contract_mismatch",
                status=HTTPStatus.BAD_GATEWAY,
            )
        schema_version = (
            str(report.get("schema_version") or "")
            if isinstance(report, Mapping)
            else ""
        )
        return {
            "artifact_id": locator["artifact_id"],
            "sha256": locator["sha256"],
            "report_schema_version": schema_version,
            "exported_at": generated_at,
        }

    def _build_fresh_client_document(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> dict:
        report, _validation, export_document = self.store.load_operator_report_with_export(
            client_id, engagement_id, run_id
        )
        source = self._source_from_export(export_document, report)
        advisory_results = self._read_advisory_results(
            client_id, engagement_id, run_id
        )
        document = build_document(
            report,
            analysis_catalogue=self.store.load_analysis_catalogue(),
            source=source,
            advisory_results=advisory_results,
            now=_utc_now(),
        )
        return self.store.write_client_document(
            client_id, engagement_id, run_id, document
        )

    def _ensure_client_document(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> dict:
        existing = self.store.read_client_document(client_id, engagement_id, run_id)
        if existing is not None:
            return existing
        return self._build_fresh_client_document(client_id, engagement_id, run_id)

    def _read_advisory_results(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> dict | None:
        try:
            return self.page_audit.read_results(client_id, engagement_id, run_id)
        except PageAuditResultsNotReadyError as exc:
            raise PaidAuditError(
                str(exc),
                code=exc.code,
                status=HTTPStatus.CONFLICT,
            ) from exc
        except PageAuditResultsError as exc:
            raise PaidAuditError(
                str(exc),
                code=exc.code,
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc

    @staticmethod
    def _advisory_status(
        document: Mapping, current_results: Mapping | None
    ) -> str:
        incorporated = document.get("advisory_source")
        if current_results is None:
            return "incorporated" if isinstance(incorporated, Mapping) else "absent"
        current_source = {
            "results_schema_version": current_results.get("results_schema_version"),
            "results_sha256": current_results.get("results_sha256"),
        }
        return "incorporated" if incorporated == current_source else "available"

    def _source_status(
        self,
        client_id: str,
        engagement_id: str,
        run_id: str,
        document: Mapping,
        *,
        advisory_status: str,
    ) -> dict:
        advisory_changed = advisory_status == "available"
        try:
            current = self._source_from_export(
                self.store.export_run(client_id, engagement_id, run_id)
            )
        except PaidAuditError as exc:
            return {
                "available": False,
                "changed": advisory_changed,
                "message": (
                    "New advisory observations are available; the edited document was kept."
                    if advisory_changed
                    else str(exc)
                ),
            }
        stored = document.get("source")
        stored_sha = stored.get("sha256") if isinstance(stored, Mapping) else None
        engine_changed = stored_sha != current["sha256"]
        changed = engine_changed or advisory_changed
        return {
            "available": True,
            "changed": changed,
            "current": current,
            "message": (
                "New advisory observations are available; the edited document was kept."
                if advisory_changed and not engine_changed
                else "A newer audit source is available; the edited document was kept."
                if changed
                else None
            ),
        }

    def _produced_with_links(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> list[dict]:
        base = (
            "/api/paid-audits/"
            + "/".join(
                quote_part
                for quote_part in (
                    self._quote(client_id),
                    self._quote(engagement_id),
                    self._quote(run_id),
                )
            )
            + "/client-report/produced/"
        )
        return [
            {**item, "download_url": f"{base}{item['number']}.pdf"}
            for item in self.store.list_produced_pdfs(
                client_id, engagement_id, run_id
            )
        ]

    @staticmethod
    def _quote(value: str) -> str:
        from urllib.parse import quote

        return quote(str(value), safe="")

    def _get_client_report(
        self, client_id: str, engagement_id: str, run_id: str
    ) -> APIResponse:
        document = self._ensure_client_document(client_id, engagement_id, run_id)
        current_advisory = self._read_advisory_results(
            client_id, engagement_id, run_id
        )
        advisory_status = self._advisory_status(document, current_advisory)
        source_status = self._source_status(
            client_id,
            engagement_id,
            run_id,
            document,
            advisory_status=advisory_status,
        )
        return APIResponse(
            {
                "document": document,
                "advisory_status": advisory_status,
                "restore_points": self.store.list_restore_points(
                    client_id, engagement_id, run_id
                ),
                "produced": self._produced_with_links(
                    client_id, engagement_id, run_id
                ),
                "source_changed": source_status["changed"],
                "source_status": source_status,
            }
        )

    def _handle_client_report_post(
        self,
        parts: list[str],
        client_id: str,
        engagement_id: str,
        run_id: str,
        payload: Mapping,
    ) -> APIResponse:
        tail = parts[6:]
        if len(tail) == 3 and tail[0] == "clause" and tail[2] in {
            "edit",
            "remove",
            "restore",
            "review-marker",
        }:
            clause_id = unquote(tail[1])
            document = self._ensure_client_document(client_id, engagement_id, run_id)
            now = _utc_now()
            if tail[2] == "edit":
                wording = payload.get("wording")
                if not isinstance(wording, str):
                    raise ClientReportError(
                        "Clause wording must be text", code="invalid_wording"
                    )
                updated = edit_clause(
                    document, clause_id, wording, now=now
                )
            elif tail[2] == "remove":
                updated = remove_clause(document, clause_id, now=now)
            elif tail[2] == "restore":
                updated = restore_clause(document, clause_id, now=now)
            else:
                not_checked = payload.get("not_checked")
                if type(not_checked) is not bool:
                    raise ClientReportError(
                        "Clause review marker must be true or false",
                        code="invalid_not_checked",
                    )
                updated = set_clause_not_checked(
                    document, clause_id, not_checked, now=now
                )
            saved = self.store.write_client_document(
                client_id, engagement_id, run_id, updated
            )
            return APIResponse({"document": saved})

        if tail == ["save"]:
            document = self._ensure_client_document(client_id, engagement_id, run_id)
            restore_point = self.store.save_restore_point(
                client_id,
                engagement_id,
                run_id,
                document,
                now=_utc_now(),
            )
            return APIResponse(
                {"restore_point": restore_point}, HTTPStatus.CREATED
            )

        if (
            len(tail) == 3
            and tail[0] == "restore-points"
            and tail[2] == "load"
        ):
            current = self._ensure_client_document(client_id, engagement_id, run_id)
            restore_number = unquote(tail[1])
            snapshot = self.store.load_restore_point(
                client_id, engagement_id, run_id, restore_number
            )
            now = _utc_now()
            restore_point = self.store.save_restore_point(
                client_id,
                engagement_id,
                run_id,
                current,
                now=now,
                label=f"Before loading restore point {restore_number}",
            )
            restored = dict(current)
            restored["source"] = snapshot.get("source", {})
            restored["advisory_source"] = snapshot.get("advisory_source")
            restored["sections"] = snapshot["sections"]
            restored["clauses"] = snapshot["clauses"]
            restored["clause_set_revision"] = snapshot["clause_set_revision"]
            restored["updated_at"] = now
            saved = self.store.write_client_document(
                client_id, engagement_id, run_id, restored
            )
            current_advisory = self._read_advisory_results(
                client_id, engagement_id, run_id
            )
            advisory_status = self._advisory_status(saved, current_advisory)
            source_status = self._source_status(
                client_id,
                engagement_id,
                run_id,
                saved,
                advisory_status=advisory_status,
            )
            return APIResponse(
                {
                    "document": saved,
                    "restore_point": restore_point,
                    "advisory_status": advisory_status,
                    "source_status": source_status,
                    "source_changed": source_status["changed"],
                }
            )

        if tail == ["produce"]:
            document = self._ensure_client_document(client_id, engagement_id, run_id)
            model = render_model(document)
            html = render_client_report_html(model)
            directory = self.store.client_report_dir(
                client_id, engagement_id, run_id
            )
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            fd, temporary_name = tempfile.mkstemp(
                prefix=".client-report-render-", suffix=".pdf", dir=directory
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                render_client_report_pdf(html, temporary)
                restore_points = self.store.list_restore_points(
                    client_id, engagement_id, run_id
                )
                latest_restore = (
                    restore_points[-1]["number"] if restore_points else None
                )
                clause_count = sum(
                    len(section.get("clauses") or [])
                    for section in model["sections"]
                )
                produced = self.store.record_produced_pdf(
                    client_id,
                    engagement_id,
                    run_id,
                    temporary,
                    produced_at=_utc_now(),
                    restore_point=latest_restore,
                    clause_count=clause_count,
                )
            finally:
                temporary.unlink(missing_ok=True)
            produced["download_url"] = (
                f"/api/paid-audits/{self._quote(client_id)}/"
                f"{self._quote(engagement_id)}/{self._quote(run_id)}/"
                f"client-report/produced/{produced['number']}.pdf"
            )
            return APIResponse({"produced": produced}, HTTPStatus.CREATED)

        if tail == ["rebuild"]:
            document = self._build_fresh_client_document(
                client_id, engagement_id, run_id
            )
            return APIResponse(
                {
                    "document": document,
                    "advisory_status": self._advisory_status(document, None),
                    "source_changed": False,
                }
            )

        return self._not_found()

    def _decide(self, client_id: str, engagement_id: str, run_id: str, payload: Mapping) -> APIResponse:
        decisions = payload.get("decisions")
        result = self.store.decide_bundle(
            client_id,
            engagement_id,
            run_id,
            bundle_id=str(payload.get("bundle_id") or ""),
            actor=str(payload.get("actor") or ""),
            rationale=str(payload.get("rationale") or ""),
            attestation=str(payload.get("attestation") or "all_items_reviewed"),
            decision=(str(payload["decision"]) if payload.get("decision") else None),
            decisions=list(decisions) if isinstance(decisions, (list, tuple)) else None,
            resolves_action_id=(str(payload["resolves_action_id"]) if payload.get("resolves_action_id") else None),
            payload=payload.get("payload") if isinstance(payload.get("payload"), Mapping) else None,
        )
        return APIResponse(
            {"decision": result, "paid_audit": self._safe_detail(client_id, engagement_id, run_id)}
        )

    def _safe_detail(self, client_id: str, engagement_id: str, run_id: str) -> Optional[dict]:
        """Return refreshed detail, or None if the engine is momentarily unavailable."""
        try:
            return self.store.get_paid_audit(client_id, engagement_id, run_id)
        except PaidAuditError:
            return None

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [part for part in str(path or "").split("/") if part]

    @staticmethod
    def _not_found() -> APIResponse:
        return APIResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)

    @staticmethod
    def _error(exc: PaidAuditError) -> APIResponse:
        status = getattr(exc, "status", HTTPStatus.BAD_REQUEST)
        code = getattr(exc, "code", "paid_audit_error")
        return APIResponse({"error": str(exc), "code": code}, status)

    @staticmethod
    def _client_report_error(exc: ClientReportError) -> APIResponse:
        status = (
            HTTPStatus.NOT_FOUND
            if getattr(exc, "code", "") == "clause_not_found"
            else HTTPStatus.BAD_REQUEST
        )
        return APIResponse(
            {"error": str(exc), "code": getattr(exc, "code", "client_report_error")},
            status,
        )
