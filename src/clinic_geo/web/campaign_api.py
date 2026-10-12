"""Typed, thin, loopback-only HTTP surface for campaign lifecycle actions.

Every handler is a pure request -> :class:`APIResponse` mapping over a SQLite
connection.  It enforces: loopback-only access, exact allowed request fields,
revision version tokens, durable idempotency keys, previews with expiry and
changed/expired conflict handling, request body-size limits, batch action-count
protection, and exception redaction.  It cannot enable any capability, mutate
evidence, or queue collection: it exposes only campaign-lifecycle endpoints and
its confirm paths call the campaign runtime/completion functions, which are
read-only over Market Evidence.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from clinic_geo.integrity import canonical_json, canonical_json_bytes, canonical_sha256

from clinic_geo.campaigns import api_store as campaign_api_store
from clinic_geo.campaigns.completion import (
    CampaignCompletionBlocked,
    CampaignCompletionConflict,
    collect_campaign_job_views,
    complete_campaign_revision,
    plan_campaign_completion,
)
from clinic_geo.campaigns.contracts import CampaignCandidateDisposition
from clinic_geo.campaigns.read_models import build_candidate_queue
from clinic_geo.campaigns.runtime import (
    CampaignActivationConflict,
    CampaignRuntimeError,
    activate_campaign_revision,
    bind_treatment_catalogue,
    revision_version_token,
)


Clock = Callable[[], datetime]

MAX_REQUEST_BODY_BYTES = 16_384
PREVIEW_TTL_SECONDS = 300
MAX_BATCH_ACTIONS = 50


class CampaignAPIError(Exception):
    """A typed, safe-to-surface campaign API error."""

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.safe_message = message
        self.code = code
        self.status = status


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class APIResponse:
    body: dict
    status: int = HTTPStatus.OK


def _require_exact_fields(payload: Mapping[str, Any] | None, allowed: set[str]) -> dict:
    if payload is None or not isinstance(payload, Mapping):
        raise CampaignAPIError(
            "request body must be a JSON object", code="invalid_request",
            status=HTTPStatus.BAD_REQUEST,
        )
    keys = set(payload)
    if keys != allowed:
        raise CampaignAPIError(
            "request fields do not match the exact allowed set",
            code="invalid_request",
            status=HTTPStatus.BAD_REQUEST,
        )
    return dict(payload)


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignAPIError(
            f"{name} must be non-empty text", code="invalid_request",
            status=HTTPStatus.BAD_REQUEST,
        )
    return value


class CampaignAPI:
    """Loopback-only campaign lifecycle API over a temporary SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Clock = _utc_now,
        preview_ttl_seconds: int = PREVIEW_TTL_SECONDS,
        max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES,
        max_batch_actions: int = MAX_BATCH_ACTIONS,
    ) -> None:
        self.connection = connection
        self.clock = clock
        self.preview_ttl_seconds = preview_ttl_seconds
        self.max_request_body_bytes = max_request_body_bytes
        self.max_batch_actions = max_batch_actions

    # -- dispatch -----------------------------------------------------------
    def handle_get(self, path: str, *, client_is_loopback: bool) -> APIResponse:
        try:
            self._require_loopback(client_is_loopback)
            parts = self._parts(path)
            if parts == ["api", "campaigns", "status"]:
                return self._domain_status()
            if parts == ["api", "campaigns", "revisions"]:
                return self._revisions_list()
            if len(parts) == 4 and parts[:3] == ["api", "campaigns", "revisions"]:
                return self._revision_summary(parts[3])
            if (
                len(parts) == 5
                and parts[:3] == ["api", "campaigns", "revisions"]
                and parts[4] == "candidates"
            ):
                return self._candidate_queue(parts[3])
            return self._not_found()
        except CampaignAPIError as exc:
            return self._error(exc)
        except Exception:  # noqa: BLE001 - redact everything unexpected
            return self._redacted_internal_error()

    def handle_post(
        self,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        client_is_loopback: bool,
        idempotency_key: str | None,
    ) -> APIResponse:
        try:
            self._require_loopback(client_is_loopback)
            self._require_body_within_limit(payload)
            parts = self._parts(path)
            if parts[:3] == ["api", "campaigns", "revisions"] and len(parts) == 6:
                revision_id = parts[3]
                group, action = parts[4], parts[5]
                if group == "activation" and action == "preview":
                    return self._activation_preview(revision_id, payload)
                if group == "activation" and action == "confirm":
                    return self._with_idempotency(
                        "activation.confirm", idempotency_key, payload,
                        lambda: self._activation_confirm(revision_id, payload),
                    )
                if group == "completion" and action == "preview":
                    return self._completion_preview(revision_id, payload)
                if group == "completion" and action == "confirm":
                    return self._with_idempotency(
                        "completion.confirm", idempotency_key, payload,
                        lambda: self._completion_confirm(revision_id, payload),
                    )
            if (
                parts[:3] == ["api", "campaigns", "candidates"]
                and len(parts) == 5
                and parts[4] == "disposition"
            ):
                return self._with_idempotency(
                    "candidate.disposition", idempotency_key, payload,
                    lambda: self._candidate_disposition(parts[3], payload),
                )
            # No collection/capability endpoint exists; anything else is absent.
            return self._not_found()
        except CampaignAPIError as exc:
            return self._error(exc)
        except Exception:  # noqa: BLE001
            return self._redacted_internal_error()

    # -- guards -------------------------------------------------------------
    @staticmethod
    def _require_loopback(client_is_loopback: bool) -> None:
        if not client_is_loopback:
            raise CampaignAPIError(
                "campaign API is available on loopback only",
                code="forbidden", status=HTTPStatus.FORBIDDEN,
            )

    def _require_body_within_limit(self, payload: Mapping[str, Any] | None) -> None:
        if payload is None:
            return
        try:
            size = len(canonical_json_bytes(payload))
        except (TypeError, ValueError) as exc:
            raise CampaignAPIError(
                "request body is not canonicalizable", code="invalid_request",
                status=HTTPStatus.BAD_REQUEST,
            ) from exc
        if size > self.max_request_body_bytes:
            raise CampaignAPIError(
                "request body exceeds the size limit",
                code="payload_too_large",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        self._require_action_count_within_limit(payload)

    def _require_action_count_within_limit(self, value: Any) -> None:
        """Reject a request that asks for an unbounded number of actions.

        No campaign endpoint accepts a batch, but a defensive cap on any array
        in the request envelope keeps a single call from fanning out into an
        unbounded amount of work or cost.
        """

        if isinstance(value, (list, tuple)):
            if len(value) > self.max_batch_actions:
                raise CampaignAPIError(
                    "request asks for too many actions",
                    code="too_many_actions",
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                )
            for item in value:
                self._require_action_count_within_limit(item)
        elif isinstance(value, Mapping):
            for item in value.values():
                self._require_action_count_within_limit(item)

    # -- idempotency --------------------------------------------------------
    def _with_idempotency(
        self,
        endpoint: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any] | None,
        apply: Callable[[], APIResponse],
    ) -> APIResponse:
        key = _require_text(idempotency_key, "Idempotency-Key")
        request_sha256 = canonical_sha256(
            {"endpoint": endpoint, "payload": payload}
        )
        existing = campaign_api_store.find_idempotent_response(
            self.connection,
            endpoint,
            key,
        )
        if existing is not None:
            if existing["request_sha256"] != request_sha256:
                raise CampaignAPIError(
                    "idempotency key reused with a different request",
                    code="idempotency_conflict", status=HTTPStatus.CONFLICT,
                )
            return APIResponse(json.loads(existing["response_json"]), existing["http_status"])
        response = apply()
        conflict = campaign_api_store.record_idempotency_key(
            self.connection,
            endpoint=endpoint,
            idempotency_key=key,
            request_sha256=request_sha256,
            response_json=lambda: canonical_json(response.body),
            http_status=int(response.status),
            created_at=lambda: _iso(self.clock()),
        )
        if conflict is not None:
            if conflict["request_sha256"] != request_sha256:
                raise CampaignAPIError(
                    "idempotency key reused with a different request",
                    code="idempotency_conflict", status=HTTPStatus.CONFLICT,
                )
            return APIResponse(
                json.loads(conflict["response_json"]), conflict["http_status"]
            )
        return response

    # -- read models --------------------------------------------------------
    def _domain_status(self) -> APIResponse:
        """Market Evidence and Snapshot status as two strictly separate blocks."""

        active, completed = campaign_api_store.revision_counts(self.connection)
        return APIResponse(
            {
                "market_evidence_status": {
                    "domain": "market_evidence",
                    "relationship": "consumed_read_only",
                    "active_campaign_revisions": active,
                },
                "snapshot_status": {
                    "domain": "free_snapshot",
                    "relationship": "separate_workflow",
                    "note": "Snapshot publication is independent of campaigns.",
                },
                "completed_campaign_revisions": completed,
            }
        )

    def _revisions_list(self) -> APIResponse:
        rows = campaign_api_store.revision_rows(self.connection)
        revisions = []
        for row in rows:
            revisions.append(
                {
                    "revision_id": row["id"],
                    "campaign_id": row["campaign_id"],
                    "revision_number": row["revision_number"],
                    "state": row["state"],
                    "query_scope_status": row["query_scope_status"],
                    "treatment_catalogue_release_id": row["treatment_catalogue_release_id"],
                    "activated_at": row["activated_at"],
                    "completed_at": row["completed_at"],
                    "version_token": revision_version_token(self.connection, row["id"]),
                }
            )
        return APIResponse({"revisions": revisions})

    def _revision_summary(self, revision_id: str) -> APIResponse:
        row = campaign_api_store.revision_row(self.connection, revision_id)
        if row is None:
            return self._not_found()
        token = revision_version_token(self.connection, revision_id)
        # Market Evidence status and Snapshot status are kept in visibly separate
        # sections; campaigns never satisfy or import Snapshot publication.
        return APIResponse(
            {
                "revision": {
                    "revision_id": row["id"],
                    "campaign_id": row["campaign_id"],
                    "revision_number": row["revision_number"],
                    "state": row["state"],
                    "query_scope_status": row["query_scope_status"],
                    "treatment_catalogue_release_id": row["treatment_catalogue_release_id"],
                    "activated_at": row["activated_at"],
                    "completed_at": row["completed_at"],
                },
                "version_token": token,
                "market_evidence_status": {"domain": "market_evidence", "consumes": "read_only"},
                "snapshot_status": {"domain": "free_snapshot", "separate": True},
            }
        )

    def _candidate_queue(self, revision_id: str) -> APIResponse:
        if not campaign_api_store.revision_exists(self.connection, revision_id):
            return self._not_found()
        queue = build_candidate_queue(self.connection, revision_id)
        return APIResponse(queue.to_canonical_dict())

    # -- activation ---------------------------------------------------------
    def _activation_preview(
        self, revision_id: str, payload: Mapping[str, Any] | None
    ) -> APIResponse:
        fields = _require_exact_fields(payload, {"treatment_catalogue_release_id"})
        catalogue = _require_text(
            fields["treatment_catalogue_release_id"], "treatment_catalogue_release_id"
        )
        row = campaign_api_store.activation_revision_row(
            self.connection,
            revision_id,
        )
        if row is None:
            return self._not_found()
        # Preparation: bind the catalogue to the planned revision so intents can
        # be selected against it before activation.  Binding is idempotent.
        try:
            token = bind_treatment_catalogue(
                self.connection,
                revision_id=revision_id,
                treatment_catalogue_release_id=catalogue,
            )
        except CampaignActivationConflict as exc:
            raise CampaignAPIError(
                str(exc), code="conflict", status=HTTPStatus.CONFLICT
            ) from exc
        except CampaignRuntimeError as exc:
            raise CampaignAPIError(
                str(exc), code="invalid_request", status=HTTPStatus.BAD_REQUEST
            ) from exc
        selected_intents = campaign_api_store.selected_intent_count(
            self.connection,
            revision_id,
        )
        summary = {
            "action": "activate",
            "revision_id": revision_id,
            "treatment_catalogue_release_id": catalogue,
            "current_state": row["state"],
            "query_scope_status": row["query_scope_status"],
            "selected_intent_count": selected_intents,
        }
        return self._store_preview(
            "activation", revision_id, token,
            payload_fields={},
            summary=summary,
        )

    def _activation_confirm(
        self, revision_id: str, payload: Mapping[str, Any] | None
    ) -> APIResponse:
        fields = _require_exact_fields(payload, {"preview_id", "activated_by"})
        activated_by = _require_text(fields["activated_by"], "activated_by")
        self._consume_preview(
            "activation", revision_id, fields["preview_id"], payload_fields={}
        )
        try:
            result = activate_campaign_revision(
                self.connection,
                revision_id=revision_id,
                activated_by=activated_by,
                clock=self.clock,
            )
        except CampaignActivationConflict as exc:
            raise CampaignAPIError(
                str(exc), code="conflict", status=HTTPStatus.CONFLICT
            ) from exc
        except CampaignRuntimeError as exc:
            raise CampaignAPIError(
                str(exc), code="invalid_request", status=HTTPStatus.BAD_REQUEST
            ) from exc
        return APIResponse(
            {
                "action": "activate",
                "revision_id": result.revision_id,
                "state": result.state,
                "activated_at": result.activated_at,
                "version_token": result.version_token,
                "replayed": result.replayed,
            },
            HTTPStatus.OK if result.replayed else HTTPStatus.CREATED,
        )

    # -- completion ---------------------------------------------------------
    def _completion_preview(
        self, revision_id: str, payload: Mapping[str, Any] | None
    ) -> APIResponse:
        fields = _require_exact_fields(payload, {"completion_reason"})
        reason = _require_text(fields["completion_reason"], "completion_reason")
        row = campaign_api_store.completion_revision_row(
            self.connection,
            revision_id,
        )
        if row is None:
            return self._not_found()
        token = revision_version_token(self.connection, revision_id)
        plan = plan_campaign_completion(
            collect_campaign_job_views(self.connection, revision_id)
        )
        summary = {
            "action": "complete",
            "revision_id": revision_id,
            "current_state": row["state"],
            "completion_reason": reason,
            "blockers": list(plan.blockers),
            "cancellations": list(plan.cancellations),
            "is_blocked": plan.is_blocked,
        }
        return self._store_preview(
            "completion", revision_id, token,
            payload_fields={"completion_reason": reason},
            summary=summary,
        )

    def _completion_confirm(
        self, revision_id: str, payload: Mapping[str, Any] | None
    ) -> APIResponse:
        fields = _require_exact_fields(
            payload, {"preview_id", "completion_reason", "completed_by"}
        )
        reason = _require_text(fields["completion_reason"], "completion_reason")
        completed_by = _require_text(fields["completed_by"], "completed_by")
        self._consume_preview(
            "completion", revision_id, fields["preview_id"],
            payload_fields={"completion_reason": reason},
        )
        try:
            result = complete_campaign_revision(
                self.connection,
                revision_id=revision_id,
                completed_by=completed_by,
                completion_reason=reason,
                clock=self.clock,
            )
        except CampaignCompletionBlocked as exc:
            raise CampaignAPIError(
                "campaign completion is blocked by running work",
                code="completion_blocked", status=HTTPStatus.CONFLICT,
            ) from exc
        except CampaignCompletionConflict as exc:
            raise CampaignAPIError(
                str(exc), code="conflict", status=HTTPStatus.CONFLICT
            ) from exc
        except CampaignRuntimeError as exc:
            raise CampaignAPIError(
                str(exc), code="invalid_request", status=HTTPStatus.BAD_REQUEST
            ) from exc
        return APIResponse(
            {
                "action": "complete",
                "revision_id": result.revision_id,
                "state": result.state,
                "completed_at": result.completed_at,
                "cancelled_job_ids": list(result.cancelled_job_ids),
                "replayed": result.replayed,
            }
        )

    # -- candidate disposition ---------------------------------------------
    def _candidate_disposition(
        self, candidate_id: str, payload: Mapping[str, Any] | None
    ) -> APIResponse:
        fields = _require_exact_fields(
            payload, {"disposition", "decided_by", "notes"}
        )
        decided_by = _require_text(fields["decided_by"], "decided_by")
        notes = fields["notes"]
        if notes is not None and not isinstance(notes, str):
            raise CampaignAPIError(
                "notes must be text or null", code="invalid_request",
                status=HTTPStatus.BAD_REQUEST,
            )
        try:
            disposition = CampaignCandidateDisposition(fields["disposition"])
        except ValueError as exc:
            raise CampaignAPIError(
                "disposition is not a recognized value", code="invalid_request",
                status=HTTPStatus.BAD_REQUEST,
            ) from exc
        if not campaign_api_store.candidate_exists(self.connection, candidate_id):
            return self._not_found()
        now = _iso(self.clock())
        if self.connection.in_transaction:
            raise CampaignRuntimeError("disposition requires a closed transaction")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            event_sha256 = canonical_sha256(
                {
                    "candidate_id": candidate_id,
                    "disposition": disposition.value,
                    "decided_by": decided_by,
                    "decided_at": now,
                    "notes": notes,
                }
            )
            campaign_api_store.insert_candidate_decision_event(
                self.connection,
                event_id=f"campaign-candidate-decision-{event_sha256}",
                candidate_id=candidate_id,
                decision=disposition.value,
                decided_by=decided_by,
                decided_at=now,
                notes=notes,
            )
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        return APIResponse(
            {
                "action": "disposition",
                "candidate_id": candidate_id,
                "disposition": disposition.value,
                "decided_at": now,
            },
            HTTPStatus.CREATED,
        )

    # -- preview storage ----------------------------------------------------
    def _store_preview(
        self,
        endpoint: str,
        revision_id: str,
        version_token: str,
        *,
        payload_fields: dict,
        summary: dict,
    ) -> APIResponse:
        now = self.clock()
        expires_at = now + timedelta(seconds=self.preview_ttl_seconds)
        payload_sha256 = canonical_sha256(
            {"endpoint": endpoint, "fields": payload_fields}
        )
        body = {
            "preview_id": "",
            "endpoint": endpoint,
            "revision_id": revision_id,
            "version_token": version_token,
            "expires_at": _iso(expires_at),
            "summary": summary,
        }
        preview_id = "campaign-preview-" + canonical_sha256(
            {
                "endpoint": endpoint,
                "revision_id": revision_id,
                "version_token": version_token,
                "payload_sha256": payload_sha256,
                "created_at": _iso(now),
                "nonce": secrets.token_hex(16),
            }
        )
        body["preview_id"] = preview_id
        if self.connection.in_transaction:
            raise CampaignRuntimeError("preview storage requires a closed transaction")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            campaign_api_store.insert_action_preview(
                self.connection,
                preview_id=preview_id,
                endpoint=endpoint,
                revision_id=revision_id,
                revision_version_token=version_token,
                payload_sha256=payload_sha256,
                response_json=canonical_json(body),
                expires_at=_iso(expires_at),
                created_at=_iso(now),
            )
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        return APIResponse(body, HTTPStatus.CREATED)

    def _consume_preview(
        self,
        endpoint: str,
        revision_id: str,
        preview_id: Any,
        *,
        payload_fields: dict,
    ) -> None:
        preview_id = _require_text(preview_id, "preview_id")
        row = campaign_api_store.read_action_preview(
            self.connection,
            preview_id=preview_id,
            endpoint=endpoint,
        )
        if row is None or row["campaign_revision_id"] != revision_id:
            raise CampaignAPIError(
                "preview not found", code="preview_not_found",
                status=HTTPStatus.NOT_FOUND,
            )
        if self.clock() >= _parse_iso(row["expires_at"]):
            raise CampaignAPIError(
                "preview has expired", code="preview_expired", status=HTTPStatus.GONE
            )
        expected_payload_sha256 = canonical_sha256(
            {"endpoint": endpoint, "fields": payload_fields}
        )
        if row["payload_sha256"] != expected_payload_sha256:
            raise CampaignAPIError(
                "confirm payload does not match the preview",
                code="preview_conflict", status=HTTPStatus.CONFLICT,
            )
        current_token = revision_version_token(self.connection, revision_id)
        if row["revision_version_token"] != current_token:
            raise CampaignAPIError(
                "campaign revision changed since the preview was taken",
                code="preview_conflict", status=HTTPStatus.CONFLICT,
            )

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _parts(path: str) -> list[str]:
        return [segment for segment in path.strip("/").split("/") if segment]

    @staticmethod
    def _not_found() -> APIResponse:
        return APIResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)

    @staticmethod
    def _error(exc: CampaignAPIError) -> APIResponse:
        return APIResponse({"error": exc.safe_message, "code": exc.code}, exc.status)

    @staticmethod
    def _redacted_internal_error() -> APIResponse:
        # Never surface internal exception text, arguments, or stack detail.
        return APIResponse(
            {"error": "internal error", "code": "internal_error"},
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


@contextmanager
def open_campaign_api_session(
    database_path: Path | str,
    *,
    clock: Clock = _utc_now,
    preview_ttl_seconds: int = PREVIEW_TTL_SECONDS,
    max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES,
    max_batch_actions: int = MAX_BATCH_ACTIONS,
) -> Iterator[CampaignAPI]:
    """Yield one request-scoped API and always close its SQLite connection.

    ``ThreadingHTTPServer`` may execute each request on a different thread.
    A connection therefore belongs to exactly one session instead of being
    cached on the process-wide web application.  Rolling back an unfinished
    transaction on exit prevents a failed request from leaking transaction
    state into a later request.

    Schema creation and migrations remain the outreach facade's responsibility;
    this helper opens only an already-initialized campaign preview database.
    """

    connection = sqlite3.connect(database_path, timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        yield CampaignAPI(
            connection,
            clock=clock,
            preview_ttl_seconds=preview_ttl_seconds,
            max_request_body_bytes=max_request_body_bytes,
            max_batch_actions=max_batch_actions,
        )
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


__all__ = [
    "APIResponse",
    "CampaignAPI",
    "CampaignAPIError",
    "MAX_BATCH_ACTIONS",
    "MAX_REQUEST_BODY_BYTES",
    "PREVIEW_TTL_SECONDS",
    "open_campaign_api_session",
]
