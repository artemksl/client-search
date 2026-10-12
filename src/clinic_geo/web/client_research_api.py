"""Strict HTTP adapter for the supplied-directory client research workspace.

Persistence and assessment belong to the public outreach service. Execution is
injected by web composition, so reading the panel never admits browser work.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass, is_dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote


MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024
MAX_DIRECTORY_BYTES = 10 * 1024 * 1024
PREFIX = "/api/client-research"
ACTIONS = frozenset({"save_contact", "add_note", "log_external_contact", "log_external_reply", "suppress", "unsuppress"})
CHECK_MODES = frozenset({"initial", "recommendations", "identification"})
QUERY_FIELDS = frozenset({"q", "priority", "area", "treatment", "state", "contact_status", "view", "limit", "offset"})
CONFIG_FIELDS = frozenset({
    "treatments", "cells", "source_provider", "judge_provider", "providers", "profile_ids",
    "source_template", "recommendation_template",
    "direct_freshness_days", "recommendation_freshness_hours",
})
POST_FIELDS = {
    "create": frozenset({"name", "city", "country_code", "country_name", "language", "timezone"}),
    "preview_import": frozenset({"file_name", "content_base64", "base_directory_revision", "account_mappings"}),
    "commit_import": frozenset({"preview_id", "expected_workspace_version", "request_id", "account_mappings"}),
    "configure": frozenset({"configuration", "expected_workspace_version"}),
    "preview_checks": frozenset({"mode", "branch_ids"}),
    "start_checks": frozenset({"preview_id", "expected_fingerprint", "expected_workspace_version", "request_id"}),
    "schedule": frozenset({"enabled", "preview_id", "expected_fingerprint", "expected_workspace_version", "request_id"}),
    "action": frozenset({"action", "expected_account_version", "request_id", "branch_id", "channel", "value", "notes", "reason", "occurred_at", "contact_id", "expected_contact_version"}),
    "review": frozenset({"reason", "actor"}),
}
REQUIRED_FIELDS = {
    "create": {"city", "country_code", "country_name"},
    "preview_import": {"file_name", "content_base64"},
    "commit_import": {"preview_id", "expected_workspace_version", "request_id"},
    "configure": {"configuration", "expected_workspace_version"},
    "preview_checks": set(),
    "start_checks": {"preview_id", "expected_fingerprint", "expected_workspace_version", "request_id"},
    "schedule": {"enabled", "expected_workspace_version", "request_id"},
    "action": {"action", "expected_account_version", "request_id"},
    "review": {"reason"},
}


@dataclass(frozen=True)
class ApiResponse:
    payload: dict[str, Any]
    status: int = HTTPStatus.OK


def _default_service(root: Path):
    from clinic_geo.outreach.api import ClientResearchService

    return ClientResearchService(root)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _unknown(values: Mapping, allowed: frozenset, label: str) -> None:
    unexpected = sorted(set(values) - allowed)
    if unexpected:
        raise ValueError(f"Unknown {label} fields: {', '.join(unexpected)}")


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if str(result) != str(value) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return result


def _parts(path: str) -> list[str]:
    if not path.startswith(PREFIX + "/"):
        return []
    result = [unquote(part) for part in path[len(PREFIX) + 1:].split("/")]
    if any(not part or any(c in part for c in ("/", "\\", "\x00")) for part in result):
        return []
    return result


def _error(exc: Exception) -> ApiResponse:
    name = type(exc).__name__
    code = str(getattr(exc, "code", "") or "")
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status is None:
        if name in {"NotFoundError", "ClientResearchNotFoundError"} or isinstance(exc, KeyError):
            status, code = HTTPStatus.NOT_FOUND, code or "not_found"
        elif "Conflict" in name or name == "IntegrityError":
            status, code = HTTPStatus.CONFLICT, code or "conflict"
        elif "Unavailable" in name or "Capability" in name or isinstance(exc, ImportError):
            status, code = HTTPStatus.SERVICE_UNAVAILABLE, code or "runner_unavailable"
        elif name == "SuppliedDirectoryValidationError" or "Configuration" in name:
            status, code = HTTPStatus.UNPROCESSABLE_ENTITY, code or "invalid_format"
        elif isinstance(exc, ValueError):
            status, code = HTTPStatus.BAD_REQUEST, code or "invalid_request"
        else:
            status, code = HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error"
    body: dict[str, Any] = {"error": str(exc) if int(status) < 500 or int(status) == 503 else "Client research could not complete this request.", "code": code or "client_research_error"}
    if getattr(exc, "issues", None):
        body["issues"] = _jsonable(exc.issues)
    return ApiResponse(body, int(status))


class ClientResearchAPI:
    def __init__(self, root: Path | str, *, service_factory: Callable | None = None, coordinator_factory: Callable | None = None):
        self.root = Path(root).resolve()
        self.service_factory = service_factory or _default_service
        self.coordinator_factory = coordinator_factory

    def handle_get(self, path: str, query: Mapping[str, list[str]] | None = None) -> ApiResponse:
        service = None
        try:
            query = query or {}
            parts = _parts(path)
            valid = (parts == ["workspaces"] or (len(parts) in {2, 3, 4} and parts[0] == "workspaces" and (len(parts) == 2 or parts[2] == "accounts")) or (len(parts) == 2 and parts[0] in {"rounds", "evidence"}))
            if not valid:
                return ApiResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)
            account_list = len(parts) == 3 and parts[2] == "accounts"
            _unknown(query, QUERY_FIELDS if account_list else frozenset(), "query")
            values = {}
            for key, entries in query.items():
                if not isinstance(entries, (list, tuple)) or len(entries) != 1:
                    raise ValueError(f"Query field {key} must occur once")
                values[key] = entries[0]
            if account_list:
                values["limit"] = _integer(values.get("limit", "100"), "limit", minimum=1, maximum=500)
                values["offset"] = _integer(values.get("offset", "0"), "offset")
                for key, allowed in {"view": {"queue", "all"}, "priority": {"", "P1", "P2", "P3"}, "state": {"", "awaiting_checks", "awaiting_identification", "needs_review", "needs_refresh", "outside_selected_scope", "could_not_identify", "identified_not_recommended", "recommended"}, "contact_status": {"", "uncontacted", "contacted", "replied", "suppressed", "closed", "held"}}.items():
                    if key in values and values[key] not in allowed:
                        raise ValueError(f"Unsupported {key}: {values[key]}")
                if "state" in values:
                    values["evidence_state"] = values.pop("state")
                if "contact_status" in values:
                    values["contact_state"] = values.pop("contact_status")
            service = self.service_factory(self.root)
            if parts == ["workspaces"]:
                key, result = "workspaces", service.list_workspaces()
            elif parts[0] == "rounds":
                key, result = "round", service.get_round(parts[1])
            elif parts[0] == "evidence":
                key, result = "evidence", service.get_evidence(parts[1])
                if self.coordinator_factory:
                    coordinator = self.coordinator_factory(service)
                    if callable(getattr(coordinator, "read_evidence", None)):
                        result = coordinator.read_evidence(parts[1])
            elif len(parts) == 2:
                key, result = "workspace", service.get_workspace(parts[1])
                if self.coordinator_factory:
                    coordinator = self.coordinator_factory(service)
                    if callable(getattr(coordinator, "inspect_availability", None)):
                        result["capabilities"] = coordinator.inspect_availability()
            elif account_list:
                key, result = "accounts", service.list_accounts(parts[1], **values)
            else:
                key, result = "account", service.get_account(parts[1], parts[3])
            return ApiResponse({key: _jsonable(result)})
        except Exception as exc:
            return _error(exc)
        finally:
            if service is not None and callable(getattr(service, "close", None)):
                service.close()

    def handle_post(self, path: str, payload: Mapping[str, Any] | None) -> ApiResponse:
        service = None
        try:
            if not isinstance(payload, Mapping):
                raise ValueError("The request body must be a JSON object")
            parts = _parts(path)
            operation = None
            if parts == ["workspaces"]:
                operation = "create"
            elif len(parts) == 3 and parts[0] == "evidence" and parts[2] == "review":
                operation = "review"
            elif len(parts) >= 3 and parts[0] == "workspaces":
                operation = {("imports", "preview"): "preview_import", ("imports", "commit"): "commit_import", ("configuration",): "configure", ("checks", "preview"): "preview_checks", ("checks", "start"): "start_checks", ("schedule",): "schedule"}.get(tuple(parts[2:]))
                if len(parts) == 5 and parts[2] == "accounts" and parts[4] == "actions":
                    operation = "action"
            if operation is None:
                return ApiResponse({"error": "not found", "code": "not_found"}, HTTPStatus.NOT_FOUND)
            _unknown(payload, POST_FIELDS[operation], "request")
            missing = sorted(REQUIRED_FIELDS[operation] - set(payload))
            if missing:
                raise ValueError(f"Missing request fields: {', '.join(missing)}")
            values = dict(payload)
            for key in ("preview_id", "expected_fingerprint"):
                if key in values and (not isinstance(values[key], str) or not values[key].strip()):
                    raise ValueError(f"{key} must be a nonempty string")
            for key in ("expected_workspace_version", "expected_account_version", "expected_contact_version"):
                if key in values:
                    values[key] = _integer(values[key], key, minimum=1)
            if operation in {"commit_import", "start_checks", "action", "schedule"}:
                request_id = values.get("request_id")
                if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
                    raise ValueError("A request_id of 1–128 characters is required")
            if operation == "configure":
                configuration = values.get("configuration")
                if not isinstance(configuration, Mapping):
                    raise ValueError("configuration must be an object")
                _unknown(configuration, CONFIG_FIELDS, "configuration")
            if operation == "preview_checks":
                values.setdefault("mode", "initial")
                if values.get("mode", "initial") not in CHECK_MODES:
                    raise ValueError("mode must be initial, recommendations, or identification")
                if "branch_ids" in values and (not isinstance(values["branch_ids"], list) or any(not isinstance(item, str) or not item for item in values["branch_ids"])):
                    raise ValueError("branch_ids must be a list of explicit branch IDs")
                if "branch_ids" in values and len(values["branch_ids"]) != len(set(values["branch_ids"])):
                    raise ValueError("branch_ids must not contain duplicates")
            if operation == "action" and values.get("action") not in ACTIONS:
                raise ValueError("Unsupported account action")
            if operation == "schedule" and not isinstance(values.get("enabled"), bool):
                raise ValueError("enabled must be a boolean")
            if operation == "schedule" and values["enabled"]:
                if not isinstance(values.get("preview_id"), str) or not isinstance(values.get("expected_fingerprint"), str):
                    raise ValueError("Enabling a schedule requires its reviewed preview and fingerprint")
            if operation == "preview_import":
                content = values.get("content_base64")
                if not isinstance(content, str):
                    raise ValueError("content_base64 must contain the selected CSV bytes")
                if len(content) > ((MAX_DIRECTORY_BYTES + 2) // 3) * 4:
                    return ApiResponse({"error": "The CSV exceeds 10 MiB.", "code": "file_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                try:
                    decoded_size = len(base64.b64decode(content, validate=True))
                except (ValueError, binascii.Error):
                    raise ValueError("content_base64 must contain valid base64 CSV bytes") from None
                if decoded_size > MAX_DIRECTORY_BYTES:
                    return ApiResponse({"error": "The CSV exceeds 10 MiB.", "code": "file_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            service = self.service_factory(self.root)
            status = HTTPStatus.OK
            if operation == "create":
                key, result, status = "workspace", service.create_workspace(**values), HTTPStatus.CREATED
            elif operation == "preview_import":
                try:
                    key, result = "preview", service.preview_import(parts[1], **values)
                except ValueError as exc:
                    response = _error(exc)
                    response.payload["code"] = "invalid_format"
                    return ApiResponse(response.payload, HTTPStatus.UNPROCESSABLE_ENTITY)
            elif operation == "commit_import":
                key, result = "import", service.commit_import(parts[1], **values)
            elif operation == "configure":
                try:
                    key, result = "configuration", service.configure(parts[1], **values)
                except ValueError as exc:
                    response = _error(exc)
                    response.payload["code"] = "invalid_configuration"
                    return ApiResponse(response.payload, HTTPStatus.UNPROCESSABLE_ENTITY)
            elif operation == "action":
                key, result = "account", service.account_action(parts[1], parts[3], **values)
            elif operation == "review":
                key, result = "review", service.review_evidence(parts[1], **values)
            else:
                if self.coordinator_factory is None:
                    return ApiResponse({"error": "The client research runner is unavailable.", "code": "runner_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
                coordinator = self.coordinator_factory(service)
                if operation == "preview_checks":
                    key, result = "preview", coordinator.preview_checks(parts[1], **values)
                elif operation == "start_checks":
                    key, result, status = "start", coordinator.start_checks(parts[1], **values), HTTPStatus.ACCEPTED
                else:
                    key, result = "schedule", coordinator.set_schedule(parts[1], **values)
            return ApiResponse({key: _jsonable(result)}, status)
        except Exception as exc:
            return _error(exc)
        finally:
            if service is not None and callable(getattr(service, "close", None)):
                service.close()
