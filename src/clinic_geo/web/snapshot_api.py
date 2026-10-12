"""HTTP-independent API adapter for Visibility Snapshot v2 review actions."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import unquote

from clinic_geo.workflows.free_audit.publication import (
    SnapshotConflictError,
    SnapshotNotApprovableError,
    SnapshotNotFoundError,
    SnapshotPublicationError,
    SnapshotPublicationStore,
)


@dataclass(frozen=True)
class APIResponse:
    payload: dict
    status: int = HTTPStatus.OK


class SnapshotAPI:
    """Route Snapshot list/detail/retry/review without coupling to an HTTP server."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        retry_callback: Optional[Callable[[str], object]] = None,
        release_artifact_builder: Optional[Callable] = None,
    ):
        self.store = SnapshotPublicationStore(
            repo_root,
            release_artifact_builder=release_artifact_builder,
        )
        self.retry_callback = retry_callback

    def handle_get(self, path: str) -> APIResponse:
        parts = self._parts(path)
        try:
            if parts == ["api", "snapshots"]:
                releases = self.store.list_current_releases()
                for release in releases:
                    release_id = str(release.get("release_id") or "")
                    artifacts = (release.get("manifest") or {}).get("artifacts") or {}
                    release["downloads"] = {
                        kind: f"/api/snapshots/releases/{release_id}/{kind}"
                        for kind in artifacts
                    }
                return APIResponse(
                    {
                        "snapshots": self.store.list_snapshots(),
                        "current_releases": releases,
                    }
                )
            if len(parts) == 3 and parts[:2] == ["api", "snapshots"]:
                return APIResponse({"snapshot": self.store.get_snapshot(unquote(parts[2]))})
            return APIResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)
        except SnapshotPublicationError as exc:
            return self._error(exc)

    def handle_post(self, path: str, payload: Mapping | None) -> APIResponse:
        parts = self._parts(path)
        payload = payload if isinstance(payload, Mapping) else {}
        if len(parts) != 4 or parts[:2] != ["api", "snapshots"]:
            return APIResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)
        report_id = unquote(parts[2])
        action = parts[3]
        try:
            if action == "retry-missing":
                # Validate and invoke the background-job seam before changing the
                # operational state.  A failed queue operation is therefore visible
                # as an API failure, not a retry that never existed.
                detail = self.store.get_snapshot(report_id)
                if detail["publication_state"] in {"prospect_ready", "rejected"}:
                    raise SnapshotConflictError(
                        "Create a new Snapshot instead of changing a finalized report"
                    )
                job = self._request_retry_recovery(report_id)
                result = self.store.mark_retry_requested(report_id)
                return APIResponse(
                    {
                        "retry": result,
                        "job": self._serialize_job(job),
                    },
                    HTTPStatus.ACCEPTED,
                )
            if action == "approve":
                snapshot = self.store.approve(
                    report_id,
                    reviewer=str(payload.get("reviewer") or ""),
                    confirmations=(
                        payload.get("confirmations")
                        if isinstance(payload.get("confirmations"), Mapping)
                        else {}
                    ),
                    expected_review_bundle_sha256=(
                        str(payload.get("review_bundle_sha256") or "").strip() or None
                    ),
                )
                return APIResponse({"snapshot": snapshot})
            if action == "resolve-competitor":
                references = payload.get("authoritative_references")
                if not isinstance(references, (list, tuple)):
                    references = []
                resolution = self.store.record_competitor_resolution(
                    report_id,
                    scope_id=str(payload.get("scope_id") or ""),
                    entity_id=str(payload.get("entity_id") or ""),
                    reviewer=str(payload.get("reviewer") or ""),
                    confirmations=(
                        payload.get("confirmations")
                        if isinstance(payload.get("confirmations"), Mapping)
                        else {}
                    ),
                    authoritative_references=references,
                )
                job = self._request_retry_recovery(report_id)
                retry = self.store.mark_retry_requested(report_id)
                return APIResponse(
                    {"resolution": resolution, "retry": retry, "job": self._serialize_job(job)},
                    HTTPStatus.ACCEPTED,
                )
            if action == "reject":
                snapshot = self.store.reject(
                    report_id,
                    reason=str(payload.get("reason") or ""),
                    reviewer=str(payload.get("reviewer") or "founder"),
                )
                return APIResponse({"snapshot": snapshot})
            return APIResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)
        except SnapshotPublicationError as exc:
            return self._error(exc)
        except (TypeError, ValueError) as exc:
            return APIResponse({"error": str(exc), "code": "invalid_request"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return APIResponse(
                {"error": f"Could not queue the Snapshot retry: {exc}", "code": "retry_failed"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [part for part in str(path or "").split("/") if part]

    @staticmethod
    def _serialize_job(job: object) -> object:
        if job is None:
            return None
        if isinstance(job, Mapping):
            return dict(job)
        values = getattr(job, "__dict__", None)
        return dict(values) if isinstance(values, dict) else {"id": str(job)}

    def _request_retry_recovery(self, report_id: str) -> object:
        """Invoke recovery before recording the publication retry transition.

        The callback is required so the publication store cannot claim that a
        retry was queued when no recovery request exists.  In the web
        composition root this callback is the explicit, same-run
        ``JobStore.resume`` boundary; it never starts a replacement run.
        """

        if self.retry_callback is None:
            raise RuntimeError("Snapshot retry recovery is unavailable")
        return self.retry_callback(report_id)

    @staticmethod
    def _error(exc: SnapshotPublicationError) -> APIResponse:
        if isinstance(exc, SnapshotNotFoundError):
            status, code = HTTPStatus.NOT_FOUND, "snapshot_not_found"
        elif isinstance(exc, SnapshotConflictError):
            status, code = HTTPStatus.CONFLICT, "snapshot_conflict"
        elif isinstance(exc, SnapshotNotApprovableError):
            status, code = HTTPStatus.UNPROCESSABLE_ENTITY, "snapshot_not_approvable"
        else:
            status, code = HTTPStatus.BAD_REQUEST, "snapshot_error"
        return APIResponse({"error": str(exc), "code": code}, status)
